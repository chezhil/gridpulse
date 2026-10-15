"""User-built networks: validation, isolation per owner, simulation and mitigation search."""

import uuid

import pytest

from graph import networks as nw
from graph.client import InvalidRequestError, UnknownEntityError
from graph.queries import get_topology, run_contingency_scan, trace_cascade

LIGHTNING = {"name": "Lightning", "trip": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}]}


@pytest.fixture
def reg(live_client, monkeypatch):
    """Isolated registry graph; every network created in the test is deleted afterwards."""
    monkeypatch.setattr(nw, "REGISTRY_GRAPH", f"gridpulse_registry_test_{uuid.uuid4().hex[:8]}")
    owners = []

    def owner(name="OP"):
        o = f"{name}_{uuid.uuid4().hex[:6]}"
        owners.append(o)
        return o
    yield owner
    for o in owners:
        for n in nw.list_networks(o, live_client)["networks"]:
            nw.delete_network(o, n["id"], live_client)
    try:
        nw._registry(live_client)._ensure_graph().delete()
    except Exception:
        pass


# --- validation (no database) ---------------------------------------------


def test_templates_are_valid():
    for tid in nw.TEMPLATES:
        spec, warnings = nw.validate_spec(nw.get_template(tid))
        assert spec["generators"] and spec["lines"]
        assert warnings == [], (tid, warnings)


def test_validation_lists_every_problem():
    bad = {
        "generators": [{"id": "g1", "output_mw": 10, "connects_to": "NOWHERE"}],
        "buses": [{"id": "B1"}, {"id": "B1"}],
        "substations": [{"id": "S1", "capacity_mw": -5}],
        "lines": [{"id": "L1", "from": "B1", "to": "B1", "limit_mw": 100}, {"id": "L2", "from": "B1", "to": "ZZ", "limit_mw": 0}],
        "loads": [{"id": "LD1", "substation": "S1", "mw_demand": 10, "priority_tier": 7}],
        "batteries": [{"id": "BT1", "substation": "S9", "max_discharge_mw": 10}],
    }
    with pytest.raises(InvalidRequestError) as exc:
        nw.validate_spec(bad)
    problems = " | ".join(exc.value.details["problems"])
    for fragment in ("generators[0].id", "already used", "capacity_mw", "from and to must differ",
                     "lines[1]: from/to", "priority_tier", "batteries[0].substation", "connects_to"):
        assert fragment in problems, fragment


def test_validation_warns_about_disconnected_parts():
    spec = nw.get_template("ring")
    spec["buses"].append({"id": "LONELY"})
    _, warnings = nw.validate_spec(spec)
    assert any("LONELY" in w for w in warnings)


# --- stored networks -------------------------------------------------------


def test_create_list_get_replace_delete_and_isolation(reg, live_client):
    alice, bob = reg("ALICE"), reg("BOB")
    net = nw.create_network(alice, nw.get_template("ring"), live_client)
    nid = net["id"]
    assert net["counts"] == {"nodes": 15, "edges": 15}
    assert [n["id"] for n in nw.list_networks(alice, live_client)["networks"]] == [nid]
    assert nw.list_networks(bob, live_client)["networks"] == []
    with pytest.raises(UnknownEntityError):
        nw.get_network(bob, nid, live_client)
    with pytest.raises(UnknownEntityError):
        nw.simulate(bob, nid, [{"name": "x"}], base=live_client)

    spec = net["spec"]
    spec["loads"].append({"id": "NEW_SCHOOL", "substation": "SUB_SOUTH", "mw_demand": 20, "priority_tier": 1, "category": "HOSPITAL"})
    updated = nw.replace_network(alice, nid, spec, live_client)
    assert updated["counts"]["nodes"] == 16
    ids = {n["id"] for n in get_topology(nw.owned_client(alice, nid, live_client))["nodes"]}
    assert "NEW_SCHOOL" in ids and "HOSP_ST_JUDE" not in ids  # its own graph, not the demo grid

    nw.delete_network(alice, nid, live_client)
    assert nw.list_networks(alice, live_client)["networks"] == []
    with pytest.raises(UnknownEntityError):
        nw.get_network(alice, nid, live_client)


