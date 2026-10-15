"""HTTP contract tests for /api/graph/* (integration cases skip without FalkorDB)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph import router as graph_router
from graph.client import GraphClient, GraphSettings, get_client


def make_app(client):
    app = FastAPI()
    app.include_router(graph_router)
    app.dependency_overrides[get_client] = lambda: client
    return TestClient(app)


@pytest.fixture
def api(graph):
    return make_app(graph)


def test_topology(api):
    r = api.get("/api/graph/topology")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"nodes": 16, "edges": 24}
    assert len(body["spans"]) == 9


def test_trace_cascade_lightning(api):
    r = api.post(
        "/api/graph/trace-cascade",
        json={"origin_id": "TX_882", "assume_tripped": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}]},
    )
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["projection_outcome"] == "OVERLOAD_PREDICTED"
    assert summary["downstream_substations"] == ["SUB_METRO"]


def test_trace_accepts_mcp_alias(api):
    r = api.post("/api/graph/trace-cascade", json={"origin_node_id": "T4"})
    assert r.status_code == 200
    assert r.json()["origin"]["kind"] == "NODE"


@pytest.mark.parametrize(
    "body",
    [
        {"origin_id": "TX_882", "max_hops": 0},
        {"origin_id": "TX_882", "max_hops": 13},
        {"origin_id": "tx 882"},
        {"origin_id": "TX_882", "assume_tripped": [{"span_id": "TX_882", "circuit": "SIDEWAYS"}]},
        {},
    ],
)
def test_trace_validation(api, body):
    assert api.post("/api/graph/trace-cascade", json=body).status_code == 422


def test_unknown_origin_is_404(api):
    r = api.post("/api/graph/trace-cascade", json={"origin_id": "TX_999"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "UNKNOWN_ENTITY"


def test_health(api):
    body = api.get("/api/graph/health").json()
    assert body["falkordb"] is True and body["seeded"] is True


def test_unavailable_is_503():
    down = GraphClient(GraphSettings(host="127.0.0.1", port=1, connect_attempts=1, retry_backoff_s=0.01))
    api = make_app(down)
    r = api.get("/api/graph/topology")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "GRAPH_UNAVAILABLE"
    assert api.get("/api/graph/health").json() == {"graph": "gridpulse", "falkordb": False, "seeded": False, "state_version": None}
