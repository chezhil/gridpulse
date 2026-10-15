"""Steady-state loading projection for the GridPulse grid.

This is the documented model behind every overload claim the graph engine
makes. It is a pure function of graph property values (read by
``queries.read_grid_snapshot``) plus optional what-if overrides, so the same
snapshot always gives the same answer.

Model ``dc-pf/1`` (linear DC power flow, see :mod:`graph.powerflow`):

1. Lossless DC approximation. Each connected part of the grid that contains
   a HydroPlant is an island; its first plant (by id) is the slack and
   supplies whatever the island's other plants (each injecting ``output_mw``)
   do not. A plant with ``capacity_mw`` is overloaded when its output
   exceeds that capacity (the slack's output is computed).
2. Energised circuits are branches with reactance ``impedance_pu``; FEEDS
   links are near-ideal ties. Tripped circuits are removed.
3. Substation net demand = sum of ``mw_demand`` over energised consumers minus
   ``active_injection_mw`` of connected BESS units (floored at 0), withdrawn
   at the substation bus. Substations not connected to any plant are ISLANDED.
4. Flows follow from the bus susceptance matrix, so parallel circuits and
   meshed corridors share flow by impedance. On a radial corridor this
   reduces to splitting each segment's flow in proportion to 1/impedance_pu.
5. Overload risk: |flow| above ``threshold_pct`` of ``limit_mw`` (circuits)
   or net demand above ``threshold_pct`` of ``capacity_mw`` (substations).
6. Relief for an overloaded circuit uses its PTDF: the demand reduction at a
   substation that brings the circuit back to the threshold. Each overload
   lists one option per sensitive substation; ``required_relief_mw`` is the
   largest of the per-element best options.
7. Steady state only: no thermal time constants, sag dynamics or relay
   timing are modelled.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .client import GraphDataError
from .powerflow import Branch, Network

MODEL_ID = "dc-pf/1"
ASSUMPTIONS = [
    "Lossless linear (DC) power flow; each plant-fed island has its own slack plant, other plants inject output_mw.",
    "A plant with capacity_mw is overloaded when its output exceeds capacity_mw.",
    "Energised circuits share flow by impedance_pu, including parallel and meshed paths.",
    "Substation net demand = energised consumer mw_demand - BESS active_injection_mw (floored at 0).",
    "Substations not connected to any plant through energised circuits are islanded.",
    "Overload risk = loading above threshold_pct of limit_mw / capacity_mw.",
    "Relief options use PTDF sensitivities of each overloaded circuit to substation demand.",
    "Steady state only; thermal time constants and relay timing are not modelled.",
]

FEEDS_REACTANCE = 1e-6

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


def _apply_overrides(snapshot: Dict[str, Any], overrides: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    consumers = [dict(c) for c in snapshot["consumers"]]
    bess = [dict(b) for b in snapshot["bess"]]
    applied: Dict[str, Any] = {}
    if not overrides:
        return consumers, bess, applied
    unknown_keys = sorted(set(overrides) - {"bess_injection_mw", "deenergized_consumers", "demand_scale"})
    if unknown_keys:
        raise GraphDataError("Unknown override keys", unknown=unknown_keys)

    scale = overrides.get("demand_scale")
    if scale is not None:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 < scale <= 5:
            raise GraphDataError("demand_scale must be a number in (0, 5]", value=scale)
        for c in consumers:
            if c.get("mw_demand") is not None:
                c["mw_demand"] = float(c["mw_demand"]) * scale
        applied["demand_scale"] = float(scale)

    by_id = {b["id"]: b for b in bess}
    injections = overrides.get("bess_injection_mw") or {}
    unknown = sorted(set(injections) - set(by_id))
    if unknown:
        raise GraphDataError("bess_injection_mw references BESS units that are not in the graph", unknown=unknown)
    for bid, mw in injections.items():
        limit = by_id[bid].get("max_discharge_mw")
        if not isinstance(mw, (int, float)) or mw < 0 or (limit is not None and mw > limit):
            raise GraphDataError("bess_injection_mw must be between 0 and max_discharge_mw", id=bid, value=mw, limit=limit)
        by_id[bid]["active_injection_mw"] = float(mw)
    if injections:
        applied["bess_injection_mw"] = {k: float(v) for k, v in sorted(injections.items())}

    off = set(overrides.get("deenergized_consumers") or [])
    unknown = sorted(off - {c["id"] for c in consumers})
    if unknown:
        raise GraphDataError("deenergized_consumers references consumers that are not in the graph", unknown=unknown)
    for c in consumers:
        if c["id"] in off:
            c["energized"] = False
    if off:
        applied["deenergized_consumers"] = sorted(off)
    return consumers, bess, applied


def project_loading(
    snapshot: Dict[str, Any],
    assume_tripped: Optional[Iterable[CircuitRef]] = None,
    threshold_pct: float = 95.0,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project circuit and substation loading for ``snapshot``.

    ``assume_tripped`` and ``overrides`` (``bess_injection_mw`` and
    ``deenergized_consumers``) are hypothetical changes layered on the stored
    state for what-if evaluation. They never change the graph.
    """
    validate_snapshot(snapshot)
    assumed: Set[CircuitRef] = {(s, c) for s, c in (assume_tripped or [])}
    threshold = threshold_pct / 100.0

    known = {(c["span_id"], c["circuit"]) for c in snapshot["circuits"]}
    unknown = sorted(f"{s}:{c}" for s, c in assumed - known)
    if unknown:
        raise GraphDataError("assume_tripped references circuits that are not in the graph", unknown=unknown)
    consumers, bess, applied_overrides = _apply_overrides(snapshot, overrides)

    circuits = []
    for c in snapshot["circuits"]:
        tripped = c["status"] == "TRIPPED" or (c["span_id"], c["circuit"]) in assumed
        circuits.append({**c, "effective_status": "TRIPPED" if tripped else "ENERGIZED"})

    plants: Dict[str, Dict[str, Any]] = {}
    for f in snapshot["feeds"]:
        plants.setdefault(f["src"], f)
    buses: Set[str] = {s["id"] for s in snapshot["substations"]}
    for c in circuits:
        buses.update((c["src"], c["dst"]))
    for f in snapshot["feeds"]:
        buses.update((f["src"], f["dst"]))
    branches = [Branch(("FEEDS", f["src"], f["dst"]), f["src"], f["dst"], FEEDS_REACTANCE) for f in snapshot["feeds"]]
    branches += [
        Branch((c["span_id"], c["circuit"]), c["src"], c["dst"], float(c["impedance_pu"]))
        for c in circuits
        if c["effective_status"] == "ENERGIZED"
    ]
    # Each connected part that contains a plant is its own island with its own slack
    # (the first plant by id); parts without a plant are unsupplied.
    nets: List[Network] = []
    net_of: Dict[str, Network] = {}
    for pid in sorted(plants):
        if pid in net_of:
            continue
        net = Network(buses, branches, pid)
        nets.append(net)
        for bus in net.island:
            net_of[bus] = net
    energized: Set[str] = set(net_of)

    demand: Dict[str, float] = {}
    for s in snapshot["substations"]:
        load = sum(float(c["mw_demand"]) for c in consumers if c["substation_id"] == s["id"] and c["energized"])
        support = sum(float(b["active_injection_mw"]) for b in bess if b["substation_id"] == s["id"])
        demand[s["id"]] = max(load - support, 0.0) if s["id"] in energized else 0.0
    # Every plant except its island's slack injects its stated output; the slack balances the island.
    flows: Dict[Any, float] = {}
    slack_output: Dict[str, float] = {}
    for net in nets:
        injections = {sid: -mw for sid, mw in demand.items() if sid in net.island}
        fixed = 0.0
        for pid, plant in plants.items():
            if pid != net.slack and pid in net.island:
                mw = float(plant.get("output_mw") or 0.0)
                injections[pid] = injections.get(pid, 0.0) + mw
                fixed += mw
        for key, value in net.flows(injections).items():
            if value:
                flows[key] = value
        slack_output[net.slack] = sum(mw for sid, mw in demand.items() if sid in net.island) - fixed
    served_subs = sorted(s for s in demand if s in energized)

    def ptdf(key: Any, sid: str) -> float:
        net = net_of.get(sid)
        return net.ptdf_for(key, sid) if net else 0.0

    circuits_out: List[Dict[str, Any]] = []
    overloads: List[Dict[str, Any]] = []
    overloaded_supply: Dict[str, List[str]] = {s["id"]: [] for s in snapshot["substations"]}
    reliefs: List[float] = []

    for c in sorted(circuits, key=lambda x: (x["span_id"], x["circuit"] != "UPPER_PRIMARY")):
        key = (c["span_id"], c["circuit"])
        signed = flows.get(key, 0.0) if c["effective_status"] == "ENERGIZED" else 0.0
        flow = abs(signed)
        loading = flow / c["limit_mw"] * 100.0
        overload = loading > threshold_pct
        element = f"{c['span_id']}:{c['circuit']}"
        options = []
        if overload:
            excess = flow - threshold * c["limit_mw"]
            for sid in served_subs:
                sens = ptdf(key, sid)
                if abs(sens) > 1e-6:
                    overloaded_supply[sid].append(element)
                    # Reducing demand at sid is +injection there; it helps when it opposes the flow.
                    if sens * signed < 0:
                        options.append({"substation_id": sid, "relief_mw": round(excess / abs(sens), 3), "sensitivity": round(sens, 6)})
            options.sort(key=lambda o: o["relief_mw"])
        relief = options[0]["relief_mw"] if options else 0.0
        circuits_out.append(
            {
                "span_id": c["span_id"],
                "circuit": c["circuit"],
                "from": c["src"],
                "to": c["dst"],
                "stored_status": c["status"],
                "status": c["effective_status"],
                "assumed_trip": key in assumed,
                "stored_flow_mw": c.get("flow_mw"),
                "flow_mw": round(flow, 3),
                "direction": "REVERSE" if signed < -1e-9 else "FORWARD",
                "limit_mw": c["limit_mw"],
                "loading_pct": round(loading, 2),
                "overload": overload,
                "relief_mw": round(relief, 3),
            }
        )
        if overload:
            reliefs.append(relief if options else float("inf"))
            overloads.append(
                {"element": element, "kind": "CIRCUIT", "loading_pct": round(loading, 2), "relief_mw": round(relief, 3), "relief_options": options}
            )

    substations_out: List[Dict[str, Any]] = []
    islanded: List[str] = []
    for s in sorted(snapshot["substations"], key=lambda x: x["id"]):
        sid = s["id"]
        is_energized = sid in energized
        net_mw = demand[sid]
        loading = net_mw / s["capacity_mw"] * 100.0
        overload = is_energized and loading > threshold_pct
        relief = max(net_mw - threshold * s["capacity_mw"], 0.0) if overload else 0.0
        if overload:
            reliefs.append(relief)
            overloads.append(
                {
                    "element": sid,
                    "kind": "SUBSTATION",
                    "loading_pct": round(loading, 2),
                    "relief_mw": round(relief, 3),
                    "relief_options": [{"substation_id": sid, "relief_mw": round(relief, 3), "sensitivity": 1.0}],
                }
            )
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
                "net_demand_mw": round(net_mw, 3),
                "capacity_mw": s["capacity_mw"],
                "loading_pct": round(loading, 2),
                "overload": overload,
                "overloaded_supply_circuits": overloaded_supply[sid],
                "relief_mw": round(relief, 3),
            }
        )

    generators_out: List[Dict[str, Any]] = []
    for pid in sorted(plants):
        plant = plants[pid]
        if pid in slack_output:
            output = slack_output[pid]
        else:
            output = float(plant.get("output_mw") or 0.0) if pid in energized else 0.0
        capacity = plant.get("capacity_mw")
        loading = (output / capacity * 100.0) if capacity else None
        overload = bool(capacity) and output > capacity
        relief = max(output - capacity, 0.0) if overload else 0.0
        generators_out.append(
            {
                "id": pid,
                "role": "SLACK" if pid in slack_output else "FIXED",
                "output_mw": round(output, 3),
                "capacity_mw": capacity,
                "loading_pct": round(loading, 2) if loading is not None else None,
                "overload": overload,
            }
        )
        if overload:
            reliefs.append(relief)
            overloads.append(
                {
                    "element": pid,
                    "kind": "GENERATOR",
                    "loading_pct": round(loading, 2),
                    "relief_mw": round(relief, 3),
                    "relief_options": [{"substation_id": sid, "relief_mw": round(relief, 3), "sensitivity": 1.0} for sid in served_subs],
                }
            )

    if islanded:
        outcome = "ISLANDING_PREDICTED"
    elif overloads:
        outcome = "OVERLOAD_PREDICTED"
    else:
        outcome = "WITHIN_LIMITS"
    required = max(reliefs, default=0.0)

    return {
        "model": MODEL_ID,
        "assumptions": ASSUMPTIONS,
        "threshold_pct": threshold_pct,
        "assumed_tripped": [{"span_id": s, "circuit": c} for s, c in sorted(assumed)],
        "overrides": applied_overrides,
        "outcome": outcome,
        "required_relief_mw": round(required, 3) if required != float("inf") else None,
        "relief_feasible": required != float("inf"),
        "overloads": overloads,
        "islanded_substations": islanded,
        "energized_nodes": sorted(energized),
        "circuits": circuits_out,
        "substations": substations_out,
        "generators": generators_out,
    }
