"""Integration tests against a real FalkorDB (skipped when unreachable).

Each test gets a freshly reset graph in an isolated, randomly named graph key.
Set GRIDPULSE_REQUIRE_FALKORDB=1 to fail instead of skipping.
"""

import pytest

from graph import topology as topo
from graph.client import (
    GraphClient,
    GraphNotSeededError,
    GraphSettings,
    GraphUnavailableError,
    InvalidRequestError,
    UnknownEntityError,
)
from graph.evidence import verify_evidence
from graph.queries import check_breaker, find_mitigation_assets, get_topology, trace_cascade, verify_tier1_protection
from graph.seed_grid import count_topology, reset_grid, seed_grid
from graph.sync import apply_state_update, read_state_version

EXPECTED = {"nodes": topo.EXPECTED_NODE_COUNT, "edges": topo.EXPECTED_EDGE_COUNT}
LIGHTNING_TRIP = {"span_id": "TX_882", "circuit": "UPPER_PRIMARY", "status": "TRIPPED", "flow_mw": 0.0}


# --- seeding ---------------------------------------------------------------


def test_repeat_seeding_creates_no_duplicates(graph):
    for _ in range(3):
        seed_grid(graph)
    assert count_topology(graph) == EXPECTED == {"nodes": 16, "edges": 24}
    dupes = graph.query(
        "dupes", "MATCH (n) WHERE n.id IS NOT NULL WITH n.id AS id, count(n) AS c WHERE c > 1 RETURN id"
    ).rows
    assert dupes == []


def test_reset_removes_injected_duplicates_and_strays(graph):
    graph.query(
        "inject",
        "MATCH (a:Tower {id:'T3'}), (b:Tower {id:'T4'}) "
        "CREATE (a)-[:SPAN {id:'TX_882', circuit:'UPPER_PRIMARY', status:'ENERGIZED'}]->(b), "
        "(:Tower {id:'T3'}), (:Tower {id:'T99'}), (a)-[:SPAN {id:'TX_999', circuit:'UPPER_PRIMARY'}]->(b)",
        read_only=False,
    )
    assert count_topology(graph) != EXPECTED
    reset_grid(graph)
    assert count_topology(graph) == EXPECTED
    spans = get_topology(graph)["spans"]
    assert [s["id"] for s in spans] == [f"TX_{n}" for n in range(880, 889)]


def test_span_mapping(graph):
    spans = {s["id"]: s for s in get_topology(graph)["spans"]}
    assert (spans["TX_882"]["from"], spans["TX_882"]["to"]) == ("T3", "T4")
    assert (spans["TX_880"]["from"], spans["TX_880"]["to"]) == ("T1", "T2")
    assert (spans["TX_888"]["from"], spans["TX_888"]["to"]) == ("T9", "SUB_METRO")
    assert all(set(s["circuits"]) == {"UPPER_PRIMARY", "LOWER_BACKUP"} for s in spans.values())


def test_canonical_ids_present(graph):
    ids = {n["id"] for n in get_topology(graph)["nodes"]}
    assert {"HYDRO_ALPINE_01", "SUB_METRO", "BESS_METRO_01", "HOSP_ST_JUDE"} <= ids
    assert {f"T{i}" for i in range(1, 10)} <= ids


# --- lightning trace -------------------------------------------------------


def test_lightning_trace_reaches_downstream_assets(graph):
    r = trace_cascade("TX_882", assume_tripped=[("TX_882", "UPPER_PRIMARY")], client=graph)
    assert r["origin"]["kind"] == "SPAN"
    assert (r["origin"]["from"], r["origin"]["to"]) == ("T3", "T4")
    hops = {n["id"]: n["hops"] for n in r["reachability"]["nodes"]}
    assert hops == {"T4": 0, "T5": 1, "T6": 2, "T7": 3, "T8": 4, "T9": 5, "SUB_METRO": 6}
    assert "T3" not in hops and "HYDRO_ALPINE_01" not in hops
    assert set(r["summary"]["downstream_consumer_ids"]) == {"HOSP_ST_JUDE", "RES_VALLEY_SUBURBS", "COM_METRO_CORE", "IND_METRO_12"}
    assert [b["id"] for b in r["impact"]["bess"]] == ["BESS_METRO_01"]
    assert r["summary"]["projection_outcome"] == "OVERLOAD_PREDICTED"
    assert r["summary"]["required_relief_mw"] == pytest.approx(80.0)
    assert r["summary"]["tier1_at_risk"] == ["HOSP_ST_JUDE"]
    sub_path = next(n for n in r["reachability"]["nodes"] if n["id"] == "SUB_METRO")["path"]
    assert sub_path == ["T4", "T5", "T6", "T7", "T8", "T9", "SUB_METRO"]


