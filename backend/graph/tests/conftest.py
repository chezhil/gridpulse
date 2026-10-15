import os
import sys
import uuid
from pathlib import Path

import pytest

# Make `import graph` work when pytest runs from the repo root or backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph import topology as topo  # noqa: E402
from graph.client import GraphClient, GraphSettings, GraphUnavailableError  # noqa: E402
from graph.seed_grid import reset_grid  # noqa: E402


def nominal_snapshot():
    """The seeded nominal state, built without FalkorDB (for pure model tests)."""
    return {
        "circuits": [
            {"span_id": r["id"], "circuit": r["circuit"], "rel_type": r["rel_type"], "src": r["from"], "dst": r["to"], **r["props"]}
            for r in topo.span_rows()
        ],
        "feeds": [{"src": r["from"], "dst": r["to"], "output_mw": 840.0} for r in topo.feeds_rows()],
        "substations": [dict(s) for s in topo.SUBSTATIONS],
        "consumers": [
            {**c, "substation_id": "SUB_METRO", "feeder_id": r["props"]["feeder_id"]}
            for c, r in zip(topo.CONSUMERS, topo.distributes_rows())
        ],
        "bess": [{**b, "substation_id": "SUB_METRO"} for b in topo.BESS_UNITS],
        "state_version": 1,
    }


@pytest.fixture
def snapshot():
    return nominal_snapshot()


@pytest.fixture(scope="session")
def falkordb_settings():
    base = GraphSettings.from_env()
    return GraphSettings(**{**base.__dict__, "graph_name": f"gridpulse_test_{uuid.uuid4().hex[:8]}", "connect_attempts": 1})


@pytest.fixture(scope="session")
def live_client(falkordb_settings):
    client = GraphClient(falkordb_settings)
    try:
        client.connect()
    except GraphUnavailableError as exc:
        if os.getenv("GRIDPULSE_REQUIRE_FALKORDB"):
            raise
        pytest.skip(f"FalkorDB not reachable ({exc.message}); integration tests skipped")
    yield client
    try:
        client._graph.delete()
    except Exception:
        pass


@pytest.fixture
def graph(live_client):
    """A freshly reset canonical graph for each test."""
    reset_grid(live_client)
    return live_client
