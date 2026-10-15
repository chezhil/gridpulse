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

from typing import Any, Callable, Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from . import queries
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
        )
    )


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