def test_trace_uses_synced_state_not_canned_result(graph):
    before = trace_cascade("TX_882", client=graph)
    assert before["summary"]["projection_outcome"] == "WITHIN_LIMITS"
    v = apply_state_update({"spans": [LIGHTNING_TRIP], "source": "test"}, graph)["state_version"]
    after = trace_cascade("TX_882", client=graph)
    assert after["state_version"] == v
    assert after["summary"]["projection_outcome"] == "OVERLOAD_PREDICTED"


def test_mitigated_state_projects_within_limits(graph):
    apply_state_update(
        {
            "spans": [LIGHTNING_TRIP],
            "bess": [{"id": "BESS_METRO_01", "active_injection_mw": 45.0, "status": "DISCHARGING"}],
            "consumers": [{"id": "IND_METRO_12", "energized": False}],
        },
        graph,
    )
    r = trace_cascade("TX_882", client=graph)
    assert r["summary"]["projection_outcome"] == "WITHIN_LIMITS"
    assert verify_tier1_protection("SUB_METRO", graph)["outcome"] == "ALL_PROTECTED"


def test_hop_bound_is_reported(graph):
    r = trace_cascade("T3", max_hops=6, client=graph)
    assert r["summary"]["downstream_substations"] == []
    assert r["reachability"]["beyond_max_hops"] == ["SUB_METRO"]
    assert trace_cascade("T3", max_hops=7, client=graph)["summary"]["downstream_substations"] == ["SUB_METRO"]


def test_leaf_origin_has_no_downstream(graph):
    r = trace_cascade("HOSP_ST_JUDE", client=graph)
    assert r["outcome"] == "NO_DOWNSTREAM_ASSETS"
    assert r["summary"]["downstream_node_count"] == 0


def test_invalid_origins(graph):
    with pytest.raises(UnknownEntityError):
        trace_cascade("TX_999", client=graph)
    with pytest.raises(InvalidRequestError):
        trace_cascade("tx_882; MATCH (n) DELETE n", client=graph)
    with pytest.raises(InvalidRequestError):
        trace_cascade("TX_882", max_hops=50, client=graph)
    with pytest.raises(InvalidRequestError):
        trace_cascade("TX_882", assume_tripped=[("TX_882", "MIDDLE")], client=graph)


def test_evidence_and_timings(graph):
    r = trace_cascade("TX_882", client=graph)
    assert {e["name"] for e in r["evidence"]} == {"resolve_origin", "trace_reachability", "grid_snapshot", "attached_assets"}
    for e in r["evidence"]:
        assert verify_evidence(e)
        assert e["server_ms"] > 0 and e["roundtrip_ms"] > 0
    assert r["timing"]["graph_server_ms"] == pytest.approx(sum(e["server_ms"] for e in r["evidence"]), abs=1e-3)
    tampered = dict(r["evidence"][0], rows=[])
    assert not verify_evidence(tampered)


# --- mitigation discovery --------------------------------------------------


def test_tier3_discovery_excludes_tier1_and_tier2(graph):
    m = find_mitigation_assets("SUB_METRO", 80.0, client=graph)
    assert [c["id"] for c in m["sheddable_loads"]["eligible"]] == ["IND_METRO_12"]
    assert m["sheddable_loads"]["eligible"][0]["breaker_id"] == "BRK_IND_12"
    excluded = {c["id"]: c["reasons"] for c in m["sheddable_loads"]["excluded"]}
    assert excluded["HOSP_ST_JUDE"] == ["PRIORITY_TIER_1"]
    assert "PRIORITY_TIER_2" in excluded["COM_METRO_CORE"]
    assert m["totals"] == {"bess_available_mw": 60.0, "sheddable_mw": 62.0, "combined_mw": 122.0}
    assert m["meets_requirement"] is True


def test_bess_eligibility_checks_soc_and_headroom(graph):
    apply_state_update({"bess": [{"id": "BESS_METRO_01", "soc_pct": 35.0}]}, graph)
    m = find_mitigation_assets("SUB_METRO", 45.0, client=graph)
    assert m["bess"]["eligible"] == []
    assert m["bess"]["ineligible"][0]["reasons"] == ["SOC_AT_OR_BELOW_MINIMUM"]

    apply_state_update({"bess": [{"id": "BESS_METRO_01", "soc_pct": 70.0, "active_injection_mw": 60.0, "status": "DISCHARGING"}]}, graph)
    m = find_mitigation_assets("SUB_METRO", 45.0, client=graph)
    assert m["bess"]["ineligible"][0]["reasons"] == ["NO_DISCHARGE_HEADROOM"]

    apply_state_update({"bess": [{"id": "BESS_METRO_01", "active_injection_mw": 20.0}]}, graph)
    m = find_mitigation_assets("SUB_METRO", 45.0, client=graph)
    assert m["bess"]["eligible"][0]["available_mw"] == pytest.approx(40.0)


