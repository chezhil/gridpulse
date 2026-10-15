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
