"""Authenticated SCADA tools and aliases into the single simulator pipeline."""
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from backend.auth.router import require_role, SESSIONS, verify_operator_token
from backend.simulator.grid_sim import grid_simulator as sim
from backend.mcp.schemas import BlastRadiusRequest, TripBreakerRequest, DispatchBessRequest
from backend.mcp import server
from backend.mcp.ws_manager import get_ws_manager

router = APIRouter(tags=['SCADA MCP'])
operator = require_role(['operator', 'NERC_OPERATOR', 'SCADA_ADMIN'])


@router.websocket('/ws/telemetry')
async def telemetry(websocket: WebSocket):
    if websocket.query_params.get('token') not in SESSIONS:
        await websocket.close(code=1008)
        return
    manager = get_ws_manager()
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


@router.get('/api/mcp/status')
def status():
    return sim.get_snapshot()


@router.post('/api/mcp/intercept/{scenario_id}')
async def trigger(scenario_id: int, session=Depends(operator)):
    try:
        return await sim.trigger_scenario(scenario_id, session.operator_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/api/mcp/reset')
async def reset(session=Depends(operator)):
    return await sim.reset(session.operator_id)


@router.post('/api/mcp/tools/falkordb_trace_blast_radius')
async def trace(req: BlastRadiusRequest, session=Depends(verify_operator_token)):
    async with sim._lock:
        return server.falkordb_trace_blast_radius(req.origin_node_id, req.max_hops)


@router.post('/api/mcp/tools/falkordb_find_mitigation_assets')
async def assets(substation_id: str = 'SUB_METRO', required_mw: float = 0, session=Depends(verify_operator_token)):
    async with sim._lock:
        return server.falkordb_find_mitigation_assets(substation_id, required_mw)


@router.post('/api/mcp/tools/scada_trip_breaker')
async def trip(req: TripBreakerRequest, session=Depends(operator)):
    async with sim._lock:
        try:
            return server.scada_trip_breaker(req.breaker_id, req.reason_code, req.cypher_audit_hash, operator_id=session.operator_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc


@router.post('/api/mcp/tools/scada_dispatch_bess')
async def dispatch(req: DispatchBessRequest, session=Depends(operator)):
    async with sim._lock:
        try:
            return server.scada_dispatch_bess(req.bess_id, req.inject_mw, req.target_circuit, operator_id=session.operator_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