def test_demo_template_reproduces_canonical_results(reg, live_client):
    owner = reg()
    nid = nw.create_network(owner, nw.get_template("demo_corridor"), live_client)["id"]
    r = nw.simulate(owner, nid, [LIGHTNING, {"name": "Heatwave", "demand_scale": 1.2},
                                 {"name": "Both TX_884", "trip": [{"span_id": "TX_884", "circuit": "UPPER_PRIMARY"},
                                                                  {"span_id": "TX_884", "circuit": "LOWER_BACKUP"}]},
                                 {"name": "N-1", "type": "n1"}], base=live_client)
    lightning, heat, island, n1 = r["cases"]
    assert lightning["required_relief_mw"] == pytest.approx(80.0)
    assert lightning["mitigation"]["verdict"] == "SAVED"
    assert lightning["mitigation"]["plan"]["trip_breakers"] == ["BRK_IND_12"]
    # Minimal battery: 80 MW needed, 62 MW shed, so about 18 MW from the battery.
    assert lightning["mitigation"]["plan"]["bess_dispatch"]["BESS_METRO_01"] == pytest.approx(18.0, abs=0.6)
    assert heat["overloads"][0]["element"] == "SUB_METRO" and heat["mitigation"]["verdict"] == "SAVED"
    assert island["outcome"] == "ISLANDING_PREDICTED" and island["mitigation"]["verdict"] == "NOT_SAVEABLE"
    assert all(t["supplied"] is False for t in island["tier1"])
    assert n1["n1_secure"] is False and len(n1["critical_outages"]) == 9
    # The simulator never writes: the stored network is unchanged afterwards.
    client = nw.owned_client(owner, nid, live_client)
    assert run_contingency_scan(write_back=False, client=client)["base_outcome"] == "WITHIN_LIMITS"


def test_meshed_city_islands_and_generator_limits(reg, live_client):
    owner = reg()
    nid = nw.create_network(owner, nw.get_template("meshed_city"), live_client)["id"]
    r = nw.simulate(owner, nid, [{"name": "Base"}, {"name": "Cut coast", "trip": [{"span_id": "ML1"}, {"span_id": "ML2"}]},
                                 {"name": "Lose FEED_A", "trip": [{"span_id": "FEED_A"}]}], base=live_client)
    base, cut, feed = r["cases"]
    assert base["outcome"] == "WITHIN_LIMITS"
    gens = {g["id"]: g for g in base["generators"]}
    assert gens["GEN_SOLAR"]["role"] == "FIXED" and gens["GEN_SOLAR"]["output_mw"] == pytest.approx(150.0)
    assert gens["GEN_COAST"]["output_mw"] == pytest.approx(sum(s["net_demand_mw"] for s in base["substations"]) - 150.0)
    # Solar island keeps every substation supplied but is far over capacity.
    assert cut["islanded_substations"] == []
    assert any(o["kind"] == "GENERATOR" and o["element"] == "GEN_SOLAR" for o in cut["overloads"])
    assert cut["mitigation"]["verdict"] == "NOT_SAVEABLE"
    assert feed["islanded_substations"] == ["SUB_A"]
    assert {t["id"]: t["supplied"] for t in feed["tier1"]} == {"HOSPITAL_A": False}


def test_engine_features_run_on_custom_networks(reg, live_client):
    owner = reg()
    nid = nw.create_network(owner, nw.get_template("ring"), live_client)["id"]
    client = nw.owned_client(owner, nid, live_client)
    t = trace_cascade("R1", max_hops=8, client=client)
    assert set(t["summary"]["downstream_substations"]) == {"SUB_NORTH", "SUB_SOUTH"}
    scan = run_contingency_scan(client=client)
    assert "RL1:UPPER_PRIMARY" in scan["critical_outages"]


def test_simulate_rejects_bad_cases(reg, live_client):
    owner = reg()
    nid = nw.create_network(owner, nw.get_template("ring"), live_client)["id"]
    for bad in ([], [{"type": "chaos"}], [{"trip": [{"span_id": "RL1", "circuit": "MIDDLE"}]}], [{"bogus": 1}]):
        with pytest.raises(InvalidRequestError):
            nw.simulate(owner, nid, bad, base=live_client)
    from graph.client import GraphDataError
    with pytest.raises(GraphDataError):
        nw.simulate(owner, nid, [{"trip": [{"span_id": "NOPE"}]}], base=live_client)


def test_ieee118_matches_pandapower_reference():
    """GridPulse's dc-pf/1 flows on the public IEEE 118-bus case vs pandapower rundcpp (stored reference)."""
    import json
    from pathlib import Path

    from graph.thermal import project_loading
    data = json.loads((Path(nw.__file__).parent / "data" / "ieee118.json").read_text())
    p = project_loading(nw.snapshot_from_spec(data["spec"]))
    assert p["outcome"] == "WITHIN_LIMITS"
    ours = {c["span_id"]: c["flow_mw"] * (-1 if c["direction"] == "REVERSE" else 1) for c in p["circuits"]}
    assert len(data["reference_flows_mw"]) == 186
    for branch, ref in data["reference_flows_mw"].items():
        assert ours[branch] == pytest.approx(ref, abs=0.01), branch
