"""MCP JSON-RPC over stdio, forwarding to the running authoritative app.

Run from the repository root: python -m backend.mcp.transport
Set GRIDPULSE_MCP_TOKEN to a login token from that app. This process deliberately
imports no simulator, graph client, or audit logger and owns no grid state.
"""
import os
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP('GridPulse SCADA')


async def _call(tool, body=None, params=None):
    token = os.environ.get('GRIDPULSE_MCP_TOKEN')
    if not token:
        raise ValueError('GRIDPULSE_MCP_TOKEN must be a token from /api/auth/login')
    base = os.environ.get('GRIDPULSE_API_URL', 'http://127.0.0.1:8000')
    async with httpx.AsyncClient(base_url=base, timeout=10, follow_redirects=False) as client:
        response = await client.post('/api/mcp/tools/' + tool, json=body, params=params,
                                     headers={'Authorization': 'Bearer ' + token})
    if response.is_error:
        data = response.json()
        raise ValueError(f"Runtime rejected tool ({response.status_code}): {data.get('detail', 'Request failed')}")
    return response.json()


@mcp.tool()
async def falkordb_trace_blast_radius(origin_node_id: str = 'TX_882',
                                     max_hops: Annotated[int, Field(ge=1, le=12)] = 6) -> dict:
    """Execute a bounded graph trace against the app's current simulator state."""
    return await _call('falkordb_trace_blast_radius', {'origin_node_id': origin_node_id, 'max_hops': max_hops})


@mcp.tool()
async def falkordb_find_mitigation_assets(substation_id: str = 'SUB_METRO',
                                        required_mw: Annotated[float, Field(ge=0, le=10000)] = 0) -> dict:
    """Find graph-eligible batteries and isolated Tier-3 loads."""
    return await _call('falkordb_find_mitigation_assets', params={
        'substation_id': substation_id, 'required_mw': required_mw})


@mcp.tool()
async def scada_trip_breaker(breaker_id: str, reason_code: str, cypher_audit_hash: str = '') -> dict:
    """Request a simulator trip; operator role, fresh graph guard and durable audit intent are required."""
    return await _call('scada_trip_breaker', {'breaker_id': breaker_id, 'reason_code': reason_code,
                                           'cypher_audit_hash': cypher_audit_hash})


@mcp.tool()
async def scada_dispatch_bess(bess_id: str, inject_mw: Annotated[float, Field(gt=0, le=60)],
                              target_circuit: Literal['LOWER_BACKUP'] = 'LOWER_BACKUP') -> dict:
    """Request battery support through the app's capacity, SOC, role and audit guards."""
    return await _call('scada_dispatch_bess', {'bess_id': bess_id, 'inject_mw': inject_mw,
                                             'target_circuit': target_circuit})


if __name__ == '__main__':
    mcp.run(transport='stdio')
