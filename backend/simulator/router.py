"""
GridPulse Simulator API Router.
Exposes REST endpoints for scenario triggers, grid reset, state inspection,
and direct SCADA actuation tools.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Depends, status
from typing import Dict, Any

from backend.graph import GraphError
from backend.mcp import server
from backend.auth.router import verify_operator_token, OperatorSession, require_role
from backend.simulator.grid_sim import (
    grid_simulator,
    BreakerActionRequest,
    BessDispatchRequest
)

sim_router = APIRouter()


@sim_router.get("/state")
def get_grid_state() -> Dict[str, Any]:
    """
    GET /api/sim/state
    Returns the authoritative real-time snapshot of grid topology,
    spans, towers, substation load, and BESS reserves.
    """
    return grid_simulator.get_snapshot()


@sim_router.post("/trigger-scenario/{scenario_id}")
async def trigger_scenario(
    scenario_id: int,
    session: OperatorSession = Depends(require_role(["operator", "NERC_OPERATOR", "SCADA_ADMIN"]))
) -> Dict[str, Any]:
    """
    POST /api/sim/trigger-scenario/{scenario_id}
    Triggers one of the 3 documented grid emergency scenarios:
    1 = Lightning Strike on Span TX-882 (T3 -> T4)
    2 = Wildfire Smoke Ionization along corridor (T5 -> T6)
    3 = 5 PM Extreme Heatwave Demand Surge (120% City Load)
    Requires authenticated operator session.
    """
    if scenario_id not in (1, 2, 3):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scenario_id {scenario_id}. Supported scenarios are 1, 2, or 3."
        )

    try:
        return await grid_simulator.trigger_scenario(
            scenario_id=scenario_id,
            operator_id=session.operator_id
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except GraphError:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scenario trigger failed: {str(e)}"
        )


@sim_router.post("/reset")
async def reset_grid(
    session: OperatorSession = Depends(require_role(["operator", "NERC_OPERATOR", "SCADA_ADMIN"]))
) -> Dict[str, Any]:
    """
    POST /api/sim/reset
    Safely aborts any active scenario and restores all 500kV circuits,
    breakers, and BESS inverters to nominal operating parameters.
    Requires authenticated operator session.
    """
    try:
        return await grid_simulator.reset(operator_id=session.operator_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except GraphError:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Grid reset failed: {str(e)}"
        )


@sim_router.post("/actuate/trip-breaker")
async def trip_breaker(
    req: BreakerActionRequest,
    session: OperatorSession = Depends(require_role(["operator", "NERC_OPERATOR", "SCADA_ADMIN"]))
) -> Dict[str, Any]:
    """
    POST /api/sim/actuate/trip-breaker
    Direct SCADA actuation tool for the autonomous MCP agent or operator.
    Guarantees Tier-1 hospital invariant protection.
    """
    try:
        async with grid_simulator._lock:
            return await asyncio.to_thread(server.scada_trip_breaker, req.breaker_id, req.reason, req.cypher_audit_hash, operator_id=session.operator_id)
    except PermissionError as pe:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(pe)
        )
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(ve)
        )


@sim_router.post("/actuate/dispatch-bess")
async def dispatch_bess(
    req: BessDispatchRequest,
    session: OperatorSession = Depends(require_role(["operator", "NERC_OPERATOR", "SCADA_ADMIN"]))
) -> Dict[str, Any]:
    """
    POST /api/sim/actuate/dispatch-bess
    Commands local utility BESS battery inverters to inject power.
    """
    try:
        async with grid_simulator._lock:
            return await asyncio.to_thread(server.scada_dispatch_bess, req.bess_id, req.inject_mw, req.target_circuit, operator_id=session.operator_id)
    except ValueError as ve:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
