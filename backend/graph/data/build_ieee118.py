"""Build the IEEE 118-bus template (ieee118.json) from pandapower's public case.

Dev-only tool: needs ``pip install pandapower``. The generated JSON is committed,
so the app and CI do not need pandapower.

    python -m backend.graph.data.build_ieee118

Network data (buses, branch reactances incl. transformer taps, loads, generator
dispatch) come from the public IEEE 118-bus case as shipped by pandapower.
Synthetic overlays, needed because the case has no such data:

* line ratings: 1.6 x |base-case DC flow|, rounded up to 10 MW, min 100 MW
* substation capacity: 1.5 x bus load, rounded up to 10 MW
* priority tiers: five buses are Tier-1 (hospitals); every fourth load bus is
  Tier-3 (industrial); the rest are Tier-2
* one 60 MW battery at each Tier-1 bus

The pandapower DC power-flow result for every branch is stored as
``reference_flows_mw`` so tests can check GridPulse's solver against it.
"""
import json
import math
from pathlib import Path

import pandapower as pp
import pandapower.networks as pn

OUT = Path(__file__).with_name("ieee118.json")
TIER1_BUSES = {11, 15, 49, 59, 80}


def bus_id(i: int) -> str:
    return f"B{i:03d}"


def main() -> None:
    net = pn.case118()
    pp.rundcpp(net)
    ppc = net._ppc
    # pandapower's internal bus order matches the case numbering (0-based).
    lookup = net._pd2ppc_lookups["bus"]
    load_mw = {}
    for _, row in net.load.iterrows():
        load_mw[int(row.bus) + 1] = load_mw.get(int(row.bus) + 1, 0.0) + float(row.p_mw)

    buses, substations, loads, batteries = [], [], [], []
    load_buses = sorted(b for b, mw in load_mw.items() if mw > 0)
    tier3 = {b for i, b in enumerate(load_buses) if i % 4 == 3 and b not in TIER1_BUSES}
    for b in range(1, len(net.bus) + 1):
        mw = load_mw.get(b, 0.0)
        if mw > 0:
            substations.append({"id": bus_id(b), "capacity_mw": float(max(10, math.ceil(1.5 * mw / 10) * 10))})
            tier = 1 if b in TIER1_BUSES else 3 if b in tier3 else 2
            loads.append({"id": f"LOAD_{bus_id(b)}", "substation": bus_id(b), "mw_demand": round(mw, 3), "priority_tier": tier,
                          "category": {1: "HOSPITAL", 2: "RESIDENTIAL", 3: "INDUSTRIAL"}[tier]})
            if tier == 1:
                batteries.append({"id": f"BESS_{bus_id(b)}", "substation": bus_id(b), "max_discharge_mw": 60.0, "soc_pct": 80.0})
        else:
            buses.append({"id": bus_id(b)})

    slack_bus = int(net.ext_grid.bus.iloc[0]) + 1
    gens = {}
    for _, row in net.gen.iterrows():
        b = int(row.bus) + 1
        g = gens.setdefault(b, {"output_mw": 0.0, "capacity_mw": 0.0})
        g["output_mw"] += float(row.p_mw)
        g["capacity_mw"] += float(row.max_p_mw)
    slack_cap = float(net.ext_grid.max_p_mw.iloc[0]) if "max_p_mw" in net.ext_grid else 1000.0
    generators = [{"id": f"GEN_000_{bus_id(slack_bus)}", "name": "Slack", "output_mw": 0.0,
                   "capacity_mw": round(slack_cap if slack_cap and slack_cap > 0 else 1000.0, 1), "connects_to": bus_id(slack_bus)}]
    for b, g in sorted(gens.items()):
        if b == slack_bus:
            continue
        cap = g["capacity_mw"] if g["capacity_mw"] > 0 else None
        generators.append({"id": f"GEN_{bus_id(b)}", "output_mw": round(g["output_mw"], 3),
                           "capacity_mw": round(max(cap, g["output_mw"]), 3) if cap else None, "connects_to": bus_id(b)})

    # Branches in pandapower result order: lines, then transformers.
    lines = []
    reference = {}
    branch_rows = []
    for idx, row in net.line.iterrows():
        branch_rows.append(("line", idx, int(row.from_bus) + 1, int(row.to_bus) + 1, float(net.res_line.p_from_mw[idx])))
    for idx, row in net.trafo.iterrows():
        branch_rows.append(("trafo", idx, int(row.hv_bus) + 1, int(row.lv_bus) + 1, float(net.res_trafo.p_hv_mw[idx])))
    ppc_branch = ppc["branch"]
    lookups = net._pd2ppc_lookups["branch"]
    for n, (kind, idx, fb, tb, flow) in enumerate(branch_rows, start=1):
        start, _ = lookups[kind]
        br = ppc_branch[start + list(getattr(net, kind).index).index(idx)]
        x = float(br[3].real)
        tap = float(br[8].real) or 1.0
        lid = f"BR{n:03d}"
        limit = float(max(100, math.ceil(1.6 * abs(flow) / 10) * 10))
        lines.append({"id": lid, "from": bus_id(fb), "to": bus_id(tb), "impedance_pu": round(x * tap, 6), "limit_mw": limit})
        reference[lid] = round(flow, 4)

    spec = {
        "name": "IEEE 118-bus test system",
        "description": "Public IEEE 118-bus case (via pandapower) with synthetic ratings, tiers and batteries; see build_ieee118.py.",
        "generators": generators, "buses": buses, "substations": substations, "lines": lines, "loads": loads,
        "batteries": batteries,
    }
    OUT.write_text(json.dumps({"spec": spec, "reference_flows_mw": reference, "source": f"pandapower {pp.__version__} case118, rundcpp"},
                              indent=1))
    print(f"wrote {OUT} ({len(buses) + len(substations)} buses, {len(lines)} branches, {len(generators)} plants)")


if __name__ == "__main__":
    main()
