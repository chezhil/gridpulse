"""Request bodies for the HTTP SCADA tool adapters in backend/mcp/router.py.

Canonical IDs and topology live in backend/graph/topology.py; responses are
the graph engine's evidence dicts, so no response models are duplicated here.
"""

from typing import Literal

from pydantic import BaseModel, Field


class BlastRadiusRequest(BaseModel):
    # A span origin starts at its downstream tower, so TX_882 reaches SUB_METRO in 6 hops.
    origin_node_id: str = "TX_882"
    max_hops: int = Field(10, ge=1, le=12)


class TripBreakerRequest(BaseModel):
    breaker_id: str
    reason_code: str
    cypher_audit_hash: str = ""


class DispatchBessRequest(BaseModel):
    bess_id: str
    inject_mw: float = Field(gt=0, le=60, allow_inf_nan=False)
    target_circuit: Literal["LOWER_BACKUP"] = "LOWER_BACKUP"