def test_shed_load_already_open_is_not_offered(graph):
    apply_state_update({"consumers": [{"id": "IND_METRO_12", "energized": False}]}, graph)
    m = find_mitigation_assets("SUB_METRO", 80.0, client=graph)
    assert m["sheddable_loads"]["eligible"] == []
    assert m["meets_requirement"] is False
    assert check_breaker("BRK_IND_12", graph)["reasons"] == ["ALREADY_OPEN"]


def test_breaker_guard(graph):
    ok = check_breaker("BRK_IND_12", graph)
    assert ok["trip_allowed"] is True and ok["shed_mw"] == pytest.approx(62.0)
    hosp = check_breaker("BRK_HOSP_01", graph)
    assert hosp["trip_allowed"] is False and "SERVES_TIER_1" in hosp["reasons"]
    assert check_breaker("BRK_NOPE_99", graph)["reasons"] == ["UNKNOWN_BREAKER"]


def test_unknown_substation(graph):
    with pytest.raises(UnknownEntityError):
        find_mitigation_assets("SUB_NOWHERE", 10.0, client=graph)
    with pytest.raises(UnknownEntityError):
        verify_tier1_protection("SUB_NOWHERE", graph)


def test_tier1_deenergized_is_reported(graph):
    apply_state_update({"consumers": [{"id": "HOSP_ST_JUDE", "energized": False}]}, graph)
    assert verify_tier1_protection(client=graph)["outcome"] == "TIER1_DEENERGIZED"


# --- sync and reset --------------------------------------------------------


def test_invalid_updates_write_nothing(graph):
    v0 = read_state_version(graph)
    bad_updates = [
        {"spans": [{"span_id": "TX_999", "circuit": "UPPER_PRIMARY", "status": "TRIPPED"}]},
        {"spans": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY", "status": "MELTED"}]},
        {"bess": [{"id": "BESS_METRO_01", "active_injection_mw": 500.0}]},
        {"consumers": [{"id": "T3", "energized": False}]},
        {"consumers": [{"id": "IND_METRO_12", "priority_tier": 1}]},
        {"towers": [{"id": "T3", "status": "FAULT"}], "consumers": [{"id": "GHOST", "energized": False}]},
    ]
    for update in bad_updates:
        with pytest.raises((InvalidRequestError, UnknownEntityError)):
            apply_state_update(update, graph)
    assert read_state_version(graph) == v0
    t3 = next(n for n in get_topology(graph)["nodes"] if n["id"] == "T3")
    assert t3["properties"]["status"] == "NOMINAL"


def test_reset_restores_nominal_and_advances_version(graph):
    v1 = apply_state_update(
        {
            "spans": [LIGHTNING_TRIP],
            "towers": [{"id": "T3", "status": "FAULT"}],
            "bess": [{"id": "BESS_METRO_01", "active_injection_mw": 45.0, "status": "DISCHARGING"}],
            "consumers": [{"id": "IND_METRO_12", "energized": False}],
        },
        graph,
    )["state_version"]
    reset = reset_grid(graph)
    assert reset["state_version"] > v1
    assert count_topology(graph) == EXPECTED
    topology = get_topology(graph)
    props = {n["id"]: n["properties"] for n in topology["nodes"]}
    assert props["T3"]["status"] == "NOMINAL"
    assert props["BESS_METRO_01"]["active_injection_mw"] == 0.0
    assert props["IND_METRO_12"]["energized"] is True
    tx882 = next(s for s in topology["spans"] if s["id"] == "TX_882")
    assert tx882["circuits"]["UPPER_PRIMARY"]["status"] == "ENERGIZED"
    assert trace_cascade("TX_882", client=graph)["summary"]["projection_outcome"] == "WITHIN_LIMITS"


# --- failure modes ---------------------------------------------------------


def test_unavailable_falkordb_is_reported():
    client = GraphClient(GraphSettings(host="127.0.0.1", port=1, connect_attempts=2, retry_backoff_s=0.01))
    with pytest.raises(GraphUnavailableError) as exc:
        client.connect()
    assert exc.value.details["port"] == 1
    with pytest.raises(GraphUnavailableError):
        trace_cascade("TX_882", client=client)


def test_empty_graph_is_not_seeded(live_client, falkordb_settings):
    empty = GraphClient(GraphSettings(**{**falkordb_settings.__dict__, "graph_name": falkordb_settings.graph_name + "_empty"}))
    with pytest.raises(GraphNotSeededError):
        get_topology(empty)
    with pytest.raises((GraphNotSeededError, UnknownEntityError)):
        trace_cascade("TX_882", client=empty)
