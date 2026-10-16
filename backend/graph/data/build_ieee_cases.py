"""Build the IEEE 14/30/118-bus sample files and templates from pandapower's public cases.

Dev-only tool: needs ``pip install pandapower``. Outputs are committed, so the
app and CI do not need pandapower.

    python -m backend.graph.data.build_ieee_cases

For each case it writes:

* ``ieee<N>.m``    - a standard MATPOWER case file (sample upload for the import UI).
  ``rateA`` is written as 0 because the source cases carry no meaningful ratings.
* ``ieee<N>.json`` - the GridPulse template produced by *our own importer* from that
  file, plus pandapower's ``rundcpp`` branch flows as ``reference_flows_mw`` so CI can
  check the GridPulse solver against an established tool.
"""
import json
from pathlib import Path

import pandapower as pp
import pandapower.networks as pn

from backend.graph.importers import matpower_to_spec, parse_matpower

HERE = Path(__file__).parent
CASES = {14: pn.case14, 30: pn.case30, 118: pn.case118}


def _row(values, ints):
    return "\t".join(str(int(v)) if i in ints else f"{float(v):.8g}" for i, v in enumerate(values))


def write_matpower(net, n: int) -> str:
    ppc = net._ppc
    bus = ppc["bus"].real.copy()
    gen = ppc["gen"].real.copy()
    branch = ppc["branch"].real.copy()
    bus[:, 0] += 1
    gen[:, 0] += 1
    branch[:, 0:2] += 1
    branch[:, 5:8] = 0  # no ratings in the source case
    lines = [f"function mpc = ieee{n}", f"% IEEE {n}-bus test case, exported from pandapower {pp.__version__} for GridPulse.",
             "mpc.version = '2';", f"mpc.baseMVA = {ppc['baseMVA']:g};", "", "% bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin",
             "mpc.bus = ["]
    lines += [_row(r[:13], {0, 1, 6, 10}) + ";" for r in bus]
    lines += ["];", "", "% bus Pg Qg Qmax Qmin Vg mBase status Pmax Pmin", "mpc.gen = ["]
    lines += [_row(r[:10], {0, 7}) + ";" for r in gen]
    lines += ["];", "", "% fbus tbus r x b rateA rateB rateC ratio angle status angmin angmax", "mpc.branch = ["]
    lines += [_row(r[:13], {0, 1, 10}) + ";" for r in branch]
    lines += ["];", ""]
    return "\n".join(lines)


def reference_flows(net) -> dict:
    lookup = net._pd2ppc_lookups["branch"]
    flows = {}
    for kind, res, col in (("line", net.res_line, "p_from_mw"), ("trafo", net.res_trafo, "p_hv_mw")):
        if kind not in lookup:
            continue
        start, _ = lookup[kind]
        for offset, idx in enumerate(getattr(net, kind).index):
            flows[f"BR{start + offset + 1:03d}"] = round(float(res[col][idx]), 4)
    return flows


def main() -> None:
    for n, factory in CASES.items():
        net = factory()
        pp.rundcpp(net)
        text = write_matpower(net, n)
        (HERE / f"ieee{n}.m").write_text(text)
        spec, notes, _ = matpower_to_spec(parse_matpower(text), f"IEEE {n}-bus test system", battery_mw=60.0 if n >= 100 else 30.0)
        spec["description"] = (f"Public IEEE {n}-bus case (via pandapower), imported with GridPulse's MATPOWER importer. "
                               "Ratings, priority tiers and batteries are synthetic overlays.")
        (HERE / f"ieee{n}.json").write_text(json.dumps({"spec": spec, "notes": notes, "reference_flows_mw": reference_flows(net),
                                                        "source": f"pandapower {pp.__version__} case{n}, rundcpp"}, indent=1))
        print(f"ieee{n}: {len(spec['buses']) + len(spec['substations'])} buses, {len(spec['lines'])} branches, notes={notes}")


if __name__ == "__main__":
    main()
