"""Authenticated SCADA tools and aliases into the single simulator pipeline."""
import asyncio

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
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
        # Accept, then close with 1008 so browsers can tell "bad session" from a network drop.
        await websocket.accept()
        await websocket.close(code=1008, reason='login required')
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
        return await asyncio.to_thread(server.falkordb_trace_blast_radius, req.origin_node_id, req.max_hops)


@router.post('/api/mcp/tools/falkordb_find_mitigation_assets')
async def assets(substation_id: str = 'SUB_METRO', required_mw: float = 0, session=Depends(verify_operator_token)):
    async with sim._lock:
        return await asyncio.to_thread(server.falkordb_find_mitigation_assets, substation_id, required_mw)


@router.post('/api/mcp/tools/scada_trip_breaker')
async def trip(req: TripBreakerRequest, session=Depends(operator)):
    async with sim._lock:
        try:
            return await asyncio.to_thread(server.scada_trip_breaker, req.breaker_id, req.reason_code, req.cypher_audit_hash, operator_id=session.operator_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc


@router.post('/api/mcp/tools/scada_dispatch_bess')
async def dispatch(req: DispatchBessRequest, session=Depends(operator)):
    async with sim._lock:
        try:
            return await asyncio.to_thread(server.scada_dispatch_bess, req.bess_id, req.inject_mw, req.target_circuit, operator_id=session.operator_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


class AdviseRequest(BaseModel):
    question: Optional[str] = Field(None, max_length=2000)
    execute: bool = Field(False, description='Apply the plan if the deterministic guard approves it (operator role only).')


@router.post('/api/mcp/agent/advise')
async def agent_advise(req: AdviseRequest, session=Depends(verify_operator_token)):
    """An LLM (Groq) investigates with read-only graph tools and proposes a plan; the graph guard decides."""
    from backend import graph
    from backend.mcp import agent
    if req.execute and session.role != 'operator':
        raise HTTPException(403, 'Only the operator role can execute an agent plan')
    async with sim._lock:
        await asyncio.to_thread(server.sync)
    try:
        # The model loop runs without the simulator lock so telemetry and scenarios keep running.
        advice = await asyncio.to_thread(agent.advise, req.question)
    except agent.AgentUnavailable as exc:
        raise HTTPException(503, {'code': 'AGENT_UNAVAILABLE', 'message': str(exc)}) from exc
    except agent.AgentFailed as exc:
        raise HTTPException(502, {'code': 'AGENT_FAILED', 'message': str(exc)}) from exc
    record = await asyncio.to_thread(
        sim.record, 'AGENT_PROPOSAL', 'SUB_METRO', advice['summary'] or 'Agent proposal',
        'SUCCESS' if advice['validation']['verdict'] == 'APPROVED' else 'REJECTED',
        {k: advice[k] for k in ('model', 'plan', 'validation', 'validation_evidence', 'tool_calls')},
        advice['plan'], session.operator_id)
    advice['audit_id'] = record['id']
    advice['executed'] = []
    if req.execute and advice['validation']['verdict'] == 'APPROVED':
        async with sim._lock:
            # State may have moved while the model was thinking; re-check before touching anything.
            await asyncio.to_thread(server.sync)
            recheck = await asyncio.to_thread(graph.evaluate_plan, advice['plan'])
            if recheck['verdict'] != 'APPROVED':
                advice['execution_blocked'] = recheck['reasons']
                return advice
            for breaker_id in advice['plan']['trip_breakers']:
                advice['executed'].append(await asyncio.to_thread(
                    server.scada_trip_breaker, breaker_id, 'Agent plan approved by graph guard',
                    operator_id=session.operator_id))
            for bess_id, mw in advice['plan']['bess_dispatch'].items():
                advice['executed'].append(await asyncio.to_thread(
                    server.scada_dispatch_bess, bess_id, mw, operator_id=session.operator_id))
            sim._agent_log = f"Agent plan executed after graph approval: {advice['summary']}"
    return advice


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    network_id: Optional[str] = Field(None, description="Ask about one of your saved networks instead of the live grid")


@router.post('/api/mcp/ask')
async def ask_the_grid(req: AskRequest, session=Depends(verify_operator_token)):
    """Plain-English question -> one read-only Cypher query (guarded, GRAPH.RO_QUERY) -> rows + short answer."""
    from backend.graph import GraphError, networks
    from backend.graph.router import _STATUS
    from backend.mcp import agent, ask
    try:
        client = networks.owned_client(session.operator_id, req.network_id) if req.network_id else None
        return await asyncio.to_thread(ask.ask, req.question, client)
    except agent.AgentUnavailable as exc:
        raise HTTPException(503, {'code': 'AGENT_UNAVAILABLE', 'message': str(exc)}) from exc
    except agent.AgentFailed as exc:
        raise HTTPException(502, {'code': 'AGENT_FAILED', 'message': str(exc)}) from exc
    except GraphError as exc:
        raise HTTPException(_STATUS.get(type(exc), 500), exc.to_dict()) from exc
