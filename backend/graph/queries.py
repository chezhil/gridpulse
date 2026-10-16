"""Deterministic GridPulse graph queries.

Public functions return plain dicts that are safe to serialise. Each one
includes ``evidence`` (the executed Cypher, parameters, returned rows, timings
and an evidence hash) so downstream decisions can cite the exact graph
result they used.

Connectivity (what is downstream of a fault) is computed by Cypher traversal.
Overload claims come only from :mod:`graph.thermal`, applied to a snapshot
read in a single query.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import topology as topo
from .client import (
    GraphClient,
    GraphNotSeededError,
    InvalidRequestError,
    QueryOutcome,
    UnknownEntityError,
    get_client,
)
from .contingency import scan_n1
from .thermal import project_loading

DEFAULT_MAX_HOPS = 10  # HYDRO_ALPINE_01 -> SUB_METRO is the longest corridor path (10 hops)
MAX_HOPS_LIMIT = 12
DEFAULT_THRESHOLD_PCT = 95.0
DEFAULT_MIN_SOC_PCT = 40.0

_ID_RE = re.compile(r"^[A-Z0-9_]{1,40}$")
_NODE_PREDICATE = " OR ".join(f"n:{label}" for label in topo.NODE_LABELS)
_EDGE_TYPES_LIST = "[" + ", ".join(f"'{t}'" for t in topo.EDGE_TYPES) + "]"
_TRANSMISSION = "|".join(topo.TRANSMISSION_EDGE_TYPES)


# --------------------------------------------------------------------------- #
# Cypher                                                                      #
# --------------------------------------------------------------------------- #

SNAPSHOT_CYPHER = """\
MATCH (a)-[r:SPAN|TERMINATES_AT]->(b)
WITH collect({span_id: r.id, circuit: r.circuit, rel_type: type(r), src: a.id, dst: b.id,
              impedance_pu: r.impedance_pu, limit_mw: r.limit_mw, flow_mw: r.flow_mw, status: r.status}) AS circuits
OPTIONAL MATCH (h:HydroPlant)-[:FEEDS]->(t)
WITH circuits, collect({src: h.id, dst: t.id, output_mw: h.output_mw, capacity_mw: h.capacity_mw}) AS feeds
OPTIONAL MATCH (s:Substation)
WITH circuits, feeds, collect({id: s.id, capacity_mw: s.capacity_mw, current_load_mw: s.current_load_mw,
                               status: s.status}) AS substations
OPTIONAL MATCH (s2:Substation)-[d:DISTRIBUTES_TO]->(c:Consumer)
WITH circuits, feeds, substations,
     collect({id: c.id, substation_id: s2.id, feeder_id: d.feeder_id, name: c.name, category: c.category,
              priority_tier: c.priority_tier, mw_demand: c.mw_demand, breaker_id: c.breaker_id,
              energized: c.energized}) AS consumers
OPTIONAL MATCH (bb:BESS)-[k:CONNECTED]->(s3:Substation)
WITH circuits, feeds, substations, consumers,
     collect({id: bb.id, substation_id: s3.id, soc_pct: bb.soc_pct, max_discharge_mw: bb.max_discharge_mw,
              active_injection_mw: bb.active_injection_mw, status: bb.status}) AS bess
OPTIONAL MATCH (m:GridMeta {id: $meta_id})
RETURN circuits, feeds, substations, consumers, bess, m.state_version AS state_version"""

RESOLVE_ORIGIN_CYPHER = f"""\
OPTIONAL MATCH (n) WHERE n.id = $origin_id AND ({_NODE_PREDICATE})
WITH collect({{id: n.id, label: labels(n)[0]}}) AS nodes
OPTIONAL MATCH (a)-[r:SPAN|TERMINATES_AT]->(b) WHERE r.id = $origin_id
RETURN nodes, collect({{circuit: r.circuit, status: r.status, rel_type: type(r), src: a.id, dst: b.id,
                        dst_label: labels(b)[0]}}) AS circuits"""

# The hop bound cannot be a Cypher parameter; it is a validated int formatted in.
REACHABILITY_CYPHER = """\
MATCH (o) WHERE o.id = $start_id AND (o:HydroPlant OR o:Tower OR o:Substation)
MATCH p = (o)-[:{edges}*1..{bound}]->(n)
WITH n, length(p) AS hops, [x IN nodes(p) | x.id] AS path
ORDER BY hops
WITH n, min(hops) AS hops, collect(path)[0] AS path
RETURN n.id AS id, labels(n)[0] AS label, hops, path, n.status AS status
ORDER BY hops, id"""

ATTACHED_ASSETS_CYPHER = """\
MATCH (s:Substation) WHERE s.id IN $substation_ids
OPTIONAL MATCH (s)-[d:DISTRIBUTES_TO]->(c:Consumer)
WITH s, collect({id: c.id, name: c.name, category: c.category, priority_tier: c.priority_tier,
                 mw_demand: c.mw_demand, breaker_id: c.breaker_id, energized: c.energized,
                 feeder_id: d.feeder_id}) AS consumers
