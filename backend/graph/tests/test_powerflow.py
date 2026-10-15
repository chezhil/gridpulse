"""DC power flow, LODF and meshed-network behaviour of the loading model."""

import pytest

from graph.powerflow import Branch, Network, solve
from graph.synthetic import generate
from graph.thermal import project_loading


def ring():
    # S -> A -> C and S -> B -> C: two parallel paths of different impedance.
    branches = [Branch("SA", "S", "A", 0.01), Branch("AC", "A", "C", 0.01), Branch("SB", "S", "B", 0.02), Branch("BC", "B", "C", 0.02)]
    return ["S", "A", "B", "C"], branches


def test_meshed_flow_splits_by_impedance():
    buses, branches = ring()
    r = solve(buses, branches, "S", {"C": -90.0})
    assert r.flows["SA"] == pytest.approx(60.0)
    assert r.flows["SB"] == pytest.approx(30.0)
    assert r.flows["AC"] == pytest.approx(60.0)


def test_islanded_bus_gets_nothing():
    buses, branches = ring()
    r = solve(buses + ["D"], branches, "S", {"C": -90.0, "D": -10.0})
    assert "D" not in r.island
    assert sum(r.flows[k] for k in ("SA", "SB")) == pytest.approx(90.0)


def test_lodf_matches_exact_resolve_on_mesh():
    g = generate(64)
    branches = [Branch(l["id"], l["src"], l["dst"], l["impedance_pu"]) for l in g.lines]
    buses = [b["id"] for b in g.buses]
    inj = {b["id"]: -b["load_mw"] for b in g.buses}
    net = Network(buses, branches, g.source)
    keys, post, bridges = net.n1_flows(net.flows(inj))
    for k_i, k in enumerate(keys[:20]):
        if k in bridges:
            continue
        exact = Network(buses, [b for b in branches if b.key != k], g.source).flows(inj)
        for l_i, l in enumerate(keys):
            if l != k:
                assert post[l_i, k_i] == pytest.approx(exact[l], abs=1e-6)


def test_bridge_outage_has_no_lodf():
    net = Network(["S", "A"], [Branch("SA", "S", "A", 0.01)], "S")
    assert net.lodf("SA") is None


def meshed_snapshot():
    """Demo-style snapshot with a second corridor: T1 -> T5 tie in parallel with T1..T5."""
    circuits = []
    for i, (a, b) in enumerate([("T1", "T2"), ("T2", "T3"), ("T3", "T4"), ("T4", "T5"), ("T5", "SUB")]):
        circuits.append({"span_id": f"TX_{880 + i}", "circuit": "UPPER_PRIMARY", "src": a, "dst": b,
                         "impedance_pu": 0.01, "limit_mw": 900.0, "status": "ENERGIZED"})
    circuits.append({"span_id": "TX_TIE", "circuit": "UPPER_PRIMARY", "src": "T1", "dst": "T5",
                     "impedance_pu": 0.04, "limit_mw": 300.0, "status": "ENERGIZED"})
    return {
        "circuits": circuits,
        "feeds": [{"src": "HYDRO", "dst": "T1"}],
        "substations": [{"id": "SUB", "capacity_mw": 1000.0}],
        "consumers": [{"id": "LOAD", "substation_id": "SUB", "mw_demand": 600.0, "energized": True, "priority_tier": 2}],
        "bess": [],
        "state_version": 1,
    }


def test_meshed_corridor_shares_flow_and_reports_relief_options():
    s = meshed_snapshot()
    r = project_loading(s)
    flows = {c["span_id"]: c["flow_mw"] for c in r["circuits"]}
    # Four 0.01 segments (0.04 total) in parallel with one 0.04 tie: an even split.
    assert flows["TX_880"] == pytest.approx(300.0)
    assert flows["TX_TIE"] == pytest.approx(300.0)
    assert flows["TX_884"] == pytest.approx(600.0)
    r = project_loading(s, [("TX_881", "UPPER_PRIMARY")])
    assert flows["TX_TIE"] < next(c["flow_mw"] for c in r["circuits"] if c["span_id"] == "TX_TIE")
    tie = next(o for o in r["overloads"] if o["element"] == "TX_TIE:UPPER_PRIMARY")
    assert tie["relief_options"][0]["substation_id"] == "SUB"
    assert r["required_relief_mw"] == pytest.approx(600.0 - 285.0)


def test_overrides_apply_bess_and_shedding(snapshot):
    r = project_loading(snapshot, [("TX_882", "UPPER_PRIMARY")],
                        overrides={"bess_injection_mw": {"BESS_METRO_01": 45.0}, "deenergized_consumers": ["IND_METRO_12"]})
    assert r["outcome"] == "WITHIN_LIMITS"
    assert r["overrides"]["deenergized_consumers"] == ["IND_METRO_12"]


def test_overrides_are_validated(snapshot):
    from graph.client import GraphDataError
    with pytest.raises(GraphDataError):
        project_loading(snapshot, overrides={"bess_injection_mw": {"BESS_METRO_01": 500.0}})
    with pytest.raises(GraphDataError):
        project_loading(snapshot, overrides={"deenergized_consumers": ["GHOST"]})
