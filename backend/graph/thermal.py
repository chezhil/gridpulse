"""Steady-state loading projection for the GridPulse corridor.

This is the documented model behind every overload claim the graph engine
makes. It is a pure function of graph property values (read by
``queries.read_grid_snapshot``) plus optional hypothetical trips, so the same
snapshot always gives the same answer.

Model ``radial-dc/1``:

1. Lossless DC approximation. The source is a slack bus: ``output_mw`` is
   reported but not enforced as a generation limit.
2. Energisation: a node is energised if it is reachable from a HydroPlant over
   FEEDS links and segments that have at least one ENERGIZED circuit.
3. Substation net demand = sum of ``mw_demand`` over energised consumers minus
   ``active_injection_mw`` of connected BESS units (floored at 0). An
   un-energised substation is ISLANDED and serves nothing.
4. A segment carries the net demand of every energised substation downstream
   of it. Parallel energised circuits share that flow in proportion to
   ``1 / impedance_pu``; tripped circuits carry 0.
5. Overload risk: loading above ``threshold_pct`` of ``limit_mw`` (circuits)
   or ``capacity_mw`` (substations).
6. Relief is the reduction in downstream net demand (BESS injection or load
   shed at the substation) that brings an element back to the threshold,
   assuming the same circuits stay energised. ``required_relief_mw`` is the
   maximum over all elements, which is exact for a single radial substation.
7. Steady state only: no thermal time constants, sag dynamics or relay
   timing are modelled.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .client import GraphDataError

MODEL_ID = "radial-dc/1"
ASSUMPTIONS = [
    "Lossless DC approximation; the hydro source is a slack bus (output_mw not enforced).",
    "Segments carry the net demand of energised substations downstream of them.",
    "Substation net demand = energised consumer mw_demand - BESS active_injection_mw (floored at 0).",
    "Parallel energised circuits share segment flow in proportion to 1/impedance_pu.",
    "A segment with no energised circuit islands everything downstream.",
    "Overload risk = loading above threshold_pct of limit_mw / capacity_mw.",
    "Steady state only; thermal time constants and relay timing are not modelled.",
]

CircuitRef = Tuple[str, str]


def _require(entity: str, item: Dict[str, Any], fields: Iterable[str], problems: List[Dict[str, Any]]) -> None:
    missing = [f for f in fields if item.get(f) is None]
    if missing:
        problems.append({"entity": entity, "id": item.get("id") or item.get("span_id"), "missing": missing})


def validate_snapshot(snapshot: Dict[str, Any]) -> None:
    problems: List[Dict[str, Any]] = []
    for c in snapshot["circuits"]:
        _require("circuit", c, ("span_id", "circuit", "src", "dst", "impedance_pu", "limit_mw", "status"), problems)
        if c.get("impedance_pu") is not None and c["impedance_pu"] <= 0:
            problems.append({"entity": "circuit", "id": f"{c['span_id']}:{c['circuit']}", "invalid": "impedance_pu"})
        if c.get("limit_mw") is not None and c["limit_mw"] <= 0:
            problems.append({"entity": "circuit", "id": f"{c['span_id']}:{c['circuit']}", "invalid": "limit_mw"})
    for s in snapshot["substations"]:
        _require("substation", s, ("id", "capacity_mw"), problems)
    for c in snapshot["consumers"]:
        _require("consumer", c, ("id", "substation_id", "mw_demand", "energized"), problems)
    for b in snapshot["bess"]:
        _require("bess", b, ("id", "substation_id", "active_injection_mw"), problems)
    if not snapshot["feeds"]:
        problems.append({"entity": "source", "missing": ["HydroPlant-[:FEEDS]->Tower"]})
    if not snapshot["circuits"]:
        problems.append({"entity": "circuit", "missing": ["SPAN/TERMINATES_AT relationships"]})
    if problems:
        raise GraphDataError("Graph state is missing properties required by the loading model", problems=problems)


def project_loading(
    snapshot: Dict[str, Any],
    assume_tripped: Optional[Iterable[CircuitRef]] = None,
    threshold_pct: float = 95.0,
) -> Dict[str, Any]:
    """Project circuit and substation loading for ``snapshot``.

    ``assume_tripped`` adds hypothetical trips on top of the stored statuses
    (for "what if" traces). It never changes the graph.
    """
    validate_snapshot(snapshot)
    assumed: Set[CircuitRef] = {(s, c) for s, c in (assume_tripped or [])}
    threshold = threshold_pct / 100.0

    known = {(c["span_id"], c["circuit"]) for c in snapshot["circuits"]}
    unknown = sorted(f"{s}:{c}" for s, c in assumed - known)
    if unknown:
        raise GraphDataError("assume_tripped references circuits that are not in the graph", unknown=unknown)

    # Group circuits into segments and work out their effective status.
    segments: Dict[str, Dict[str, Any]] = {}
    for c in snapshot["circuits"]:
        seg = segments.setdefault(c["span_id"], {"span_id": c["span_id"], "src": c["src"], "dst": c["dst"], "circuits": []})
        tripped = c["status"] == "TRIPPED" or (c["span_id"], c["circuit"]) in assumed
        seg["circuits"].append({**c, "effective_status": "TRIPPED" if tripped else "ENERGIZED"})

    adjacency: Dict[str, List[str]] = {}
    for f in snapshot["feeds"]:
        adjacency.setdefault(f["src"], []).append(f["dst"])
    for seg in segments.values():
        if any(c["effective_status"] == "ENERGIZED" for c in seg["circuits"]):
            adjacency.setdefault(seg["src"], []).append(seg["dst"])

    sources = {f["src"] for f in snapshot["feeds"]}
    energized = _reachable(adjacency, sources)

    consumers_by_sub: Dict[str, List[Dict[str, Any]]] = {}
    for c in snapshot["consumers"]:
        consumers_by_sub.setdefault(c["substation_id"], []).append(c)
    bess_by_sub: Dict[str, List[Dict[str, Any]]] = {}
    for b in snapshot["bess"]:
        bess_by_sub.setdefault(b["substation_id"], []).append(b)

    net_demand: Dict[str, float] = {}
    for s in snapshot["substations"]:
        demand = sum(float(c["mw_demand"]) for c in consumers_by_sub.get(s["id"], []) if c["energized"])
        injection = sum(float(b["active_injection_mw"]) for b in bess_by_sub.get(s["id"], []))
        net_demand[s["id"]] = max(demand - injection, 0.0) if s["id"] in energized else 0.0

    served_downstream: Dict[str, Set[str]] = {}

    def downstream_subs(node: str) -> Set[str]:
        if node not in served_downstream:
            served_downstream[node] = {n for n in _reachable(adjacency, {node}) if n in net_demand}
        return served_downstream[node]

    circuits_out: List[Dict[str, Any]] = []
    overloads: List[Dict[str, Any]] = []
    overloaded_supply: Dict[str, List[str]] = {s["id"]: [] for s in snapshot["substations"]}
    reliefs: List[float] = []

    for span_id in sorted(segments):
        seg = segments[span_id]
        live = [c for c in seg["circuits"] if c["effective_status"] == "ENERGIZED"]
        seg_flow = 0.0
        if live and seg["src"] in energized:
            seg_flow = sum(net_demand[s] for s in downstream_subs(seg["dst"]))
        total_y = sum(1.0 / c["impedance_pu"] for c in live)
        for c in sorted(seg["circuits"], key=lambda x: x["circuit"], reverse=True):
            share = (1.0 / c["impedance_pu"]) / total_y if c["effective_status"] == "ENERGIZED" else 0.0
            flow = seg_flow * share
            loading = flow / c["limit_mw"] * 100.0
            overload = loading > threshold_pct
            relief = max(seg_flow - threshold * c["limit_mw"] / share, 0.0) if overload else 0.0
            element = f"{span_id}:{c['circuit']}"
            circuits_out.append(
                {
                    "span_id": span_id,
                    "circuit": c["circuit"],
                    "from": seg["src"],
                    "to": seg["dst"],
                    "stored_status": c["status"],
                    "status": c["effective_status"],
                    "assumed_trip": (span_id, c["circuit"]) in assumed,
                    "stored_flow_mw": c.get("flow_mw"),
                    "flow_mw": round(flow, 3),
                    "limit_mw": c["limit_mw"],
                    "loading_pct": round(loading, 2),
                    "overload": overload,
                    "relief_mw": round(relief, 3),
                }
            )
            if overload:
                reliefs.append(relief)
                overloads.append({"element": element, "kind": "CIRCUIT", "loading_pct": round(loading, 2), "relief_mw": round(relief, 3)})
                for sub in downstream_subs(seg["dst"]):
                    overloaded_supply[sub].append(element)

    substations_out: List[Dict[str, Any]] = []
    islanded: List[str] = []
    for s in sorted(snapshot["substations"], key=lambda x: x["id"]):
        sid = s["id"]
        is_energized = sid in energized
        net = net_demand[sid]
        loading = net / s["capacity_mw"] * 100.0
        overload = is_energized and loading > threshold_pct
        relief = max(net - threshold * s["capacity_mw"], 0.0) if overload else 0.0
        if overload:
            reliefs.append(relief)
            overloads.append({"element": sid, "kind": "SUBSTATION", "loading_pct": round(loading, 2), "relief_mw": round(relief, 3)})
        if not is_energized:
            status = "ISLANDED"
            islanded.append(sid)
        elif overload or overloaded_supply[sid]:
            status = "OVERLOAD_RISK"
        else:
            status = "ONLINE"
        substations_out.append(
            {
                "id": sid,
                "energized": is_energized,
                "stored_status": s.get("status"),
                "projected_status": status,
                "net_demand_mw": round(net, 3),
                "capacity_mw": s["capacity_mw"],
                "loading_pct": round(loading, 2),
                "overload": overload,
                "overloaded_supply_circuits": overloaded_supply[sid],
                "relief_mw": round(relief, 3),
            }
        )

    if islanded:
        outcome = "ISLANDING_PREDICTED"
    elif overloads:
        outcome = "OVERLOAD_PREDICTED"
    else:
        outcome = "WITHIN_LIMITS"

    return {
        "model": MODEL_ID,
        "assumptions": ASSUMPTIONS,
        "threshold_pct": threshold_pct,
        "assumed_tripped": [{"span_id": s, "circuit": c} for s, c in sorted(assumed)],
        "outcome": outcome,
        "required_relief_mw": round(max(reliefs, default=0.0), 3),
        "overloads": overloads,
        "islanded_substations": islanded,
        "energized_nodes": sorted(energized),
        "circuits": circuits_out,
        "substations": substations_out,
    }


def _reachable(adjacency: Dict[str, List[str]], starts: Iterable[str]) -> Set[str]:
    seen: Set[str] = set()
    queue = deque(starts)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(adjacency.get(node, []))
    return seen
