"""Simulator -> graph state synchronisation.

The simulator owns physics; the graph holds the state decisions are based on.
Before the interceptor traces or acts, the simulator pushes its current
values with :func:`apply_state_update`. Every accepted update bumps the
graph's ``state_version``, which trace results echo back, so an audit record
can show which state a decision used.

The whole update is validated (field names, types, enums, ranges, IDs exist)
before anything is written. Writes are then applied in one query per entity
type; they are not a single transaction, so callers should hold the
simulator's state lock across sync -> trace -> actuate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import topology as topo
from .client import GraphClient, InvalidRequestError, UnknownEntityError, get_client


def _number(minimum: float = 0.0, maximum: float = math.inf) -> Callable[[Any], Optional[str]]:
    def check(value: Any) -> Optional[str]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "must be a finite number"
        if not minimum <= value <= maximum:
            return f"must be between {minimum} and {maximum}"
        return None

    return check


def _enum(values: Tuple[str, ...]) -> Callable[[Any], Optional[str]]:
    return lambda v: None if v in values else f"must be one of {list(values)}"


def _boolean(value: Any) -> Optional[str]:
    return None if isinstance(value, bool) else "must be a boolean"


# entity key -> (label, {field: validator})
NODE_FIELDS: Dict[str, Tuple[str, Dict[str, Callable[[Any], Optional[str]]]]] = {
    "hydro": ("HydroPlant", {"output_mw": _number()}),
    "towers": ("Tower", {"status": _enum(topo.TOWER_STATUSES), "insulator_temp_c": _number(-60, 400)}),
    "substations": (
        "Substation",
        {
            "status": _enum(topo.SUBSTATION_STATUSES),
            "current_load_mw": _number(),
            "transformer_temp_c": _number(-60, 400),
        },
    ),
    "bess": (
        "BESS",
        {"status": _enum(topo.BESS_STATUSES), "soc_pct": _number(0, 100), "active_injection_mw": _number()},
    ),
    "consumers": ("Consumer", {"energized": _boolean, "mw_demand": _number()}),
}
SPAN_FIELDS: Dict[str, Callable[[Any], Optional[str]]] = {
    "status": _enum(topo.SPAN_STATUSES),
    "flow_mw": _number(),
    "sag_m": _number(0, 100),
}

EXISTING_NODES_CYPHER = """\
UNWIND $ids AS id
OPTIONAL MATCH (n {id: id})
RETURN id, labels(n)[0] AS label, n.max_discharge_mw AS max_discharge_mw"""

EXISTING_CIRCUITS_CYPHER = """\
UNWIND $keys AS k
OPTIONAL MATCH ()-[r:SPAN|TERMINATES_AT]->() WHERE r.id = k.span_id AND r.circuit = k.circuit
RETURN k.span_id AS span_id, k.circuit AS circuit, count(r) AS matches"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate(update: Dict[str, Any]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    problems: List[Dict[str, Any]] = []
    unknown_keys = sorted(set(update) - set(NODE_FIELDS) - {"spans", "source"})
    if unknown_keys:
        problems.append({"field": "update", "error": f"unknown sections {unknown_keys}"})

    node_rows: Dict[str, List[Dict[str, Any]]] = {}
    for key, (label, fields) in NODE_FIELDS.items():
        for i, item in enumerate(update.get(key) or []):
            where = f"{key}[{i}]"
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                problems.append({"field": where, "error": "must be an object with a string id"})
                continue
            props = {k: v for k, v in item.items() if k != "id"}
            for field, value in props.items():
                check = fields.get(field)
                error = f"field not writable (allowed: {sorted(fields)})" if check is None else check(value)
                if error:
                    problems.append({"field": f"{where}.{field}", "id": item["id"], "error": error})
            if not props:
                problems.append({"field": where, "id": item["id"], "error": "no properties to set"})
            node_rows.setdefault(label, []).append({"id": item["id"], "props": props})

    span_rows: List[Dict[str, Any]] = []
    for i, item in enumerate(update.get("spans") or []):
        where = f"spans[{i}]"
        if not isinstance(item, dict) or not isinstance(item.get("span_id"), str) or item.get("circuit") not in topo.CIRCUITS:
            problems.append({"field": where, "error": f"needs span_id and circuit in {list(topo.CIRCUITS)}"})
            continue
        props = {k: v for k, v in item.items() if k not in ("span_id", "circuit")}
        for field, value in props.items():
            check = SPAN_FIELDS.get(field)
            error = f"field not writable (allowed: {sorted(SPAN_FIELDS)})" if check is None else check(value)
            if error:
                problems.append({"field": f"{where}.{field}", "id": item["span_id"], "error": error})
        if not props:
            problems.append({"field": where, "id": item["span_id"], "error": "no properties to set"})
        span_rows.append({"span_id": item["span_id"], "circuit": item["circuit"], "props": props})

    return node_rows, span_rows, problems


