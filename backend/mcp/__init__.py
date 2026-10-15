"""
GridPulse SCADA MCP Package
Autonomous Cascading Blackout Interceptor powered by FalkorDB GraphBLAS state.
Exports MCP tools, autonomous interceptor, and WebSocket telemetry broadcaster.
"""

from backend.mcp.schemas import (
    HYDRO_PLANT_ID,
    TOWER_IDS,
    SPAN_IDS,
    SUBSTATION_ID,
    BESS_ID,
    TIER1_HOSPITAL_ID,
    TIER3_BREAKER_ID,
    SPAN_MAPPING,
    ScenarioInfo,
    MetricsInfo,
    SpanStatus,
    TelemetryPayload,
    AuditRecord,
    BlastRadiusRequest,
    BlastRadiusResult,
    MitigationAssetsResult,
    TripBreakerRequest,
    TripBreakerResult,
    DispatchBessRequest,
    DispatchBessResult,
)
from backend.mcp.server import (
    falkordb_trace_blast_radius,
    falkordb_find_mitigation_assets,
    scada_trip_breaker,
    scada_dispatch_bess,
    generate_cypher_audit_hash,
    get_grid_model,
    GroundedGridModel,
)
from backend.mcp.ws_manager import (
    WebSocketTelemetryManager,
    get_ws_manager,
)
from backend.mcp.interceptor import (
    GridPulseInterceptor,
    get_interceptor,
)
from backend.mcp.router import router as mcp_router

__all__ = [
    "HYDRO_PLANT_ID",
    "TOWER_IDS",
    "SPAN_IDS",
    "SUBSTATION_ID",
    "BESS_ID",
    "TIER1_HOSPITAL_ID",
    "TIER3_BREAKER_ID",
    "SPAN_MAPPING",
    "ScenarioInfo",
    "MetricsInfo",
    "SpanStatus",
    "TelemetryPayload",
    "AuditRecord",
    "BlastRadiusRequest",
    "BlastRadiusResult",
    "MitigationAssetsResult",
    "TripBreakerRequest",
    "TripBreakerResult",
    "DispatchBessRequest",
    "DispatchBessResult",
    "falkordb_trace_blast_radius",
    "falkordb_find_mitigation_assets",
    "scada_trip_breaker",
    "scada_dispatch_bess",
    "generate_cypher_audit_hash",
    "get_grid_model",
    "GroundedGridModel",
    "WebSocketTelemetryManager",
    "get_ws_manager",
    "GridPulseInterceptor",
    "get_interceptor",
    "mcp_router",
]