OPTIONAL MATCH (b:BESS)-[k:CONNECTED]->(s)
RETURN s.id AS substation_id, s.capacity_mw AS capacity_mw, s.current_load_mw AS current_load_mw,
       s.status AS status, consumers,
       collect({id: b.id, soc_pct: b.soc_pct, max_discharge_mw: b.max_discharge_mw,
                active_injection_mw: b.active_injection_mw, status: b.status,
                inverter_id: k.inverter_id}) AS bess
ORDER BY substation_id"""

TOPOLOGY_NODES_CYPHER = f"""\
MATCH (n) WHERE {_NODE_PREDICATE}
RETURN n.id AS id, labels(n)[0] AS label, properties(n) AS properties
ORDER BY label, id"""

TOPOLOGY_EDGES_CYPHER = f"""\
MATCH (a)-[r]->(b) WHERE type(r) IN {_EDGE_TYPES_LIST}
RETURN type(r) AS type, a.id AS source, b.id AS target, properties(r) AS properties
ORDER BY type, source, target"""

META_CYPHER = "OPTIONAL MATCH (m:GridMeta {id: $meta_id}) RETURN properties(m) AS meta"

MITIGATION_CYPHER = """\
MATCH (s:Substation {id: $substation_id})
OPTIONAL MATCH (b:BESS)-[k:CONNECTED]->(s)
WITH s, collect({id: b.id, soc_pct: b.soc_pct, max_discharge_mw: b.max_discharge_mw,
                 active_injection_mw: b.active_injection_mw, status: b.status,
                 inverter_id: k.inverter_id, connection_max_mw: k.max_mw}) AS bess
OPTIONAL MATCH (s)-[d:DISTRIBUTES_TO]->(c:Consumer)
RETURN s.id AS substation_id, s.status AS substation_status, bess,
       collect({id: c.id, name: c.name, category: c.category, priority_tier: c.priority_tier,
                mw_demand: c.mw_demand, breaker_id: c.breaker_id, energized: c.energized,
                feeder_id: d.feeder_id}) AS consumers"""

BREAKER_CYPHER = """\
OPTIONAL MATCH (:Substation)-[d:DISTRIBUTES_TO]->(:Consumer {breaker_id: $breaker_id})
WITH collect(DISTINCT d.feeder_id) AS feeders
MATCH (s:Substation)-[d2:DISTRIBUTES_TO]->(c:Consumer)
WHERE c.breaker_id = $breaker_id OR d2.feeder_id IN feeders
RETURN c.id AS id, c.name AS name, c.priority_tier AS priority_tier, c.mw_demand AS mw_demand,
       c.energized AS energized, c.breaker_id AS breaker_id, d2.feeder_id AS feeder_id,
       s.id AS substation_id, c.breaker_id = $breaker_id AS on_breaker
ORDER BY id"""

TIER1_CYPHER = """\
MATCH (s:Substation)-[d:DISTRIBUTES_TO]->(h:Consumer)
WHERE h.priority_tier = 1 AND ($substation_id IS NULL OR s.id = $substation_id)
RETURN h.id AS id, h.name AS name, h.energized AS energized, h.breaker_id AS breaker_id,
       d.feeder_id AS feeder_id, s.id AS substation_id, s.status AS substation_status
ORDER BY id"""


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def validate_id(value: Any, field: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidRequestError(f"{field} must match {_ID_RE.pattern}", field=field, value=value)
    return value


def _validate_hops(max_hops: Any) -> int:
    if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 1 <= max_hops <= MAX_HOPS_LIMIT:
        raise InvalidRequestError(f"max_hops must be an integer between 1 and {MAX_HOPS_LIMIT}", value=max_hops)
    return max_hops


def _validate_number(value: Any, field: str, minimum: float = 0.0, maximum: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidRequestError(f"{field} must be a finite number", field=field, value=value)
    if not minimum <= value <= maximum:
        raise InvalidRequestError(f"{field} must be between {minimum} and {maximum}", field=field, value=value)
    return float(value)


def _compact(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop the all-null maps that OPTIONAL MATCH + collect produce."""
    return [i for i in items if i.get("id") is not None or i.get("span_id") is not None or i.get("circuit") is not None]