def apply_state_update(update: Dict[str, Any], client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Write simulator state into the graph.

    ``update`` sections (all optional)::

        {
          "source": "sim-run-42",                      # free-text tag stored on GridMeta
          "spans": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY", "status": "TRIPPED", "flow_mw": 0.0}],
          "towers": [{"id": "T3", "status": "FAULT", "insulator_temp_c": 91.0}],
          "substations": [{"id": "SUB_METRO", "current_load_mw": 840.0, "status": "OVERLOAD_RISK"}],
          "bess": [{"id": "BESS_METRO_01", "active_injection_mw": 45.0, "status": "DISCHARGING", "soc_pct": 77.5}],
          "consumers": [{"id": "IND_METRO_12", "energized": false}],
          "hydro": [{"id": "HYDRO_ALPINE_01", "output_mw": 840.0}]
        }

    Raises :class:`InvalidRequestError` or :class:`UnknownEntityError` without
    writing anything if any part is invalid.
    """
    client = client or get_client()
    if not isinstance(update, dict):
        raise InvalidRequestError("update must be an object")
    node_rows, span_rows, problems = _validate(update)
    if problems:
        raise InvalidRequestError("State update rejected", problems=problems)
    if not node_rows and not span_rows:
        raise InvalidRequestError("State update contains no changes")

    evidence = []
    ids = sorted({r["id"] for rows in node_rows.values() for r in rows})
    if ids:
        found_q = client.query("sync_check_nodes", EXISTING_NODES_CYPHER, {"ids": ids})
        evidence.append(found_q)
        found = {r["id"]: r for r in found_q.rows}
        for label, rows in node_rows.items():
            for r in rows:
                actual = found.get(r["id"], {}).get("label")
                if actual != label:
                    problems.append({"id": r["id"], "expected_label": label, "found_label": actual})
                elif label == "BESS" and "active_injection_mw" in r["props"]:
                    limit = found[r["id"]]["max_discharge_mw"]
                    if limit is not None and r["props"]["active_injection_mw"] > limit:
                        problems.append({"id": r["id"], "field": "active_injection_mw", "error": f"exceeds max_discharge_mw {limit}"})
    if span_rows:
        keys = [{"span_id": r["span_id"], "circuit": r["circuit"]} for r in span_rows]
        circ_q = client.query("sync_check_circuits", EXISTING_CIRCUITS_CYPHER, {"keys": keys})
        evidence.append(circ_q)
        for r in circ_q.rows:
            if r["matches"] != 1:
                problems.append({"span_id": r["span_id"], "circuit": r["circuit"], "matches": r["matches"]})
    if problems:
        raise UnknownEntityError("State update references unknown or mismatched entities; nothing was written", problems=problems)

    applied: Dict[str, int] = {}
    for label, rows in node_rows.items():
        q = client.query(
            f"sync_{label}",
            f"UNWIND $rows AS row MATCH (n:{label} {{id: row.id}}) SET n += row.props RETURN count(n) AS updated",
            {"rows": rows},
            read_only=False,
            retries=0,
        )
        evidence.append(q)
        applied[label] = int(q.rows[0]["updated"])
    if span_rows:
        q = client.query(
            "sync_circuits",
            "UNWIND $rows AS row MATCH ()-[r:SPAN|TERMINATES_AT]->() "
            "WHERE r.id = row.span_id AND r.circuit = row.circuit SET r += row.props RETURN count(r) AS updated",
            {"rows": span_rows},
            read_only=False,
            retries=0,
        )
        evidence.append(q)
        applied["circuits"] = int(q.rows[0]["updated"])

    meta_q = client.query(
        "sync_bump_version",
        "MERGE (m:GridMeta {id: $id}) ON CREATE SET m.state_version = 0 "
        "SET m.state_version = m.state_version + 1, m.updated_at = $now, m.last_source = $source "
        "RETURN m.state_version AS state_version",
        {"id": topo.META_ID, "now": _now(), "source": str(update.get("source") or "")},
        read_only=False,
        retries=0,
    )
    evidence.append(meta_q)
    return {
        "state_version": int(meta_q.rows[0]["state_version"]),
        "applied": applied,
        "server_ms": round(sum(e.server_ms for e in evidence), 4),
    }


def read_state_version(client: Optional[GraphClient] = None) -> Optional[int]:
    client = client or get_client()
    q = client.query("state_version", "OPTIONAL MATCH (m:GridMeta {id: $id}) RETURN m.state_version AS v", {"id": topo.META_ID})
    v = q.rows[0]["v"] if q.rows else None
    return int(v) if v is not None else None
