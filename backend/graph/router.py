"""FastAPI routes for ``/api/graph/*``.

Mount with ``app.include_router(graph.router)``. Errors use FastAPI's
``{"detail": {...}}`` envelope with a stable ``code``:

==========================  ======  ============================================
code                        status  meaning
==========================  ======  ============================================
INVALID_REQUEST             422     bad origin id, hop bound, circuit, threshold
UNKNOWN_ENTITY              404     origin / substation not in the graph
GRAPH_NOT_SEEDED            503     FalkorDB reachable but topology missing
GRAPH_UNAVAILABLE           503     FalkorDB unreachable after bounded retries
GRAPH_QUERY_FAILED          502     FalkorDB rejected or failed a query
GRAPH_DATA_INCOMPLETE       500     required properties missing on graph state
==========================  ======  ============================================
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from . import algorithms, benchmark, memory, queries
from .client import (
    GraphClient,
    GraphDataError,
    GraphError,
    GraphNotSeededError,
    GraphQueryError,
    GraphUnavailableError,
    InvalidRequestError,
    UnknownEntityError,
    get_client,
)
from .sync import read_state_version

router = APIRouter(prefix="/api/graph", tags=["graph"])

_STATUS = {
    InvalidRequestError: 422,
    UnknownEntityError: 404,
    GraphNotSeededError: 503,
    GraphUnavailableError: 503,
    GraphQueryError: 502,
    GraphDataError: 500,
}


def _run(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return fn()
    except GraphError as exc:
        raise HTTPException(status_code=_STATUS.get(type(exc), 500), detail=exc.to_dict()) from exc


class CircuitRef(BaseModel):
    span_id: str = Field(pattern=r"^TX_\d{3}$", examples=["TX_882"])
    circuit: Literal["UPPER_PRIMARY", "LOWER_BACKUP"]


class TraceCascadeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "origin_id": "TX_882",
                    "max_hops": 6,
                    "assume_tripped": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}],
                }
            ]
        }
    )

    origin_id: str = Field(
        validation_alias=AliasChoices("origin_id", "origin_node_id"),
        pattern=r"^[A-Z0-9_]{1,40}$",
        description="Span ID (TX_880..TX_888) or node ID (T1..T9, SUB_METRO, ...).",
    )
    max_hops: int = Field(queries.DEFAULT_MAX_HOPS, ge=1, le=queries.MAX_HOPS_LIMIT)
    assume_tripped: List[CircuitRef] = Field(
        default_factory=list,
        max_length=18,
        description="Hypothetical trips layered on the stored state for the projection. Never written to the graph.",
    )
    threshold_pct: float = Field(queries.DEFAULT_THRESHOLD_PCT, ge=1, le=150)
    overrides: Optional["WhatIfOverrides"] = None


class WhatIfOverrides(BaseModel):
    bess_injection_mw: Dict[str, float] = Field(default_factory=dict, examples=[{"BESS_METRO_01": 45.0}])
    deenergized_consumers: List[str] = Field(default_factory=list, max_length=50, examples=[["IND_METRO_12"]])


class PlanRequest(BaseModel):
    trip_breakers: List[str] = Field(default_factory=list, max_length=20, examples=[["BRK_IND_12"]])
    bess_dispatch: Dict[str, float] = Field(default_factory=dict, examples=[{"BESS_METRO_01": 45.0}])
    threshold_pct: float = Field(queries.DEFAULT_THRESHOLD_PCT, ge=1, le=150)


class BenchmarkRequest(BaseModel):
    size: Literal[1000, 10000, 100000] = 1000
    runs: int = Field(20, ge=1, le=200)
    origins: int = Field(10, ge=1, le=50)
    max_hops: int = Field(6, ge=2, le=16)


TraceCascadeRequest.model_rebuild()


@router.get("/topology")
def get_topology(client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """All GridPulse nodes and relationships, plus a per-span circuit summary."""
    return _run(lambda: queries.get_topology(client))


@router.post("/trace-cascade")
def trace_cascade(body: TraceCascadeRequest, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Downstream blast radius of ``origin_id`` and the loading projection for the current state."""
    return _run(
        lambda: queries.trace_cascade(
            body.origin_id,
            max_hops=body.max_hops,
            assume_tripped=[t.model_dump() for t in body.assume_tripped],
            threshold_pct=body.threshold_pct,
            client=client,
            overrides=body.overrides.model_dump() if body.overrides else None,
        )
    )


@router.post("/contingency")
def contingency(threshold_pct: float = 95.0, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """N-1 scan: trip each energised circuit in turn and re-solve; tags circuits with the result."""
    return _run(lambda: queries.run_contingency_scan(threshold_pct, write_back=True, client=client))


@router.get("/criticality")
def criticality(limit: int = 10, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Buses ranked by betweenness centrality (algo.betweenness), joined with N-1 tags."""
    return _run(lambda: algorithms.criticality_ranking(limit, client))


@router.get("/reroute")
def reroute(
    source_id: str = "HYDRO_ALPINE_01", target_id: str = "SUB_METRO", k: int = 3, client: GraphClient = Depends(get_client)
) -> Dict[str, Any]:
    """Lowest-impedance energised paths (algo.SPpaths), skipping tripped circuits."""
    return _run(lambda: algorithms.reroute_paths(source_id, target_id, k, client=client))


@router.post("/evaluate-plan")
def evaluate_plan(body: PlanRequest, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """What-if check of a mitigation plan against the actuation guards. Never actuates."""
    plan = {"trip_breakers": body.trip_breakers, "bess_dispatch": body.bess_dispatch}
    return _run(lambda: queries.evaluate_plan(plan, body.threshold_pct, client=client))


@router.post("/benchmark")
def run_benchmark(body: BenchmarkRequest, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Measured FalkorDB vs SQLite 6-hop trace on a synthetic meshed grid (results checked equal)."""
    return _run(lambda: benchmark.run_benchmark(body.size, body.runs, body.origins, client, body.max_hops))


@router.get("/islands")
def islands(client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Topological islands via algo.WCC."""
    return _run(lambda: algorithms.connected_components(client))


@router.get("/zones")
def grid_zones(client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Grid zones via algo.labelPropagation."""
    return _run(lambda: algorithms.zones(client))


@router.get("/memory/incidents")
def memory_incidents(limit: int = 50, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Episodic memory: past incidents with their actions and outcomes."""
    return _run(lambda: memory.list_incidents(limit, client))


@router.get("/memory/playbooks")
def memory_playbooks(client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Procedural memory: plans learned per trigger signature, with success/failure counts."""
    return _run(lambda: memory.list_playbooks(client))


@router.get("/memory/recall")
def memory_recall(origin_id: str, hops: int = 2, client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Incidents and playbooks within `hops` grid hops of a span or node."""
    return _run(lambda: memory.recall(origin_id, hops, client=client))


@router.get("/health")
def health(client: GraphClient = Depends(get_client)) -> Dict[str, Any]:
    """Readiness probe: FalkorDB reachable and topology seeded. Never raises."""
    out: Dict[str, Any] = {"graph": client.graph_name, "falkordb": False, "seeded": False, "state_version": None}
    if not client.ping():
        return out
    out["falkordb"] = True
    try:
        out["state_version"] = read_state_version(client)
        out["seeded"] = out["state_version"] is not None
    except GraphError as exc:
        out["error"] = exc.to_dict()
    return out
