"""GridPulse FalkorDB graph engine.

Integration surface (see README.md in this directory):

* ``router``                      FastAPI router for /api/graph/*
* ``init_graph(reset=False)``     startup hook: connect with retries and seed if empty
* ``reset_grid()``                restore nominal topology/state (POST /api/sim/reset)
* ``apply_state_update(update)``  simulator -> graph state sync
* ``trace_cascade(...)``          blast radius + loading projection
* ``find_mitigation_assets(...)`` BESS and Tier-3 load discovery
* ``check_breaker(breaker_id)``   breaker trip eligibility (Tier-1 guard)
* ``verify_tier1_protection()``   Tier-1 energisation / risk audit
* ``evaluate_plan(plan)``         what-if check of a mitigation plan (no actuation)
* ``run_contingency_scan()``      N-1 security scan, tags circuits in the graph
* ``criticality_ranking()``       betweenness centrality of grid buses
* ``reroute_paths()``             k lowest-impedance energised paths
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .client import (
    GraphClient,
    GraphDataError,
    GraphError,
    GraphNotSeededError,
    GraphQueryError,
    GraphSettings,
    GraphUnavailableError,
    InvalidRequestError,
    UnknownEntityError,
    get_client,
    set_client,
)
from .evidence import evidence_hash, verify_evidence
from . import memory
from .algorithms import connected_components, criticality_ranking, reroute_paths, zones
from .queries import (
    check_breaker,
    evaluate_plan,
    run_contingency_scan,
    find_mitigation_assets,
    get_topology,
    read_grid_snapshot,
    trace_cascade,
    verify_tier1_protection,
)
from .router import router
from .seed_grid import ensure_seeded, reset_grid, seed_grid
from .sync import apply_state_update, read_state_version
from .thermal import project_loading


def init_graph(reset: bool = False, client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Connect (bounded retries) and make sure the topology exists.

    ``reset=True`` restores nominal state; the default keeps live state if the
    graph is already seeded.
    """
    client = client or get_client()
    client.connect()
    return reset_grid(client) if reset else ensure_seeded(client)


__all__ = [
    "GraphClient",
    "GraphDataError",
    "GraphError",
    "GraphNotSeededError",
    "GraphQueryError",
    "GraphSettings",
    "GraphUnavailableError",
    "InvalidRequestError",
    "UnknownEntityError",
    "apply_state_update",
    "check_breaker",
    "connected_components",
    "memory",
    "zones",
    "criticality_ranking",
    "evaluate_plan",
    "reroute_paths",
    "run_contingency_scan",
    "ensure_seeded",
    "evidence_hash",
    "find_mitigation_assets",
    "get_client",
    "get_topology",
    "init_graph",
    "project_loading",
    "read_grid_snapshot",
    "read_state_version",
    "reset_grid",
    "router",
    "seed_grid",
    "set_client",
    "trace_cascade",
    "verify_evidence",
    "verify_tier1_protection",
]