def _timing(outcomes: List[QueryOutcome], started: float) -> Dict[str, float]:
    return {
        "graph_server_ms": round(sum(o.server_ms for o in outcomes), 4),
        "graph_roundtrip_ms": round(sum(o.roundtrip_ms for o in outcomes), 4),
        "engine_total_ms": round((time.perf_counter() - started) * 1000.0, 4),
        "query_count": len(outcomes),
    }


def read_grid_snapshot(client: Optional[GraphClient] = None) -> Tuple[Dict[str, Any], QueryOutcome]:
    """Read the whole dynamic state in one query (one consistent FalkorDB snapshot)."""
    client = client or get_client()
    outcome = client.query("grid_snapshot", SNAPSHOT_CYPHER, {"meta_id": topo.META_ID})
    row = outcome.rows[0] if outcome.rows else {}
    snapshot = {
        "circuits": [c for c in row.get("circuits", []) if c.get("span_id") is not None],
        "feeds": [f for f in row.get("feeds", []) if f.get("src") is not None],
        "substations": _compact(row.get("substations", [])),
        "consumers": _compact(row.get("consumers", [])),
        "bess": _compact(row.get("bess", [])),
        "state_version": row.get("state_version"),
    }
    if not snapshot["circuits"] and not snapshot["substations"]:
        raise GraphNotSeededError(f"Graph '{client.graph_name}' has no GridPulse topology; run the seeder")
    return snapshot, outcome


# --------------------------------------------------------------------------- #
# Topology                                                                    #
# --------------------------------------------------------------------------- #


