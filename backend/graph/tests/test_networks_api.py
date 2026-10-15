"""HTTP contract for /api/networks (real FalkorDB, isolated graphs and registry)."""
import uuid

import pytest
from fastapi.testclient import TestClient

import backend.graph.networks as nw
from backend import graph
from backend.main import app


@pytest.fixture
def api(live_client, monkeypatch):
    monkeypatch.setattr(nw, "REGISTRY_GRAPH", f"gridpulse_registry_api_{uuid.uuid4().hex[:8]}")
    previous = graph.get_client()
    graph.set_client(graph.GraphClient(graph.GraphSettings(**{**live_client.settings.__dict__,
                                                               "graph_name": f"gridpulse_api_{uuid.uuid4().hex[:8]}"})))
    with TestClient(app) as c:
        yield c
    graph.set_client(previous)


def login(c, user):
    token = c.post("/api/auth/login", json={"username": user, "password": "gridpulse2026", "role": "operator"}).json()["access_token"]
    return {"Authorization": "Bearer " + token}


def test_network_lifecycle_over_http(api):
    alice, bob = login(api, "ALICE_HTTP"), login(api, "BOB_HTTP")
    assert api.get("/api/networks").status_code == 401
    templates = api.get("/api/networks/templates").json()["templates"]
    assert {t["id"] for t in templates} == {"demo_corridor", "ring", "meshed_city", "ieee118"}

    bad = api.post("/api/networks/validate", headers=alice, json={"spec": {"generators": []}})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "INVALID_REQUEST"

    created = api.post("/api/networks", headers=alice, json={"template": "demo_corridor", "name": "My corridor"})
    assert created.status_code == 201, created.text
    nid = created.json()["id"]
    assert created.json()["name"] == "My corridor"
    assert api.get(f"/api/networks/{nid}", headers=bob).status_code == 404

    sim = api.post(f"/api/networks/{nid}/simulate", headers=alice, json={"cases": [
        {"name": "Lightning", "trip": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}]},
        {"name": "N-1", "type": "n1"}]})
    assert sim.status_code == 200, sim.text
    body = sim.json()
    assert body["cases"][0]["mitigation"]["verdict"] == "SAVED"
    assert body["summary"]["saved_by_mitigation"] == 1

    assert api.post(f"/api/networks/{nid}/trace", headers=alice, json={"origin_id": "TX_882"}).json()["summary"]["downstream_substations"] == ["SUB_METRO"]
    assert api.get(f"/api/networks/{nid}/criticality", headers=alice).status_code == 200
    assert api.get(f"/api/networks/{nid}/reroute", headers=alice, params={"source_id": "HYDRO_ALPINE_01", "target_id": "SUB_METRO"}).json()["outcome"] == "PATHS_FOUND"
    assert api.delete(f"/api/networks/{nid}", headers=alice).json() == {"deleted": nid}
    assert api.get(f"/api/networks/{nid}", headers=alice).status_code == 404


def test_custom_network_trace_with_custom_span_ids(api):
    alice = login(api, "TRACE_HTTP")
    nid = api.post("/api/networks", headers=alice, json={"template": "meshed_city"}).json()["id"]
    r = api.post(f"/api/networks/{nid}/trace", headers=alice,
                 json={"origin_id": "ML1", "assume_tripped": [{"span_id": "ML1", "circuit": "UPPER_PRIMARY"}]})
    assert r.status_code == 200, r.text
    assert r.json()["projection"]["assumed_tripped"] == [{"span_id": "ML1", "circuit": "UPPER_PRIMARY"}]
    assert api.get(f"/api/networks/{nid}/islands", headers=alice).json()["connected"] is True
    assert api.get(f"/api/networks/{nid}/zones", headers=alice).json()["zones"]
    api.delete(f"/api/networks/{nid}", headers=alice)
