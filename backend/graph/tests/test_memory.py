"""Graph memory: incidents linked to grid elements, multi-hop recall, playbooks, reset survival."""

import pytest

from graph import memory
from graph.algorithms import connected_components, zones
from graph.client import InvalidRequestError
from graph.seed_grid import count_topology, reset_grid

@pytest.fixture(autouse=True)
def clean_memory(graph):
    """Memory deliberately survives grid resets, so clear it between tests."""
    graph.query("clear_memory", "MATCH (n) WHERE n:Incident OR n:Action OR n:Outcome OR n:Playbook DETACH DELETE n",
                read_only=False)


TRIP = [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}]
PLAN = {"trip_breakers": ["BRK_IND_12"], "bess_dispatch": {"BESS_METRO_01": 45.0}}


def record(graph, status="MITIGATED", origin="TX_882", trips=TRIP, plan=PLAN, **extra):
    return memory.record_incident({"origin_id": origin, "trips": trips, "status": status, "plan": plan,
                                   "breaker_targets": {"BRK_IND_12": "IND_METRO_12"}, **extra}, graph)


def test_incident_is_linked_and_recalled_by_neighbourhood(graph):
    first = record(graph, run_id="R1", required_relief_mw=80.0)
    assert first["linked_to"] == ["T3", "T4"]
    assert first["signature"] == "TRIP:TX_882:UPPER_PRIMARY"
    near = memory.recall("TX_883", hops=2, client=graph)  # T4->T5 is one hop from the incident
    assert [i["id"] for i in near["incidents"]] == [first["incident_id"]]
    assert near["incidents"][0]["actions"][0] == {"kind": "TRIP_BREAKER", "target": "BRK_IND_12", "mw": None}
    assert near["playbooks"][0]["plan"] == PLAN
    assert memory.recall("TX_887", hops=1, client=graph)["incidents"] == []
    targeted = graph.query("t", "MATCH (:Action)-[:TARGETED]->(x) RETURN collect(x.id) AS ids").rows[0]["ids"]
    assert sorted(targeted) == ["BESS_METRO_01", "IND_METRO_12"]


def test_playbook_counts_success_and_failure(graph):
    record(graph)
    record(graph, status="FAILED", plan=None, note="battery offline")
    pb = memory.list_playbooks(graph)["playbooks"][0]
    assert (pb["success_count"], pb["failure_count"]) == (1, 1)
    assert pb["plan"] == PLAN  # a failure never overwrites the last working plan
    assert sorted(pb["applies_to"]) == ["T3", "T4"]


def test_memory_survives_grid_reset(graph):
    record(graph)
    reset_grid(graph)
    assert len(memory.list_incidents(client=graph)["incidents"]) == 1
    assert count_topology(graph) == {"nodes": 16, "edges": 24}


def test_signature_and_validation():
    assert memory.signature_for([], 1.2) == "DEMAND_SURGE"
    assert memory.signature_for([{"span_id": "B", "circuit": "X"}, {"span_id": "A", "circuit": "Y"}]) == "TRIP:A:Y|TRIP:B:X"
    with pytest.raises(InvalidRequestError):
        memory.record_incident({"origin_id": "TX_882", "status": "MAYBE"})


def test_islands_and_zones(graph):
    comps = connected_components(graph)
    assert comps["connected"] is True and comps["components"][0]["has_generator"]
    assert comps["components"][0]["size"] == 11
    z = zones(graph)
    assert sum(zone["size"] for zone in z["zones"]) == 11


# --- learning ---------------------------------------------------------------

def test_suggest_ranks_exact_then_similar_faults(graph):
    record(graph, downstream=["SUB_METRO"], shed_mw=62.0)
    exact = memory.suggest("TX_882", TRIP, client=graph)["suggestions"]
    assert exact[0]["exact"] is True and exact[0]["similarity"] == 1.0 and exact[0]["plan"] == PLAN
    near = memory.suggest("TX_884", [{"span_id": "TX_884", "circuit": "UPPER_PRIMARY"}], client=graph)["suggestions"]
    assert near and near[0]["exact"] is False and near[0]["signature"] == "TRIP:TX_882:UPPER_PRIMARY"
    assert near[0]["hops_away"] == 1 and near[0]["downstream_overlap"] == 1.0 and near[0]["score"] < exact[0]["score"]
    assert memory.suggest("SUB_METRO", [], 1.2, client=graph)["suggestions"] == [] or \
        all(sg["similarity"] < 1.0 for sg in memory.suggest("SUB_METRO", [], 1.2, client=graph)["suggestions"])


def test_failures_lower_confidence_and_retire_playbooks(graph):
    record(graph, downstream=["SUB_METRO"])
    first = memory.suggest("TX_882", TRIP, client=graph)["suggestions"][0]["confidence"]
    record(graph, status="FAILED", plan=None)
    assert memory.suggest("TX_882", TRIP, client=graph)["suggestions"][0]["confidence"] < first
    record(graph, status="FAILED", plan=None)
    pb = memory.list_playbooks(graph)["playbooks"][0]
    assert pb["retired"] is True
    assert memory.suggest("TX_882", TRIP, client=graph)["suggestions"] == []


def test_generalised_reuse_credits_the_source_playbook(graph):
    record(graph, downstream=["SUB_METRO"])
    out = record(graph, origin="TX_884", trips=[{"span_id": "TX_884", "circuit": "UPPER_PRIMARY"}],
                 reused_playbook="TRIP:TX_882:UPPER_PRIMARY", downstream=["SUB_METRO"], steps=1, mitigation_ms=12.5)
    assert out["reused_credit"]["success_count"] == 2
    src = next(p for p in memory.list_playbooks(graph)["playbooks"] if p["signature"] == "TRIP:TX_882:UPPER_PRIMARY")
    assert src["generalized"] == 1
    curve = memory.learning_curve(graph)
    assert curve["summary"]["solved_from_memory"] == 1 and curve["summary"]["generalized_reuses"] == 1
    assert curve["incidents"][-1]["learned_from"] == "TRIP:TX_882:UPPER_PRIMARY" and curve["incidents"][-1]["steps"] == 1
    assert graph.query("r", "MATCH (:Incident)-[:REUSED]->(p:Playbook) RETURN p.id AS id").rows[0]["id"] == "TRIP:TX_882:UPPER_PRIMARY"
