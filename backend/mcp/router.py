"""
GridPulse SCADA MCP FastAPI Router
Provides REST and WebSocket endpoints for MCP tools, autonomous interceptor, and telemetry streaming.
Ready for Jyothi to mount cleanly into `backend/main.py`.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query, Path
from typing import Dict, Any, List, Optional

from backend.mcp.schemas import (
    BlastRadiusRequest,
    BlastRadiusResult,
    MitigationAssetsResult,
    TripBreakerRequest,
    TripBreakerResult,
    DispatchBessRequest,
    DispatchBessResult,
    AuditRecord,
    SUBSTATION_ID,
)
from backend.mcp.server import (
    falkordb_trace_blast_radius,
    falkordb_find_mitigation_assets,
    scada_trip_breaker,
    scada_dispatch_bess,
    get_grid_model,
)
from backend.mcp.interceptor import get_interceptor
from backend.mcp.ws_manager import get_ws_manager

router = APIRouter(tags=["SCADA MCP"])


@router.websocket("/ws/telemetry")
async def websocket_telemetry_endpoint(websocket: WebSocket):
    """
    Real-time SCADA telemetry WebSocket feed.
    Pushes an initial state snapshot on connect and streams live updates as scenarios evolve.
    """
    ws_manager = get_ws_manager()
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep socket alive and allow incoming client pings or control messages
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except (WebSocketDisconnect, Exception):
        await ws_manager.disconnect(websocket)


@router.get("/api/mcp/status")
async def get_mcp_status() -> Dict[str, Any]:
    """Returns current status of MCP tools, interceptor, and physical grid model."""
    interceptor = get_interceptor()
    model = get_grid_model()
    payload = interceptor.ws_manager.get_current_payload()

    return {
        "status": "ONLINE",
        "active_run_id": interceptor.active_run_id,
        "is_intercepting": interceptor.active_task is not None and not interceptor.active_task.done(),
        "bess": {
            "soc_pct": model.bess_soc_pct,
            "active_mw": model.bess_active_mw,
            "status": model.bess_status,
        },
        "substation": {
            "load_mw": model.substation_load_mw,
            "capacity_mw": model.substation_capacity_mw,
            "status": model.substation_status,
        },
        "breakers": model.breakers,
        "current_scenario": payload.scenario.model_dump(),
    }


@router.post("/api/mcp/tools/falkordb_trace_blast_radius", response_model=BlastRadiusResult)
async def tool_trace_blast_radius(req: BlastRadiusRequest) -> BlastRadiusResult:
    """Executes GraphBLAS multi-hop cascade blast radius trace."""
    return falkordb_trace_blast_radius(origin_node_id=req.origin_node_id, max_hops=req.max_hops)


@router.post("/api/mcp/tools/falkordb_find_mitigation_assets", response_model=MitigationAssetsResult)
async def tool_find_mitigation_assets(
    substation_id: str = Query(default=SUBSTATION_ID),
    required_mw: float = Query(default=85.0),
) -> MitigationAssetsResult:
    """Discovers available BESS inverters and Tier-3 loads within 2 hops."""
    return falkordb_find_mitigation_assets(substation_id=substation_id, required_mw=required_mw)


@router.post("/api/mcp/tools/scada_trip_breaker", response_model=TripBreakerResult)
async def tool_trip_breaker(req: TripBreakerRequest) -> TripBreakerResult:
    """Actuates breaker trip with mandatory Tier-1 shielding safety validation."""
    return scada_trip_breaker(
        breaker_id=req.breaker_id,
        reason_code=req.reason_code,
        cypher_audit_hash=req.cypher_audit_hash,
    )


@router.post("/api/mcp/tools/scada_dispatch_bess", response_model=DispatchBessResult)
async def tool_dispatch_bess(req: DispatchBessRequest) -> DispatchBessResult:
    """Dispatches BESS inverter injection with SOC and capacity guardrails."""
    return scada_dispatch_bess(
        bess_id=req.bess_id,
        inject_mw=req.inject_mw,
        target_circuit=req.target_circuit,
    )


@router.get("/api/mcp/audit", response_model=List[AuditRecord])
async def get_mcp_audit_records() -> List[AuditRecord]:
    """Returns immutable, timestamped SCADA MCP action audit trail."""
    interceptor = get_interceptor()
    return interceptor.audit_records


@router.post("/api/mcp/intercept/{scenario_id}")
async def trigger_mcp_interceptor(scenario_id: int = Path(..., ge=1, le=3)) -> Dict[str, Any]:
    """Initiates autonomous interceptor for Scenario 1 (Lightning), 2 (Wildfire), or 3 (Heatwave)."""
    interceptor = get_interceptor()
    return await interceptor.trigger_scenario(scenario_id=scenario_id)


@router.post("/api/mcp/reset")
async def reset_mcp_grid() -> Dict[str, Any]:
    """Resets grid state to nominal 500kV flow and clears active scenario execution."""
    interceptor = get_interceptor()
    return await interceptor.reset()
