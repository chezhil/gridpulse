"""User-built electricity networks, each isolated in its own FalkorDB graph.

A network is described by a JSON *spec* (generators, buses, substations,
lines, loads, batteries). It is validated, then written into a dedicated
graph named ``gridpulse_net_<id>`` using the same labels and relationship
types as the demo grid, so every engine feature (trace, ``dc-pf/1`` power
flow, N-1, betweenness, reroutes, plan checks) runs on it unchanged. A
registry graph records who owns which network; owners only ever see their own.

Test cases (line trips, demand surges, battery/shedding what-ifs, N-1) are
evaluated with :func:`simulate`, which also searches for the smallest safe
mitigation (battery first, then Tier-3 shedding) and reports whether the
grid can be saved without touching Tier-1/Tier-2 loads.
"""

from __future__ import annotations

import json
import math
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import topology as topo
from .client import GraphClient, GraphError, GraphNotSeededError, GraphSettings, InvalidRequestError, UnknownEntityError, get_client
from .contingency import scan_n1
from .queries import read_grid_snapshot
from .thermal import project_loading

REGISTRY_GRAPH = "gridpulse_registry"
GRAPH_PREFIX = "gridpulse_net_"
MAX_NODES = 2000
MAX_LINES = 5000
MAX_NETWORKS_PER_OWNER = 25
MAX_CASES = 25
BATCH = 2000