def get_topology(client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Full node/edge listing for ``GET /api/graph/topology``."""
    client = client or get_client()
    started = time.perf_counter()
    nodes_q = client.query("topology_nodes", TOPOLOGY_NODES_CYPHER)
    edges_q = client.query("topology_edges", TOPOLOGY_EDGES_CYPHER)
    meta_q = client.query("topology_meta", META_CYPHER, {"meta_id": topo.META_ID})
    if not nodes_q.rows:
        raise GraphNotSeededError(f"Graph '{client.graph_name}' has no GridPulse topology; run the seeder")

    nodes = [{"id": r["id"], "label": r["label"], "properties": r["properties"]} for r in nodes_q.rows]
    edges = []
    spans: Dict[str, Dict[str, Any]] = {}
    for r in edges_q.rows:
        props = r["properties"]
        if r["type"] in ("SPAN", "TERMINATES_AT"):
            edge_id = f"{props.get('id')}:{props.get('circuit')}"
            span = spans.setdefault(props.get("id"), {"id": props.get("id"), "from": r["source"], "to": r["target"], "circuits": {}})
            span["circuits"][props.get("circuit")] = {
                k: props.get(k) for k in ("status", "flow_mw", "limit_mw", "impedance_pu", "sag_m")
            }
        else:
            edge_id = f"{r['type']}:{r['source']}->{r['target']}"
        edges.append({"id": edge_id, "type": r["type"], "source": r["source"], "target": r["target"], "properties": props})

    meta = (meta_q.rows[0]["meta"] if meta_q.rows else None) or {}
    outcomes = [nodes_q, edges_q, meta_q]
    return {
        "graph": client.graph_name,
        "schema_version": meta.get("schema_version"),
        "state_version": meta.get("state_version"),
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
        "spans": [spans[k] for k in sorted(spans)],
        "evidence": [o.evidence() for o in outcomes],
        "timing": _timing(outcomes, started),
    }


# --------------------------------------------------------------------------- #
# Cascade tracing                                                             #
# --------------------------------------------------------------------------- #


def resolve_origin(origin_id: str, client: Optional[GraphClient] = None) -> Tuple[Dict[str, Any], QueryOutcome]:
    """Identify ``origin_id`` as a span (corridor segment) or a node."""
    client = client or get_client()
    validate_id(origin_id, "origin_id")
    outcome = client.query("resolve_origin", RESOLVE_ORIGIN_CYPHER, {"origin_id": origin_id})
    row = outcome.rows[0] if outcome.rows else {"nodes": [], "circuits": []}
    nodes = _compact(row["nodes"])
    circuits = [c for c in row["circuits"] if c.get("circuit") is not None]
    if circuits:
        endpoints = {(c["src"], c["dst"]) for c in circuits}
        src, dst = sorted(endpoints)[0]
        origin = {
            "id": origin_id,
            "kind": "SPAN",
            "from": src,
            "to": dst,
            "start_node_id": dst,
            "start_label": next(c["dst_label"] for c in circuits if c["dst"] == dst),
            "circuits": sorted(
                ({k: c[k] for k in ("circuit", "status", "rel_type")} for c in circuits),
                key=lambda c: c["circuit"],
                reverse=True,
            ),
        }
        if len(endpoints) > 1:
            origin["warning"] = "span circuits have inconsistent endpoints"
    elif nodes:
        label = nodes[0]["label"]
        origin = {"id": origin_id, "kind": "NODE", "label": label, "start_node_id": origin_id, "start_label": label}
    else:
        raise UnknownEntityError(f"No node or span with id '{origin_id}'", origin_id=origin_id)
    return origin, outcome


def trace_reachability(start_id: str, max_hops: int, client: Optional[GraphClient] = None) -> Tuple[Dict[str, Any], QueryOutcome]:
    """Downstream nodes within ``max_hops`` along FEEDS/SPAN/TERMINATES_AT, ignoring breaker state.

    Queries one hop further than requested so the result can say whether
    the bound cut anything off.
    """
    client = client or get_client()
    validate_id(start_id, "start_id")
    _validate_hops(max_hops)
    cypher = REACHABILITY_CYPHER.format(edges=_TRANSMISSION, bound=max_hops + 1)
    outcome = client.query("trace_reachability", cypher, {"start_id": start_id})
    nodes = [r for r in outcome.rows if r["hops"] <= max_hops]
    beyond = sorted(r["id"] for r in outcome.rows if r["hops"] > max_hops)
    return {"max_hops": max_hops, "nodes": nodes, "beyond_max_hops": beyond}, outcome


def attached_assets(substation_ids: List[str], client: Optional[GraphClient] = None) -> Tuple[List[Dict[str, Any]], QueryOutcome]:
    client = client or get_client()
    outcome = client.query("attached_assets", ATTACHED_ASSETS_CYPHER, {"substation_ids": list(substation_ids)})
    subs = []
    for r in outcome.rows:
        subs.append({**r, "consumers": _compact(r["consumers"]), "bess": _compact(r["bess"])})
    return subs, outcome


def _parse_trips(assume_tripped: Optional[Iterable[Any]]) -> List[Tuple[str, str]]:
    trips = []
    for item in assume_tripped or []:
        if isinstance(item, dict):
            span_id, circuit = item.get("span_id"), item.get("circuit")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            span_id, circuit = item
        else:
            raise InvalidRequestError("assume_tripped items must be {span_id, circuit}", value=item)
        validate_id(span_id, "assume_tripped.span_id")
        if circuit not in topo.CIRCUITS:
            raise InvalidRequestError(f"circuit must be one of {list(topo.CIRCUITS)}", value=circuit)
        trips.append((span_id, circuit))
    return trips


def trace_cascade(
    origin_id: str,
    max_hops: int = DEFAULT_MAX_HOPS,
    assume_tripped: Optional[Iterable[Any]] = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    client: Optional[GraphClient] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Blast-radius trace for ``POST /api/graph/trace-cascade``.

    Combines (a) Cypher reachability downstream of ``origin_id`` with (b) the
    ``dc-pf/1`` loading projection over the current graph state plus any
    ``assume_tripped`` circuits and what-if ``overrides``. Connectivity and overload are reported
    separately; a node being downstream is not by itself an overload claim.
    """
    client = client or get_client()
    started = time.perf_counter()
    _validate_hops(max_hops)
    threshold_pct = _validate_number(threshold_pct, "threshold_pct", 1.0, 150.0)
    trips = _parse_trips(assume_tripped)

    origin, origin_q = resolve_origin(origin_id, client)
    reach, reach_q = trace_reachability(origin["start_node_id"], max_hops, client)
    snapshot, snap_q = read_grid_snapshot(client)
    projection = project_loading(snapshot, trips, threshold_pct, overrides)

    start = origin["start_node_id"]
    nodes = [{"id": start, "label": origin["start_label"], "hops": 0, "path": [start], "status": None}]
    # In a directed loop the start node comes back at a later hop; it is already listed at hop 0.
    nodes += [n for n in reach["nodes"] if n["id"] != start]

    downstream_sub_ids = [n["id"] for n in nodes if n["label"] == "Substation"]
    outcomes = [origin_q, reach_q, snap_q]
    subs: List[Dict[str, Any]] = []
    if downstream_sub_ids:
        subs, assets_q = attached_assets(downstream_sub_ids, client)
        outcomes.append(assets_q)

    sub_projection = {s["id"]: s for s in projection["substations"]}
    sub_hops = {n["id"]: n["hops"] for n in nodes if n["label"] == "Substation"}
    impact_subs, impact_consumers, impact_bess = [], [], []
    for s in subs:
        proj = sub_projection.get(s["substation_id"], {})
        risk = {"ISLANDED": "ISLANDED", "OVERLOAD_RISK": "OVERLOAD_RISK"}.get(proj.get("projected_status"), "NONE")
        hops = sub_hops[s["substation_id"]]
        impact_subs.append(
            {
                "id": s["substation_id"],
                "hops": hops,
                "stored_status": s["status"],
                "projected_status": proj.get("projected_status"),
                "loading_pct": proj.get("loading_pct"),
                "capacity_mw": s["capacity_mw"],
                "net_demand_mw": proj.get("net_demand_mw"),
                "overloaded_supply_circuits": proj.get("overloaded_supply_circuits", []),
            }
        )
        for c in s["consumers"]:
            impact_consumers.append({**c, "substation_id": s["substation_id"], "hops": hops + 1, "risk": risk})
        for b in s["bess"]:
            impact_bess.append({**b, "substation_id": s["substation_id"], "hops": hops + 1})

    tier1_at_risk = [c["id"] for c in impact_consumers if c.get("priority_tier") == 1 and c["risk"] != "NONE"]
    reach_outcome = "IMPACT_FOUND" if len(nodes) > 1 or impact_consumers else "NO_DOWNSTREAM_ASSETS"

    return {
        "origin": origin,
        "state_version": snapshot["state_version"],
        "outcome": reach_outcome,
        "summary": {
            "origin_id": origin_id,
            "downstream_node_count": len(nodes) - 1,
            "downstream_substations": downstream_sub_ids,
            "downstream_consumer_ids": [c["id"] for c in impact_consumers],
            "furthest_hop": max((n["hops"] for n in nodes), default=0),
            "projection_outcome": projection["outcome"],
            "required_relief_mw": projection["required_relief_mw"],
            "tier1_at_risk": tier1_at_risk,
        },
        "reachability": {"max_hops": max_hops, "nodes": nodes, "beyond_max_hops": reach["beyond_max_hops"]},
        "impact": {"substations": impact_subs, "consumers": impact_consumers, "bess": impact_bess},
        "projection": projection,
        "evidence": [o.evidence() for o in outcomes],
        "timing": _timing(outcomes, started),
    }


# --------------------------------------------------------------------------- #
# Mitigation discovery                                                        #
# --------------------------------------------------------------------------- #


def find_mitigation_assets(
    substation_id: str,
    required_mw: float,
    min_soc_pct: float = DEFAULT_MIN_SOC_PCT,
    client: Optional[GraphClient] = None,
) -> Dict[str, Any]:
    """BESS units connected to the substation and Tier-3 loads it distributes to.

    Every candidate is returned with the reasons it is or is not usable; this
    function discovers options and never decides an action.
    """
    client = client or get_client()
    started = time.perf_counter()
    validate_id(substation_id, "substation_id")
    required_mw = _validate_number(required_mw, "required_mw", 0.0, 10_000.0)
    min_soc_pct = _validate_number(min_soc_pct, "min_soc_pct", 0.0, 100.0)
    outcome = client.query("find_mitigation_assets", MITIGATION_CYPHER, {"substation_id": substation_id})
    if not outcome.rows:
        raise UnknownEntityError(f"No substation with id '{substation_id}'", substation_id=substation_id)
    row = outcome.rows[0]

    bess_ok, bess_no = [], []
    for b in _compact(row["bess"]):
        reasons = []
        for field in ("soc_pct", "max_discharge_mw", "active_injection_mw", "status"):
            if b.get(field) is None:
                reasons.append(f"MISSING_{field.upper()}")
        if not reasons:
            if b["status"] not in ("STANDBY", "DISCHARGING"):
                reasons.append(f"STATUS_{b['status']}")
            if b["soc_pct"] <= min_soc_pct:
                reasons.append("SOC_AT_OR_BELOW_MINIMUM")
        limit = b.get("max_discharge_mw")
        if limit is not None and b.get("connection_max_mw") is not None:
            limit = min(limit, b["connection_max_mw"])
        available = max((limit or 0.0) - (b.get("active_injection_mw") or 0.0), 0.0)
        if not reasons and available <= 0:
            reasons.append("NO_DISCHARGE_HEADROOM")
        entry = {**b, "available_mw": round(available, 3)}
        (bess_no if reasons else bess_ok).append({**entry, "reasons": reasons} if reasons else entry)

    consumers = _compact(row["consumers"])
    loads_ok, loads_no = [], []
    for c in consumers:
        reasons = []
        if c.get("priority_tier") != 3:
            reasons.append(f"PRIORITY_TIER_{c.get('priority_tier')}")
        if c.get("energized") is not True:
            reasons.append("NOT_ENERGIZED")
        if not c.get("breaker_id"):
            reasons.append("NO_BREAKER")
        if c.get("mw_demand") is None:
            reasons.append("MISSING_MW_DEMAND")
        shared = [
            o["id"]
            for o in consumers
            if o["id"] != c["id"]
            and o.get("priority_tier") != 3
            and ((c.get("breaker_id") and o.get("breaker_id") == c.get("breaker_id")) or (c.get("feeder_id") and o.get("feeder_id") == c.get("feeder_id")))
        ]
        if shared:
            reasons.append("SHARES_BREAKER_OR_FEEDER_WITH_PROTECTED_LOAD")
        if reasons:
            loads_no.append({**c, "reasons": reasons, "shared_with": shared})
        else:
            loads_ok.append(c)
    loads_ok.sort(key=lambda c: (-c["mw_demand"], c["id"]))

    bess_mw = sum(b["available_mw"] for b in bess_ok)
    shed_mw = sum(c["mw_demand"] for c in loads_ok)
    return {
        "substation_id": substation_id,
        "substation_status": row["substation_status"],
        "required_mw": required_mw,
        "min_soc_pct": min_soc_pct,
        "bess": {"eligible": bess_ok, "ineligible": bess_no},
        "sheddable_loads": {"eligible": loads_ok, "excluded": loads_no},
        "totals": {
            "bess_available_mw": round(bess_mw, 3),
            "sheddable_mw": round(shed_mw, 3),
            "combined_mw": round(bess_mw + shed_mw, 3),
        },
        "meets_requirement": bess_mw + shed_mw >= required_mw,
        "evidence": [outcome.evidence()],
        "timing": _timing([outcome], started),
    }


def check_breaker(breaker_id: str, client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Who a breaker (and its feeder) serves, and whether shedding it is allowed.

    ``trip_allowed`` is true only when the breaker exists, every consumer on
    its breaker or feeder is Tier 3, and at least one of them is energised.
    """
    client = client or get_client()
    started = time.perf_counter()
    validate_id(breaker_id, "breaker_id")
    outcome = client.query("check_breaker", BREAKER_CYPHER, {"breaker_id": breaker_id})
    served = [r for r in outcome.rows if r["on_breaker"]]
    reasons = []
    if not served:
        reasons.append("UNKNOWN_BREAKER")
    tiers = sorted({r["priority_tier"] for r in outcome.rows if r["priority_tier"] is not None})
    if 1 in tiers:
        reasons.append("SERVES_TIER_1")
    if 2 in tiers:
        reasons.append("SERVES_TIER_2")
    if any(r["priority_tier"] is None for r in outcome.rows):
        reasons.append("MISSING_PRIORITY_TIER")
    closed = any(r["energized"] is True for r in served)
    if served and not closed:
        reasons.append("ALREADY_OPEN")
    return {
        "breaker_id": breaker_id,
        "exists": bool(served),
        "currently_closed": closed,
        "served_consumers": served,
        "feeder_consumers": outcome.rows,
        "priority_tiers": tiers,
        "shed_mw": round(sum(r["mw_demand"] or 0.0 for r in served if r["energized"]), 3),
        "trip_allowed": not reasons,
        "reasons": reasons,
        "evidence": [outcome.evidence()],
        "timing": _timing([outcome], started),
    }


def verify_tier1_protection(substation_id: Optional[str] = None, client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Current energisation and projected risk for every Tier-1 consumer."""
    client = client or get_client()
    started = time.perf_counter()
    if substation_id is not None:
        validate_id(substation_id, "substation_id")
    tier1_q = client.query("verify_tier1_protection", TIER1_CYPHER, {"substation_id": substation_id})
    snapshot, snap_q = read_grid_snapshot(client)
    if substation_id is not None and substation_id not in {s["id"] for s in snapshot["substations"]}:
        raise UnknownEntityError(f"No substation with id '{substation_id}'", substation_id=substation_id)
    projection = project_loading(snapshot)
    proj_by_sub = {s["id"]: s for s in projection["substations"]}

    assets = []
    for r in tier1_q.rows:
        proj = proj_by_sub.get(r["substation_id"], {})
        supplied = bool(proj.get("energized"))
        protected = r["energized"] is True and supplied
        assets.append(
            {
                **r,
                "supply_path_energized": supplied,
                "projected_substation_status": proj.get("projected_status"),
                "protected": protected,
                "at_risk": proj.get("projected_status") in ("OVERLOAD_RISK", "ISLANDED"),
            }
        )
    if not assets:
        outcome = "NO_TIER1_ASSETS"
    elif not all(a["protected"] for a in assets):
        outcome = "TIER1_DEENERGIZED"
    elif any(a["at_risk"] for a in assets):
        outcome = "TIER1_AT_RISK"
    else:
        outcome = "ALL_PROTECTED"
    outcomes = [tier1_q, snap_q]
    return {
        "substation_id": substation_id,
        "state_version": snapshot["state_version"],
        "outcome": outcome,
        "all_protected": outcome == "ALL_PROTECTED",
        "assets": assets,
        "evidence": [o.evidence() for o in outcomes],
        "timing": _timing(outcomes, started),
    }


# --------------------------------------------------------------------------- #
# N-1 contingency scan                                                        #
# --------------------------------------------------------------------------- #

N1_WRITE_CYPHER = """\
UNWIND $rows AS row
MATCH ()-[r:SPAN|TERMINATES_AT]->() WHERE r.id = row.span_id AND r.circuit = row.circuit
SET r.n1_outcome = row.outcome, r.n1_worst_element = row.worst_element,
    r.n1_worst_loading_pct = row.worst_loading_pct, r.n1_state_version = $state_version
RETURN count(r) AS updated"""


def run_contingency_scan(
    threshold_pct: float = DEFAULT_THRESHOLD_PCT, write_back: bool = True, client: Optional[GraphClient] = None
) -> Dict[str, Any]:
    """Lose each energised circuit in turn, re-solve, and (optionally) tag circuits with the result.

    Tags are ``n1_outcome``, ``n1_worst_element``, ``n1_worst_loading_pct`` and
    ``n1_state_version`` on each circuit relationship; compare the version with
    the current ``state_version`` to know whether they are stale.
    """
    client = client or get_client()
    started = time.perf_counter()
    threshold_pct = _validate_number(threshold_pct, "threshold_pct", 1.0, 150.0)
    snapshot, snap_q = read_grid_snapshot(client)
    model_started = time.perf_counter()
    scan = scan_n1(snapshot, threshold_pct)
    model_ms = (time.perf_counter() - model_started) * 1000.0
    outcomes = [snap_q]
    if write_back:
        rows = [
            {k: c[k] for k in ("span_id", "circuit", "outcome", "worst_element", "worst_loading_pct")}
            for c in scan["contingencies"]
        ]
        outcomes.append(
            client.query(
                "n1_write_back", N1_WRITE_CYPHER, {"rows": rows, "state_version": snapshot["state_version"]}, read_only=False
            )
        )
    timing = _timing(outcomes, started)
    timing["model_ms"] = round(model_ms, 4)
    return {
        **scan,
        "state_version": snapshot["state_version"],
        "written_back": write_back,
        "evidence": [o.evidence() for o in outcomes],
        "timing": timing,
    }


# --------------------------------------------------------------------------- #
# Plan evaluation (what-if, no actuation)                                     #
# --------------------------------------------------------------------------- #


def evaluate_plan(
    plan: Dict[str, Any],
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    min_soc_pct: float = DEFAULT_MIN_SOC_PCT,
    client: Optional[GraphClient] = None,
) -> Dict[str, Any]:
    """Check a proposed mitigation against the same guards actuation uses, without actuating.

    ``plan`` = ``{"trip_breakers": ["BRK_IND_12"], "bess_dispatch": {"BESS_METRO_01": 45.0}}``.
    The verdict is ``APPROVED`` only if every breaker passes :func:`check_breaker`,
    every dispatch is within SOC and discharge limits, no Tier-1 consumer
    loses supply, and the projected state is within limits.
    """
    client = client or get_client()
    started = time.perf_counter()
    if not isinstance(plan, dict):
        raise InvalidRequestError("plan must be an object")
    unknown = sorted(set(plan) - {"trip_breakers", "bess_dispatch"})
    if unknown:
        raise InvalidRequestError("plan has unknown keys", unknown=unknown)
    breakers = plan.get("trip_breakers") or []
    dispatch = plan.get("bess_dispatch") or {}
    if not isinstance(breakers, list) or not isinstance(dispatch, dict):
        raise InvalidRequestError("trip_breakers must be a list and bess_dispatch an object")
    threshold_pct = _validate_number(threshold_pct, "threshold_pct", 1.0, 150.0)
    min_soc_pct = _validate_number(min_soc_pct, "min_soc_pct", 0.0, 100.0)

    reasons: List[str] = []
    evidence: List[Dict[str, Any]] = []
    deenergize: List[str] = []
    breaker_checks = []
    seen_breakers = set()
    for b in breakers:
        validate_id(b, "trip_breakers[]")
        if b in seen_breakers:
            reasons.append(f"{b}: DUPLICATE_BREAKER")
            continue
        seen_breakers.add(b)
        check = check_breaker(b, client)
        evidence += check["evidence"]
        breaker_checks.append({k: check[k] for k in ("breaker_id", "trip_allowed", "reasons", "shed_mw")})
        if not check["trip_allowed"]:
            reasons.append(f"{b}: {','.join(check['reasons'])}")
        deenergize += [c["id"] for c in check["served_consumers"]]

    snapshot, snap_q = read_grid_snapshot(client)
    evidence.append(snap_q.evidence())
    bess_by_id = {b["id"]: b for b in snapshot["bess"]}
    injections: Dict[str, float] = {}
    for bid, mw in dispatch.items():
        validate_id(bid, "bess_dispatch key")
        b = bess_by_id.get(bid)
        if b is None:
            reasons.append(f"{bid}: UNKNOWN_BESS")
            continue
        mw = _validate_number(mw, f"bess_dispatch.{bid}", 0.0, 10_000.0)
        if mw <= 0:
            # The simulator only accepts a positive setpoint; omit the battery instead of sending 0.
            reasons.append(f"{bid}: NON_POSITIVE_DISPATCH")
            continue
        if b.get("soc_pct") is None or b["soc_pct"] <= min_soc_pct:
            reasons.append(f"{bid}: SOC_AT_OR_BELOW_MINIMUM")
        if b.get("status") not in ("STANDBY", "DISCHARGING"):
            reasons.append(f"{bid}: STATUS_{b.get('status')}")
        if b.get("max_discharge_mw") is None or mw > b["max_discharge_mw"]:
            reasons.append(f"{bid}: EXCEEDS_MAX_DISCHARGE")
            continue
        injections[bid] = mw

    overrides = {"bess_injection_mw": injections, "deenergized_consumers": sorted(set(deenergize))}
    projection = project_loading(snapshot, threshold_pct=threshold_pct, overrides=overrides)
    sub_status = {s["id"]: s["projected_status"] for s in projection["substations"]}
    tier1 = [c for c in snapshot["consumers"] if c.get("priority_tier") == 1]
    tier1_out = []
    for c in tier1:
        supplied = bool(sub_status.get(c["substation_id"]) not in (None, "ISLANDED") and c["id"] not in deenergize and c["energized"])
        tier1_out.append({"id": c["id"], "supplied": supplied, "substation_status": sub_status.get(c["substation_id"])})
        if not supplied:
            reasons.append(f"{c['id']}: TIER1_LOSES_SUPPLY")
    if projection["outcome"] != "WITHIN_LIMITS":
        reasons.append(f"PROJECTION_{projection['outcome']}")

    shed = sum(c["shed_mw"] for c in breaker_checks if c["trip_allowed"])
    return {
        "verdict": "REJECTED" if reasons else "APPROVED",
        "reasons": reasons,
        "plan": {"trip_breakers": breakers, "bess_dispatch": dispatch},
        "state_version": snapshot["state_version"],
        "breaker_checks": breaker_checks,
        "relief_mw": round(shed + sum(injections.values()), 3),
        "tier1": tier1_out,
        "projection": projection,
        "evidence": evidence,
        "timing": {"engine_total_ms": round((time.perf_counter() - started) * 1000.0, 4)},
    }