log = logging.getLogger(__name__)
_ID = re.compile(r"^[A-Z0-9_]{1,40}$")
CATEGORY_BY_TIER = {1: "HOSPITAL", 2: "RESIDENTIAL", 3: "INDUSTRIAL"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Spec validation                                                             #
# --------------------------------------------------------------------------- #


def _num(value: Any, where: str, problems: List[str], lo: float = 0.0, hi: float = 1e6, required: bool = True,
         lo_inclusive: bool = True) -> Optional[float]:
    if value is None:
        if required:
            problems.append(f"{where}: required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        problems.append(f"{where}: must be a finite number")
        return None
    if value < lo or (value == lo and not lo_inclusive) or value > hi:
        problems.append(f"{where}: must be {'>=' if lo_inclusive else '>'} {lo} and <= {hi}")
        return None
    return float(value)


def validate_spec(spec: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Normalise a network spec. Raises InvalidRequestError listing every problem."""
    problems: List[str] = []
    warnings: List[str] = []
    if not isinstance(spec, dict):
        raise InvalidRequestError("spec must be an object")
    sections = ("generators", "buses", "substations", "lines", "loads", "batteries")
    unknown = sorted(set(spec) - set(sections) - {"name", "description", "layout"})
    if unknown:
        problems.append(f"unknown sections {unknown}")
    for key in sections:
        if spec.get(key) is not None and not isinstance(spec.get(key), list):
            problems.append(f"{key}: must be a list")
    if problems:
        raise InvalidRequestError("Network spec rejected", problems=problems)

    name = str(spec.get("name") or "Untitled network").strip()[:80]
    seen: Dict[str, str] = {}

    def claim(raw_id: Any, kind: str, where: str) -> Optional[str]:
        if not isinstance(raw_id, str) or not _ID.match(raw_id):
            problems.append(f"{where}.id: must match {_ID.pattern} (uppercase letters, digits, underscore)")
            return None
        if raw_id in seen:
            problems.append(f"{where}.id: '{raw_id}' already used by a {seen[raw_id]}")
            return None
        seen[raw_id] = kind
        return raw_id

    out: Dict[str, Any] = {"name": name, "description": str(spec.get("description") or "")[:500]}
    out["buses"] = []
    for i, b in enumerate(spec.get("buses") or []):
        if not isinstance(b, dict):
            problems.append(f"buses[{i}]: must be an object")
            continue
        bid = claim(b.get("id"), "bus", f"buses[{i}]")
        if bid:
            out["buses"].append({"id": bid, "name": str(b.get("name") or bid)[:80]})
    out["substations"] = []
    for i, s in enumerate(spec.get("substations") or []):
        if not isinstance(s, dict):
            problems.append(f"substations[{i}]: must be an object")
            continue
        sid = claim(s.get("id"), "substation", f"substations[{i}]")
        cap = _num(s.get("capacity_mw"), f"substations[{i}].capacity_mw", problems, 0, 1e6, lo_inclusive=False)
        if sid and cap:
            out["substations"].append({"id": sid, "name": str(s.get("name") or sid)[:80], "capacity_mw": cap})
    grid_nodes = {b["id"] for b in out["buses"]} | {s["id"] for s in out["substations"]}
    sub_ids = {s["id"] for s in out["substations"]}

    out["generators"] = []
    for i, g in enumerate(spec.get("generators") or []):
        if not isinstance(g, dict):
            problems.append(f"generators[{i}]: must be an object")
            continue
        gid = claim(g.get("id"), "generator", f"generators[{i}]")
        output = _num(g.get("output_mw", 0), f"generators[{i}].output_mw", problems, 0, 1e6)
        cap = _num(g.get("capacity_mw"), f"generators[{i}].capacity_mw", problems, 0, 1e6, required=False, lo_inclusive=False)
        target = g.get("connects_to")
        if target not in grid_nodes:
            problems.append(f"generators[{i}].connects_to: '{target}' is not a bus or substation")
        if cap is not None and output is not None and output > cap:
            problems.append(f"generators[{i}]: output_mw exceeds capacity_mw")
        if gid and output is not None and target in grid_nodes:
            out["generators"].append({"id": gid, "name": str(g.get("name") or gid)[:80], "output_mw": output,
                                      "capacity_mw": cap, "connects_to": target})
    if not out["generators"] and not any(p.startswith("generators") for p in problems):
        problems.append("generators: at least one generator is required")

    out["lines"] = []
    line_ids = set()
    for i, ln in enumerate(spec.get("lines") or []):
        where = f"lines[{i}]"
        if not isinstance(ln, dict):
            problems.append(f"{where}: must be an object")
            continue
        # Lines share the id namespace with nodes, so a trace origin id is never ambiguous.
        lid = claim(ln.get("id"), "line", where)
        src, dst = ln.get("from"), ln.get("to")
        if src not in grid_nodes or dst not in grid_nodes:
            problems.append(f"{where}: from/to must be existing buses or substations (got {src!r} -> {dst!r})")
        elif src == dst:
            problems.append(f"{where}: from and to must differ")
        x = _num(ln.get("impedance_pu", 0.01), f"{where}.impedance_pu", problems, 0, 10, lo_inclusive=False)
        limit = _num(ln.get("limit_mw"), f"{where}.limit_mw", problems, 0, 1e6, lo_inclusive=False)
        backup = ln.get("backup")
        backup_out = None
        if backup is not None:
            if not isinstance(backup, dict):
                problems.append(f"{where}.backup: must be an object")
            else:
                bx = _num(backup.get("impedance_pu", x or 0.01), f"{where}.backup.impedance_pu", problems, 0, 10, lo_inclusive=False)
                bl = _num(backup.get("limit_mw"), f"{where}.backup.limit_mw", problems, 0, 1e6, lo_inclusive=False)
                if bx and bl:
                    backup_out = {"impedance_pu": bx, "limit_mw": bl}
        if lid and x and limit and src in grid_nodes and dst in grid_nodes and src != dst:
            line_ids.add(lid)
            out["lines"].append({"id": lid, "from": src, "to": dst, "impedance_pu": x, "limit_mw": limit, "backup": backup_out})

    out["loads"] = []
    for i, ld in enumerate(spec.get("loads") or []):
        where = f"loads[{i}]"
        if not isinstance(ld, dict):
            problems.append(f"{where}: must be an object")
            continue
        cid = claim(ld.get("id"), "load", where)
        sub = ld.get("substation")
        if sub not in sub_ids:
            problems.append(f"{where}.substation: '{sub}' is not a substation")
        mw = _num(ld.get("mw_demand"), f"{where}.mw_demand", problems, 0, 1e6)
        tier = ld.get("priority_tier", 2)
        if isinstance(tier, bool) or tier not in (1, 2, 3):
            problems.append(f"{where}.priority_tier: must be 1, 2 or 3")
        category = ld.get("category") or CATEGORY_BY_TIER.get(tier, "COMMERCIAL")
        if category not in topo.CONSUMER_CATEGORIES:
            problems.append(f"{where}.category: must be one of {list(topo.CONSUMER_CATEGORIES)}")
        breaker = ld.get("breaker_id") or (f"BRK_{cid}" if cid else None)
        if breaker is not None and (not isinstance(breaker, str) or not _ID.match(breaker)):
            problems.append(f"{where}.breaker_id: must match {_ID.pattern}"
                            + (" (the default BRK_<id> is too long; give breaker_id)" if not ld.get("breaker_id") else ""))
            breaker = None
        feeder = ld.get("feeder_id") or (f"FDR_{breaker}"[:40] if breaker else None)
        if feeder is not None and (not isinstance(feeder, str) or not _ID.match(feeder)):
            problems.append(f"{where}.feeder_id: must match {_ID.pattern}")
            feeder = None
        if cid and sub in sub_ids and mw is not None and tier in (1, 2, 3) and breaker and feeder:
            out["loads"].append({"id": cid, "name": str(ld.get("name") or cid)[:80], "substation": sub, "mw_demand": mw,
                                 "priority_tier": tier, "category": category, "breaker_id": breaker,
                                 "feeder_id": feeder})

    out["batteries"] = []
    for i, bt in enumerate(spec.get("batteries") or []):
        where = f"batteries[{i}]"
        if not isinstance(bt, dict):
            problems.append(f"{where}: must be an object")
            continue
        bid = claim(bt.get("id"), "battery", where)
        sub = bt.get("substation")
        if sub not in sub_ids:
            problems.append(f"{where}.substation: '{sub}' is not a substation")
        mx = _num(bt.get("max_discharge_mw"), f"{where}.max_discharge_mw", problems, 0, 1e5, lo_inclusive=False)
        soc = _num(bt.get("soc_pct", 80), f"{where}.soc_pct", problems, 0, 100)
        if bid and sub in sub_ids and mx and soc is not None:
            out["batteries"].append({"id": bid, "substation": sub, "max_discharge_mw": mx, "soc_pct": soc})

    # Optional drawing positions from the editor: {element_id: {"x": .., "y": ..}}; unknown ids are dropped.
    layout = spec.get("layout") or {}
    out["layout"] = {}
    if not isinstance(layout, dict):
        problems.append("layout: must be an object of {id: {x, y}}")
    else:
        for el_id, xy in layout.items():
            if el_id in seen and isinstance(xy, dict) and all(
                    isinstance(xy.get(k), (int, float)) and not isinstance(xy.get(k), bool) and math.isfinite(xy[k]) for k in ("x", "y")):
                out["layout"][el_id] = {"x": round(float(xy["x"]), 1), "y": round(float(xy["y"]), 1)}

    node_count = len(seen)
    if node_count > MAX_NODES:
        problems.append(f"too many elements ({node_count} > {MAX_NODES})")
    if len(out["lines"]) > MAX_LINES:
        problems.append(f"too many lines ({len(out['lines'])} > {MAX_LINES})")
    if problems:
        raise InvalidRequestError("Network spec rejected", problems=problems)

    # Structural warnings: things that are legal but probably not intended.
    adj: Dict[str, List[str]] = {}
    for ln in out["lines"]:
        adj.setdefault(ln["from"], []).append(ln["to"])
        adj.setdefault(ln["to"], []).append(ln["from"])
    reached = set()
    stack = [g["connects_to"] for g in out["generators"]]
    while stack:
        n = stack.pop()
        if n not in reached:
            reached.add(n)
            stack.extend(adj.get(n, []))
    unreached = sorted(grid_nodes - reached)
    if unreached:
        warnings.append(f"not connected to any generator: {unreached[:20]}")
    loaded = {ld["substation"] for ld in out["loads"]}
    idle = sorted(sub_ids - loaded)
    if idle:
        warnings.append(f"substations with no loads: {idle[:20]}")
    if not out["lines"]:
        warnings.append("network has no lines")
    return out, warnings


# --------------------------------------------------------------------------- #
# Graph construction                                                          #
# --------------------------------------------------------------------------- #


def _rows(spec: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    sub_ids = {s["id"] for s in spec["substations"]}
    circuits = []
    for ln in spec["lines"]:
        rel = "TERMINATES_AT" if ln["to"] in sub_ids else "SPAN"
        variants = [("UPPER_PRIMARY", ln["impedance_pu"], ln["limit_mw"])]
        if ln["backup"]:
            variants.append(("LOWER_BACKUP", ln["backup"]["impedance_pu"], ln["backup"]["limit_mw"]))
        for circuit, x, limit in variants:
            circuits.append({"rel": rel, "src": ln["from"], "dst": ln["to"], "props": {
                "id": ln["id"], "circuit": circuit, "impedance_pu": x, "limit_mw": limit,
                "flow_mw": 0.0, "status": "ENERGIZED", "sag_m": 0.0, "kv": 500}})
    return {
        "HydroPlant": [{"id": g["id"], "props": {"name": g["name"], "output_mw": g["output_mw"], "capacity_mw": g["capacity_mw"],
                                                 "voltage_kv": 500}} for g in spec["generators"]],
        "Tower": [{"id": b["id"], "props": {"name": b["name"], "status": "NOMINAL", "insulator_temp_c": 35.0}} for b in spec["buses"]],
        "Substation": [{"id": s["id"], "props": {"name": s["name"], "capacity_mw": s["capacity_mw"], "current_load_mw": 0.0,
                                                 "transformer_temp_c": 60.0, "status": "ONLINE"}} for s in spec["substations"]],
        "BESS": [{"id": b["id"], "props": {"soc_pct": b["soc_pct"], "max_discharge_mw": b["max_discharge_mw"],
                                           "active_injection_mw": 0.0, "status": "STANDBY"}} for b in spec["batteries"]],
        "Consumer": [{"id": c["id"], "props": {k: c[k] for k in ("name", "category", "priority_tier", "mw_demand", "breaker_id")}
                      | {"energized": True}} for c in spec["loads"]],
        "_feeds": [{"src": g["id"], "dst": g["connects_to"], "props": {"mw": g["output_mw"], "kv": 500, "impedance_pu": 0.0001}}
                   for g in spec["generators"]],
        "_circuits": circuits,
        "_connected": [{"src": b["id"], "dst": b["substation"], "props": {"max_mw": b["max_discharge_mw"], "inverter_id": f"INV_{b['id']}"}}
                       for b in spec["batteries"]],
        "_distributes": [{"src": c["substation"], "dst": c["id"], "props": {"feeder_id": c["feeder_id"], "priority_tier": c["priority_tier"]}}
                         for c in spec["loads"]],
    }


def build_graph(client: GraphClient, spec: Dict[str, Any]) -> Dict[str, int]:
    """Replace the contents of ``client``'s graph with ``spec`` (idempotent)."""
    try:
        client.query("net_clear", "MATCH (n) DETACH DELETE n", read_only=False, retries=0)
    except GraphNotSeededError:
        pass
    for label in topo.NODE_LABELS + ("GridMeta",):
        try:
            client.query(f"net_index_{label}", f"CREATE INDEX FOR (n:{label}) ON (n.id)", read_only=False, retries=0)
        except Exception as exc:
            if "already indexed" not in str(exc):
                raise
    rows = _rows(spec)
    for label in topo.NODE_LABELS:
        for i in range(0, len(rows[label]), BATCH):
            client.query(f"net_create_{label}", f"UNWIND $rows AS row CREATE (n:{label} {{id: row.id}}) SET n += row.props",
                         {"rows": rows[label][i:i + BATCH]}, read_only=False, retries=0)
    edge_sets = (
        ("FEEDS", "HydroPlant", "_feeds"),
        ("CONNECTED", "BESS", "_connected"),
        ("DISTRIBUTES_TO", "Substation", "_distributes"),
    )
    for rel, src_label, key in edge_sets:
        for i in range(0, len(rows[key]), BATCH):
            client.query(f"net_{rel}", f"UNWIND $rows AS row MATCH (a:{src_label} {{id: row.src}}), (b {{id: row.dst}}) "
                         f"CREATE (a)-[r:{rel}]->(b) SET r += row.props", {"rows": rows[key][i:i + BATCH]}, read_only=False, retries=0)
    for rel in ("SPAN", "TERMINATES_AT"):
        batch = [r for r in rows["_circuits"] if r["rel"] == rel]
        for i in range(0, len(batch), BATCH):
            client.query(f"net_{rel}", f"UNWIND $rows AS row MATCH (a {{id: row.src}}), (b {{id: row.dst}}) "
                         f"CREATE (a)-[r:{rel}]->(b) SET r += row.props", {"rows": batch[i:i + BATCH]}, read_only=False, retries=0)
    client.query("net_meta", "CREATE (m:GridMeta {id: $id, state_version: 1, schema_version: $schema, seeded_at: $now})",
                 {"id": topo.META_ID, "schema": topo.SCHEMA_VERSION, "now": _now()}, read_only=False, retries=0)
    return {"nodes": sum(len(rows[l]) for l in topo.NODE_LABELS),
            "edges": len(rows["_feeds"]) + len(rows["_circuits"]) + len(rows["_connected"]) + len(rows["_distributes"])}


# --------------------------------------------------------------------------- #
# Registry (ownership)                                                        #
# --------------------------------------------------------------------------- #


_CLIENTS: Dict[Tuple[Any, ...], GraphClient] = {}
_CLIENTS_LOCK = threading.Lock()


def _client_for(graph_name: str, base: Optional[GraphClient] = None) -> GraphClient:
    """One cached client (and connection pool) per graph, instead of a new pool on every request."""
    base = base or get_client()
    settings = GraphSettings(**{**base.settings.__dict__, "graph_name": graph_name})
    key = tuple(sorted(settings.__dict__.items()))
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = _CLIENTS[key] = GraphClient(settings)
        return client


def _forget_client(graph_name: str) -> None:
    with _CLIENTS_LOCK:
        for key in [k for k, c in _CLIENTS.items() if c.graph_name == graph_name]:
            del _CLIENTS[key]


def _registry(base: Optional[GraphClient] = None) -> GraphClient:
    return _client_for(REGISTRY_GRAPH, base)


def network_client(network_id: str, base: Optional[GraphClient] = None) -> GraphClient:
    return _client_for(GRAPH_PREFIX + network_id.lower(), base)


def _registry_rows(reg: GraphClient, cypher: str, params: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    try:
        return reg.query(name, cypher, params).rows
    except GraphNotSeededError:
        return []


def _owned(owner: str, network_id: str, base: Optional[GraphClient] = None) -> Dict[str, Any]:
    if not isinstance(network_id, str) or not re.match(r"^NET_[A-F0-9]{8}$", network_id):
        raise UnknownEntityError(f"No network '{network_id}'", network_id=network_id)
    rows = _registry_rows(_registry(base), "MATCH (n:Network {id: $id, owner: $owner}) RETURN properties(n) AS n",
                          {"id": network_id, "owner": owner}, "registry_get")
    if not rows:
        raise UnknownEntityError(f"No network '{network_id}'", network_id=network_id)
    return rows[0]["n"]


def _summary(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: record.get(k) for k in ("id", "name", "owner", "created_at", "updated_at")} | {
        "counts": json.loads(record.get("counts_json") or "{}"), "warnings": json.loads(record.get("warnings_json") or "[]")}


def create_network(owner: str, spec: Dict[str, Any], base: Optional[GraphClient] = None) -> Dict[str, Any]:
    normalized, warnings = validate_spec(spec)
    reg = _registry(base)
    count = _registry_rows(reg, "MATCH (n:Network {owner: $owner}) RETURN count(n) AS c", {"owner": owner}, "registry_count")
    if count and count[0]["c"] >= MAX_NETWORKS_PER_OWNER:
        raise InvalidRequestError(f"Each owner may keep at most {MAX_NETWORKS_PER_OWNER} networks; delete one first")
    network_id = "NET_" + uuid.uuid4().hex[:8].upper()
    counts = build_graph(network_client(network_id, base), normalized)
    now = _now()
    reg.query("registry_create", "CREATE (n:Network {id: $id, owner: $owner, name: $name, graph: $graph, created_at: $now, "
              "updated_at: $now, spec_json: $spec, counts_json: $counts, warnings_json: $warnings})",
              {"id": network_id, "owner": owner, "name": normalized["name"], "graph": GRAPH_PREFIX + network_id.lower(), "now": now,
               "spec": json.dumps(normalized), "counts": json.dumps(counts), "warnings": json.dumps(warnings)},
              read_only=False, retries=0)
    return get_network(owner, network_id, base)


def replace_network(owner: str, network_id: str, spec: Dict[str, Any], base: Optional[GraphClient] = None) -> Dict[str, Any]:
    old_spec = json.loads(_owned(owner, network_id, base)["spec_json"])
    normalized, warnings = validate_spec(spec)
    client = network_client(network_id, base)
    try:
        counts = build_graph(client, normalized)
    except GraphError:
        # build_graph clears first, so a failure part-way would leave a half-built graph; restore the saved one.
        try:
            build_graph(client, old_spec)
        except GraphError:
            log.exception("could not restore network %s after a failed replace", network_id)
        raise
    _registry(base).query("registry_update", "MATCH (n:Network {id: $id, owner: $owner}) SET n.name = $name, n.updated_at = $now, "
                          "n.spec_json = $spec, n.counts_json = $counts, n.warnings_json = $warnings",
                          {"id": network_id, "owner": owner, "name": normalized["name"], "now": _now(),
                           "spec": json.dumps(normalized), "counts": json.dumps(counts), "warnings": json.dumps(warnings)},
                          read_only=False, retries=0)
    return get_network(owner, network_id, base)


def get_network(owner: str, network_id: str, base: Optional[GraphClient] = None) -> Dict[str, Any]:
    record = _owned(owner, network_id, base)
    return {**_summary(record), "spec": json.loads(record["spec_json"])}


def list_networks(owner: str, base: Optional[GraphClient] = None) -> Dict[str, Any]:
    rows = _registry_rows(_registry(base), "MATCH (n:Network {owner: $owner}) RETURN properties(n) AS n ORDER BY n.created_at DESC",
                          {"owner": owner}, "registry_list")
    return {"owner": owner, "networks": [_summary(r["n"]) for r in rows], "limit": MAX_NETWORKS_PER_OWNER}


def delete_network(owner: str, network_id: str, base: Optional[GraphClient] = None) -> Dict[str, Any]:
    _owned(owner, network_id, base)
    client = network_client(network_id, base)
    try:
        client._ensure_graph().delete()
    except Exception as exc:
        if "empty key" not in str(exc) and "Invalid graph" not in str(exc):
            log.warning("could not delete graph for network %s: %s", network_id, exc)
    _forget_client(client.graph_name)
    _registry(base).query("registry_delete", "MATCH (n:Network {id: $id, owner: $owner}) DELETE n",
                          {"id": network_id, "owner": owner}, read_only=False, retries=0)
    return {"deleted": network_id}


def owned_client(owner: str, network_id: str, base: Optional[GraphClient] = None) -> GraphClient:
    """GraphClient for a network the owner has access to (raises 404-style error otherwise)."""
    _owned(owner, network_id, base)
    return network_client(network_id, base)


# --------------------------------------------------------------------------- #
# Templates                                                                   #
# --------------------------------------------------------------------------- #


def _demo_template() -> Dict[str, Any]:
    lines = []
    for seg in topo.SPAN_SEGMENTS:
        lines.append({"id": seg["id"], "from": seg["from"], "to": seg["to"],
                      "impedance_pu": topo.CIRCUIT_RATINGS["UPPER_PRIMARY"]["impedance_pu"],
                      "limit_mw": topo.CIRCUIT_RATINGS["UPPER_PRIMARY"]["limit_mw"],
                      "backup": {"impedance_pu": topo.CIRCUIT_RATINGS["LOWER_BACKUP"]["impedance_pu"],
                                 "limit_mw": topo.CIRCUIT_RATINGS["LOWER_BACKUP"]["limit_mw"]}})
    feeders = {r["to"]: r["props"]["feeder_id"] for r in topo.distributes_rows()}
    return {
        "name": "Alpine corridor (GridPulse demo)",
        "description": "The canonical 9-tower 500 kV corridor with parallel primary/backup circuits.",
        "generators": [{"id": "HYDRO_ALPINE_01", "name": "Headwaters Pumped Hydro", "output_mw": 840.0, "connects_to": "T1"}],
        "buses": [{"id": t["id"]} for t in topo.TOWERS],
        "substations": [{"id": s["id"], "capacity_mw": s["capacity_mw"]} for s in topo.SUBSTATIONS],
        "lines": lines,
        "loads": [{"id": c["id"], "name": c["name"], "substation": "SUB_METRO", "mw_demand": c["mw_demand"],
                   "priority_tier": c["priority_tier"], "category": c["category"], "breaker_id": c["breaker_id"],
                   "feeder_id": feeders[c["id"]]} for c in topo.CONSUMERS],
        "batteries": [{"id": b["id"], "substation": "SUB_METRO", "max_discharge_mw": b["max_discharge_mw"], "soc_pct": b["soc_pct"]}
                      for b in topo.BESS_UNITS],
    }


def _ring_template() -> Dict[str, Any]:
    ring = ["R1", "R2", "R3", "R4", "R5", "R6"]
    lines = [{"id": f"RL{i + 1}", "from": ring[i], "to": ring[(i + 1) % len(ring)], "impedance_pu": 0.02, "limit_mw": 450.0}
             for i in range(len(ring))]
    lines += [{"id": "TIE_NORTH", "from": "R3", "to": "SUB_NORTH", "impedance_pu": 0.01, "limit_mw": 400.0},
              {"id": "TIE_SOUTH", "from": "R5", "to": "SUB_SOUTH", "impedance_pu": 0.01, "limit_mw": 400.0}]
    return {
        "name": "Ring grid",
        "description": "Six buses in a loop feeding two substations: any single ring line can be lost, but flows shift.",
        "generators": [{"id": "GEN_RIVER", "output_mw": 0.0, "capacity_mw": 700.0, "connects_to": "R1"}],
        "buses": [{"id": r} for r in ring],
        "substations": [{"id": "SUB_NORTH", "capacity_mw": 380.0}, {"id": "SUB_SOUTH", "capacity_mw": 380.0}],
        "lines": lines,
        "loads": [
            {"id": "CLINIC_NORTH", "substation": "SUB_NORTH", "mw_demand": 40.0, "priority_tier": 1, "category": "HOSPITAL"},
            {"id": "HOMES_NORTH", "substation": "SUB_NORTH", "mw_demand": 180.0, "priority_tier": 2},
            {"id": "MILL_NORTH", "substation": "SUB_NORTH", "mw_demand": 90.0, "priority_tier": 3},
            {"id": "HOMES_SOUTH", "substation": "SUB_SOUTH", "mw_demand": 210.0, "priority_tier": 2},
            {"id": "SMELTER_SOUTH", "substation": "SUB_SOUTH", "mw_demand": 70.0, "priority_tier": 3},
        ],
        "batteries": [{"id": "BESS_NORTH", "substation": "SUB_NORTH", "max_discharge_mw": 40.0, "soc_pct": 85.0}],
    }


def _meshed_template() -> Dict[str, Any]:
    grid = [[f"M{r}{c}" for c in range(3)] for r in range(3)]
    lines = []
    n = 0
    for r in range(3):
        for c in range(3):
            if c < 2:
                n += 1
                lines.append({"id": f"ML{n}", "from": grid[r][c], "to": grid[r][c + 1], "impedance_pu": 0.015, "limit_mw": 380.0})
            if r < 2:
                n += 1
                lines.append({"id": f"ML{n}", "from": grid[r][c], "to": grid[r + 1][c], "impedance_pu": 0.015, "limit_mw": 380.0})
    lines += [{"id": "FEED_A", "from": "M22", "to": "SUB_A", "impedance_pu": 0.008, "limit_mw": 400.0},
              {"id": "FEED_B", "from": "M20", "to": "SUB_B", "impedance_pu": 0.008, "limit_mw": 350.0},
              {"id": "FEED_C", "from": "M02", "to": "SUB_C", "impedance_pu": 0.008, "limit_mw": 350.0}]
    return {
        "name": "Meshed city",
        "description": "A 3x3 meshed bus grid with two power plants and three substations.",
        "generators": [{"id": "GEN_COAST", "output_mw": 0.0, "capacity_mw": 800.0, "connects_to": "M00"},
                       {"id": "GEN_SOLAR", "output_mw": 150.0, "capacity_mw": 200.0, "connects_to": "M21"}],
        "buses": [{"id": b} for row in grid for b in row],
        "substations": [{"id": "SUB_A", "capacity_mw": 380.0}, {"id": "SUB_B", "capacity_mw": 320.0},
                        {"id": "SUB_C", "capacity_mw": 300.0}],
        "lines": lines,
        "loads": [
            {"id": "HOSPITAL_A", "substation": "SUB_A", "mw_demand": 35.0, "priority_tier": 1, "category": "HOSPITAL"},
            {"id": "DOWNTOWN_A", "substation": "SUB_A", "mw_demand": 220.0, "priority_tier": 2, "category": "COMMERCIAL"},
            {"id": "PORT_A", "substation": "SUB_A", "mw_demand": 60.0, "priority_tier": 3},
            {"id": "SUBURB_B", "substation": "SUB_B", "mw_demand": 190.0, "priority_tier": 2},
            {"id": "FACTORY_B", "substation": "SUB_B", "mw_demand": 80.0, "priority_tier": 3},
            {"id": "CAMPUS_C", "substation": "SUB_C", "mw_demand": 150.0, "priority_tier": 2, "category": "COMMERCIAL"},
            {"id": "DATACENTER_C", "substation": "SUB_C", "mw_demand": 90.0, "priority_tier": 3, "category": "INDUSTRIAL"},
        ],
        "batteries": [{"id": "BESS_A", "substation": "SUB_A", "max_discharge_mw": 50.0, "soc_pct": 75.0},
                      {"id": "BESS_C", "substation": "SUB_C", "max_discharge_mw": 30.0, "soc_pct": 35.0}],
    }


DATA_DIR = Path(__file__).parent / "data"
SAMPLE_FILES = {"ieee14": "ieee14.m", "ieee30": "ieee30.m", "ieee118": "ieee118.m"}


def _ieee_template(n: int):
    return lambda: json.loads((DATA_DIR / f"ieee{n}.json").read_text())["spec"]


TEMPLATES = {"demo_corridor": _demo_template, "ring": _ring_template, "meshed_city": _meshed_template,
             "ieee14": _ieee_template(14), "ieee30": _ieee_template(30), "ieee118": _ieee_template(118)}


def sample_file(name: str) -> str:
    """Bundled public MATPOWER case files, for trying the importer."""
    if name not in SAMPLE_FILES:
        raise UnknownEntityError(f"No sample '{name}'", available=sorted(SAMPLE_FILES))
    return (DATA_DIR / SAMPLE_FILES[name]).read_text()


def list_templates() -> Dict[str, Any]:
    out = []
    for key, fn in TEMPLATES.items():
        spec = fn()
        out.append({"id": key, "name": spec["name"], "description": spec["description"],
                    "counts": {k: len(spec[k]) for k in ("generators", "buses", "substations", "lines", "loads", "batteries")}})
    return {"templates": out}


def get_template(template_id: str) -> Dict[str, Any]:
    if template_id not in TEMPLATES:
        raise UnknownEntityError(f"No template '{template_id}'", available=sorted(TEMPLATES))
    return TEMPLATES[template_id]()


# --------------------------------------------------------------------------- #
# Test-case simulation                                                        #
# --------------------------------------------------------------------------- #


def _case_inputs(case: Dict[str, Any], snapshot: Dict[str, Any], i: int) -> Tuple[str, List[Tuple[str, str]], Dict[str, Any]]:
    if not isinstance(case, dict):
        raise InvalidRequestError(f"cases[{i}] must be an object")
    unknown = sorted(set(case) - {"name", "type", "trip", "demand_scale", "bess_injection_mw", "deenergize"})
    if unknown:
        raise InvalidRequestError(f"cases[{i}] has unknown fields {unknown}")
    kind = case.get("type", "scenario")
    if kind not in ("scenario", "n1"):
        raise InvalidRequestError(f"cases[{i}].type must be 'scenario' or 'n1'")
    trips = []
    for t in case.get("trip") or []:
        if not isinstance(t, dict) or t.get("circuit", "UPPER_PRIMARY") not in topo.CIRCUITS:
            raise InvalidRequestError(f"cases[{i}].trip entries need span_id and circuit UPPER_PRIMARY|LOWER_BACKUP")
        trips.append((t.get("span_id"), t.get("circuit", "UPPER_PRIMARY")))
    overrides: Dict[str, Any] = {}
    if case.get("demand_scale") is not None:
        overrides["demand_scale"] = case["demand_scale"]
    if case.get("bess_injection_mw"):
        overrides["bess_injection_mw"] = case["bess_injection_mw"]
    if case.get("deenergize"):
        overrides["deenergized_consumers"] = case["deenergize"]
    return kind, trips, overrides


def _sheddable(snapshot: Dict[str, Any], already_off: List[str]) -> List[Dict[str, Any]]:
    consumers = snapshot["consumers"]
    out = []
    for c in consumers:
        if c.get("priority_tier") != 3 or not c.get("energized") or c["id"] in already_off or not c.get("breaker_id"):
            continue
        shares_protected = any(o["id"] != c["id"] and o.get("priority_tier") != 3 and
                               (o.get("breaker_id") == c["breaker_id"] or
                                (c.get("feeder_id") and o.get("feeder_id") == c.get("feeder_id"))) for o in consumers)
        if not shares_protected:
            out.append(c)
    return sorted(out, key=lambda c: (-float(c["mw_demand"]), c["id"]))


def suggest_mitigation(snapshot: Dict[str, Any], trips: List[Tuple[str, str]], overrides: Dict[str, Any],
                       threshold_pct: float, min_soc_pct: float = 40.0) -> Dict[str, Any]:
    """Smallest safe fix found greedily: batteries first, then Tier-3 shedding, re-solving after each step."""
    base = project_loading(snapshot, trips, threshold_pct, overrides)
    if base["outcome"] == "WITHIN_LIMITS":
        return {"verdict": "NOT_NEEDED", "plan": {"bess_dispatch": {}, "trip_breakers": []}, "final_outcome": base["outcome"]}
    plan_bess: Dict[str, float] = dict(overrides.get("bess_injection_mw") or {})
    off: List[str] = list(overrides.get("deenergized_consumers") or [])
    breakers: List[str] = []

    def project():
        ov = {k: v for k, v in overrides.items() if k not in ("bess_injection_mw", "deenergized_consumers")}
        if plan_bess:
            ov["bess_injection_mw"] = plan_bess
        if off:
            ov["deenergized_consumers"] = off
        return project_loading(snapshot, trips, threshold_pct, ov)

    current = base
    threshold = threshold_pct / 100.0
    batteries = {b["id"]: b for b in snapshot["bess"]
                 if b.get("soc_pct") is not None and b["soc_pct"] > min_soc_pct and b.get("status") != "OFFLINE"}
    loads = {c["id"]: c for c in _sheddable(snapshot, off)}

    def excess(projection: Dict[str, Any]) -> float:
        """Total MW above limits; the search minimises this (new overloads count too)."""
        total = sum(max(0.0, c["flow_mw"] - threshold * c["limit_mw"]) for c in projection["circuits"])
        total += sum(max(0.0, s["net_demand_mw"] - threshold * s["capacity_mw"]) for s in projection["substations"] if s["energized"])
        total += sum(max(0.0, g["output_mw"] - g["capacity_mw"]) for g in projection.get("generators", []) if g.get("capacity_mw"))
        return total

    # Each round, try every remaining battery and Tier-3 load, re-solve, and keep the one that removes the most overload.
    for _ in range(len(batteries) + len(loads)):
        if current["outcome"] != "OVERLOAD_PREDICTED":
            break
        now = excess(current)
        best = None
        for bid, b in batteries.items():
            used = float(plan_bess.get(bid, b.get("active_injection_mw") or 0.0))
            if float(b["max_discharge_mw"]) - used <= 0:
                continue
            saved = plan_bess.get(bid)
            plan_bess[bid] = float(b["max_discharge_mw"])
            trial = project()
            gain = now - excess(trial)
            if saved is None:
                del plan_bess[bid]
            else:
                plan_bess[bid] = saved
            if gain > 1e-6 and (best is None or gain > best[0]):
                best = (gain, "bess", bid, trial)
        for cid in loads:
            if cid in off:
                continue
            off.append(cid)
            trial = project()
            off.pop()
            gain = now - excess(trial)
            if gain > 1e-6 and (best is None or gain > best[0]):
                best = (gain, "load", cid, trial)
        if best is None:
            break
        _, kind, ident, trial = best
        if kind == "bess":
            plan_bess[ident] = float(batteries[ident]["max_discharge_mw"])
        else:
            off.append(ident)
            breakers.append(loads[ident]["breaker_id"])
        current = trial
    if current["outcome"] == "WITHIN_LIMITS":
        # Undo any shedding that turned out to be unnecessary, smallest loads last to stay minimal.
        mw = {c["id"]: float(c["mw_demand"]) for c in snapshot["consumers"]}
        for cid in sorted([c for c in off if c not in (overrides.get("deenergized_consumers") or [])], key=lambda c: -mw[c]):
            off.remove(cid)
            trial = project()
            if trial["outcome"] == "WITHIN_LIMITS":
                current = trial
                breakers = [b for b in breakers if b != next(c["breaker_id"] for c in snapshot["consumers"] if c["id"] == cid)]
            else:
                off.append(cid)
    if current["outcome"] == "WITHIN_LIMITS":
        # Shrink each battery setpoint to the smallest value that still keeps limits (0.5 MW resolution).
        for bid in sorted(plan_bess):
            floor = float(next((b.get("active_injection_mw") or 0.0) for b in snapshot["bess"] if b["id"] == bid))
            lo, hi = floor, plan_bess[bid]
            ceiling = hi
            while hi - lo > 0.5:
                mid = (lo + hi) / 2
                plan_bess[bid] = mid
                if project()["outcome"] == "WITHIN_LIMITS":
                    hi = mid
                else:
                    lo = mid
            # Round up: rounding down could land below the smallest setpoint proven safe.
            plan_bess[bid] = min(math.ceil(hi * 10) / 10, ceiling)
            if plan_bess[bid] <= floor:
                del plan_bess[bid]
        current = project()
    tier1_ok = all(
        c["id"] not in off and c.get("energized") and
        next((s["projected_status"] for s in current["substations"] if s["id"] == c["substation_id"]), "ISLANDED") != "ISLANDED"
        for c in snapshot["consumers"] if c.get("priority_tier") == 1)
    saved = current["outcome"] == "WITHIN_LIMITS" and tier1_ok
    return {
        "verdict": "SAVED" if saved else "NOT_SAVEABLE",
        "reason": None if saved else ("islanding cannot be fixed by battery or shedding" if current["outcome"] == "ISLANDING_PREDICTED"
                                      else "safe battery support and Tier-3 shedding are not enough (would need generator redispatch or new capacity)"),
        "plan": {"bess_dispatch": plan_bess, "trip_breakers": breakers, "shed_loads": off},
        "final_outcome": current["outcome"],
        "remaining_overloads": [o["element"] for o in current["overloads"]],
    }


def _case_result(name: str, projection: Dict[str, Any], snapshot: Dict[str, Any], mitigation: Dict[str, Any]) -> Dict[str, Any]:
    sub_status = {s["id"]: s["projected_status"] for s in projection["substations"]}
    off = set(projection.get("overrides", {}).get("deenergized_consumers") or [])
    tier1 = [{"id": c["id"], "supplied": c["id"] not in off and bool(c.get("energized")) and sub_status.get(c["substation_id"]) != "ISLANDED"}
             for c in snapshot["consumers"] if c.get("priority_tier") == 1]
    worst = sorted(projection["circuits"], key=lambda c: -c["loading_pct"])[:5]
    return {
        "name": name,
        "type": "scenario",
        "outcome": projection["outcome"],
        "required_relief_mw": projection["required_relief_mw"],
        "overloads": [{k: o[k] for k in ("element", "kind", "loading_pct", "relief_mw")} for o in projection["overloads"]],
        "islanded_substations": projection["islanded_substations"],
        "tier1": tier1,
        "worst_circuits": [{k: c[k] for k in ("span_id", "circuit", "from", "to", "flow_mw", "limit_mw", "loading_pct")} for c in worst],
        "substations": [{k: s[k] for k in ("id", "projected_status", "net_demand_mw", "capacity_mw", "loading_pct")} for s in projection["substations"]],
        "generators": projection.get("generators", []),
        "mitigation": mitigation,
    }


def simulate(owner: str, network_id: str, cases: List[Dict[str, Any]], threshold_pct: float = 95.0,
             base: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Run test cases against a stored network. Never writes to the network graph."""
    started = time.perf_counter()
    if not isinstance(cases, list) or not cases:
        raise InvalidRequestError("cases must be a non-empty list")
    if len(cases) > MAX_CASES:
        raise InvalidRequestError(f"at most {MAX_CASES} cases per run")
    client = owned_client(owner, network_id, base)
    snapshot, snap_q = read_grid_snapshot(client)
    results = []
    for i, case in enumerate(cases):
        kind, trips, overrides = _case_inputs(case, snapshot, i)
        name = str(case.get("name") or f"case {i + 1}")[:80]
        if kind == "n1":
            scan = scan_n1(snapshot, threshold_pct)
            results.append({"name": name, "type": "n1", "n1_secure": scan["n1_secure"], "base_outcome": scan["base_outcome"],
                            "contingency_count": scan["contingency_count"], "critical_outages": scan["critical_outages"],
                            "contingencies": scan["contingencies"]})
            continue
        projection = project_loading(snapshot, trips, threshold_pct, overrides)
        mitigation = _from_memory(client, snapshot, trips, overrides, threshold_pct, projection) if projection["outcome"] == "OVERLOAD_PREDICTED" else None
        if mitigation is None:
            mitigation = suggest_mitigation(snapshot, trips, overrides, threshold_pct)
        results.append(_case_result(name, projection, snapshot, mitigation))
    return {
        "network_id": network_id,
        "state_version": snapshot["state_version"],
        "threshold_pct": threshold_pct,
        "model": "dc-pf/1",
        "cases": results,
        "summary": {
            "cases": len(results),
            "within_limits": sum(1 for r in results if r.get("outcome") == "WITHIN_LIMITS" or r.get("n1_secure")),
            "saved_by_mitigation": sum(1 for r in results if r.get("mitigation", {}).get("verdict") == "SAVED"),
            "not_saveable": sum(1 for r in results if r.get("mitigation", {}).get("verdict") == "NOT_SAVEABLE"),
            "solved_from_memory": sum(1 for r in results if r.get("mitigation", {}).get("from_memory")),
        },
        "evidence": [snap_q.evidence()],
        "timing": {"engine_total_ms": round((time.perf_counter() - started) * 1000.0, 3), "graph_server_ms": round(snap_q.server_ms, 4)},
    }


def snapshot_from_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The graph snapshot a stored network would produce, built in memory (for offline checks and tests)."""
    normalized, _ = validate_spec(spec)
    rows = _rows(normalized)
    circuits = [{"span_id": r["props"]["id"], "circuit": r["props"]["circuit"], "rel_type": r["rel"], "src": r["src"], "dst": r["dst"],
                 **{k: v for k, v in r["props"].items() if k not in ("id", "circuit")}} for r in rows["_circuits"]]
    plants = {g["id"]: g for g in normalized["generators"]}
    return {
        "circuits": circuits,
        "feeds": [{"src": f["src"], "dst": f["dst"], "output_mw": plants[f["src"]]["output_mw"],
                   "capacity_mw": plants[f["src"]]["capacity_mw"]} for f in rows["_feeds"]],
        "substations": [{"id": s["id"], "capacity_mw": s["capacity_mw"], "status": "ONLINE"} for s in normalized["substations"]],
        "consumers": [{"id": c["id"], "substation_id": c["substation"], "feeder_id": c["feeder_id"], "name": c["name"],
                       "category": c["category"], "priority_tier": c["priority_tier"], "mw_demand": c["mw_demand"],
                       "breaker_id": c["breaker_id"], "energized": True} for c in normalized["loads"]],
        "bess": [{"id": b["id"], "substation_id": b["substation"], "soc_pct": b["soc_pct"], "max_discharge_mw": b["max_discharge_mw"],
                  "active_injection_mw": 0.0, "status": "STANDBY"} for b in normalized["batteries"]],
        "state_version": 0,
    }



# --------------------------------------------------------------------------- #
# Learning on user networks: memory-first mitigation and rehearsal training   #
# --------------------------------------------------------------------------- #


def _origin_for(trips: List[Tuple[str, str]], projection: Dict[str, Any]) -> Optional[str]:
    if trips:
        return trips[0][0]
    over = [o["element"] for o in projection["overloads"] if o["kind"] == "SUBSTATION"]
    subs = [s["id"] for s in projection["substations"]]
    return (over or subs or [None])[0]


def _from_memory(client: GraphClient, snapshot: Dict[str, Any], trips: List[Tuple[str, str]], overrides: Dict[str, Any],
                 threshold_pct: float, projection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Try learned plans (same or similar faults) before searching; None if memory has nothing that works."""
    from . import memory
    origin = _origin_for(trips, projection)
    if origin is None:
        return None
    try:
        learned = memory.suggest(origin, [{"span_id": t, "circuit": c} for t, c in trips], overrides.get("demand_scale"), client=client)
    except GraphError:
        return None
    consumers = {c["breaker_id"]: c for c in snapshot["consumers"]}
    for sg in learned["suggestions"]:
        plan = sg["plan"]
        if any(b not in consumers or consumers[b].get("priority_tier") != 3 for b in plan.get("trip_breakers") or []):
            continue
        bess = {b["id"]: b for b in snapshot["bess"]}
        if any(bid not in bess or mw > float(bess[bid].get("max_discharge_mw") or 0) for bid, mw in (plan.get("bess_dispatch") or {}).items()):
            continue
        off = sorted(set(overrides.get("deenergized_consumers") or []) | {consumers[b]["id"] for b in plan.get("trip_breakers") or []})
        ov = {**overrides, "deenergized_consumers": off, "bess_injection_mw": {**(overrides.get("bess_injection_mw") or {}), **plan.get("bess_dispatch", {})}}
        trial = project_loading(snapshot, trips, threshold_pct, ov)
        if trial["outcome"] == "WITHIN_LIMITS":
            return {"verdict": "SAVED", "from_memory": True, "playbook": sg["signature"], "why": sg["why"], "score": sg["score"],
                    "plan": {"bess_dispatch": plan.get("bess_dispatch", {}), "trip_breakers": plan.get("trip_breakers", []), "shed_loads": off},
                    "final_outcome": trial["outcome"], "remaining_overloads": []}
    return None


def rehearse(owner: str, network_id: str, max_cases: int = 40, demand_scales: Tuple[float, ...] = (1.1, 1.2),
             threshold_pct: float = 95.0, base: Optional[GraphClient] = None) -> Dict[str, Any]:
    """'Train' a network: rehearse its critical outages and demand surges, and store what worked as playbooks.

    Cases are the N-1-critical single-line outages plus demand surges. Each is solved by the
    simulator (memory first, then search); results are written to the network's own graph
    memory with source REHEARSAL, so later simulations and the agent start with experience.
    """
    from . import memory
    started = time.perf_counter()
    if isinstance(max_cases, bool) or not isinstance(max_cases, int) or not 1 <= max_cases <= 200:
        raise InvalidRequestError("max_cases must be between 1 and 200")
    client = owned_client(owner, network_id, base)
    snapshot, _ = read_grid_snapshot(client)
    scan = scan_n1(snapshot, threshold_pct)
    cases: List[Dict[str, Any]] = [{"trips": [(c["span_id"], c["circuit"])], "demand_scale": None}
                                   for c in scan["contingencies"] if not c["secure"]]
    cases += [{"trips": [], "demand_scale": d} for d in demand_scales]
    cases = cases[:max_cases]
    learned = saved = failed = skipped = reused = 0
    log = []
    for case in cases:
        overrides = {"demand_scale": case["demand_scale"]} if case["demand_scale"] else {}
        t0 = time.perf_counter()
        projection = project_loading(snapshot, case["trips"], threshold_pct, overrides)
        if projection["outcome"] == "WITHIN_LIMITS":
            skipped += 1
            continue
        if projection["outcome"] == "ISLANDING_PREDICTED":
            mitigation = {"verdict": "NOT_SAVEABLE", "plan": {}, "reason": "islanding"}
        else:
            mitigation = _from_memory(client, snapshot, case["trips"], overrides, threshold_pct, projection) \
                or suggest_mitigation(snapshot, case["trips"], overrides, threshold_pct)
        origin = _origin_for(case["trips"], projection)
        plan = {k: mitigation.get("plan", {}).get(k) or ([] if k == "trip_breakers" else {}) for k in ("trip_breakers", "bess_dispatch")}
        shed = sum(c["mw_demand"] for c in snapshot["consumers"] if c.get("breaker_id") in plan["trip_breakers"])
        status = "MITIGATED" if mitigation["verdict"] == "SAVED" else "FAILED"
        stored = memory.record_incident({
            "source": "REHEARSAL", "scenario": "REHEARSAL", "origin_id": origin,
            "trips": [{"span_id": t, "circuit": c} for t, c in case["trips"]], "demand_scale": case["demand_scale"],
            "overloads": [o["element"] for o in projection["overloads"]], "required_relief_mw": projection["required_relief_mw"],
            "status": status, "final_outcome": mitigation.get("final_outcome") or "", "plan": plan if status == "MITIGATED" else None,
            "breaker_targets": {c["breaker_id"]: c["id"] for c in snapshot["consumers"] if c.get("breaker_id")},
            "mitigation_ms": round((time.perf_counter() - t0) * 1000.0, 1), "steps": 1 if mitigation.get("from_memory") else 2,
            "shed_mw": round(shed, 3), "downstream": [s["id"] for s in projection["substations"] if s["overload"] or s["overloaded_supply_circuits"]],
            "reused_playbook": mitigation.get("playbook"),
        }, client)
        saved += status == "MITIGATED"
        failed += status == "FAILED"
        reused += bool(mitigation.get("from_memory"))
        learned += bool(stored["playbook"] and stored["playbook"]["success_count"] == 1 and status == "MITIGATED" and not mitigation.get("from_memory"))
        log.append({"case": stored["signature"], "verdict": mitigation["verdict"], "from_memory": bool(mitigation.get("from_memory")),
                    "playbook": mitigation.get("playbook"), "incident": stored["incident_id"]})
    curve = memory.learning_curve(client)
    return {"network_id": network_id, "cases": len(cases), "skipped_within_limits": skipped, "saved": saved, "not_saveable": failed,
            "solved_from_memory": reused, "new_playbooks": learned, "log": log, "memory": curve["summary"],
            "timing": {"engine_total_ms": round((time.perf_counter() - started) * 1000.0, 1)}}
