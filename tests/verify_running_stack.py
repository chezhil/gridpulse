"""Destructive demo acceptance checks against THIS Compose stack only.

Run manually from the repository root with .venv/bin/python. Restarts backend,
stops/starts its FalkorDB, resets grid state, and preserves historical audits.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get('GRIDPULSE_URL', 'http://127.0.0.1:8000')
client = httpx.Client(base_url=BASE, timeout=15)


def compose(*args):
    subprocess.run(['docker', 'compose', *args], cwd=ROOT, check=True)


def ready():
    for _ in range(60):
        try:
            if client.get('/health').json()['status'] == 'HEALTHY':
                return
        except httpx.HTTPError:
            pass
        time.sleep(.25)
    raise AssertionError('Compose app did not become healthy')


def login(role):
    r = client.post('/api/auth/login', json={'role': role, 'demo': True})
    r.raise_for_status()
    return r.json()['access_token']


async def mcp_checks(token, role):
    params = StdioServerParameters(command=sys.executable, args=['-m', 'backend.mcp.transport'],
        cwd=str(ROOT), env={**os.environ, 'GRIDPULSE_MCP_TOKEN': token, 'GRIDPULSE_API_URL': BASE})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == {'falkordb_trace_blast_radius', 'falkordb_find_mitigation_assets',
                                              'scada_trip_breaker', 'scada_dispatch_bess'}
            trace = await session.call_tool('falkordb_trace_blast_radius', {'origin_node_id': 'TX_882'})
            assert not trace.isError
            evidence = json.loads(trace.content[0].text)
            assert evidence['evidence'] and evidence['timing']['graph_server_ms'] > 0
            rejected = await session.call_tool('scada_trip_breaker', {'breaker_id': 'BRK_HOSP_01', 'reason_code': 'acceptance'})
            assert rejected.isError
            dispatch = await session.call_tool('scada_dispatch_bess', {'bess_id': 'BESS_METRO_01', 'inject_mw': 45})
            assert dispatch.isError == (role != 'operator')
    print('MCP stdio initialize/list/trace/guards:', role, flush=True)


def main():
    ready()
    token = login('operator')
    headers = {'Authorization': 'Bearer ' + token}
    client.post('/api/sim/reset', headers=headers).raise_for_status()
    asyncio.run(mcp_checks(login('auditor'), 'auditor'))
    asyncio.run(mcp_checks(token, 'operator'))
    state = client.get('/api/sim/state').json()
    assert state['metrics']['bess_injection_mw'] == 45
    logs = client.get('/api/audit/logs', params={'run_id': state['run_id']}).json()
    ids = {e['id'] for e in logs}
    assert any(e['outcome'] == 'REQUESTED' for e in logs)
    assert any(e['action'] == 'SCADA_DISPATCH_BESS' and e['outcome'] == 'SUCCESS' for e in logs)
    compose('restart', 'backend')
    ready()
    remaining = {e['id'] for e in client.get('/api/audit/logs', params={'run_id': state['run_id']}).json()}
    assert ids <= remaining
    assert client.get('/api/sim/state').json()['metrics']['bess_injection_mw'] == 0
    print('Audit evidence survived real backend process restart:', len(ids), 'records', flush=True)
    token = login('operator')
    headers = {'Authorization': 'Bearer ' + token}
    before = client.get('/api/sim/state').json()
    compose('stop', 'falkordb')
    try:
        started = time.monotonic()
        assert client.get('/health').json()['status'] == 'DEGRADED'
        r = client.post('/api/sim/trigger-scenario/1', headers=headers)
        assert r.status_code == 503, r.text
        after = client.get('/api/sim/state').json()
        assert after['scenario'] == before['scenario']
        assert after['metrics']['bess_injection_mw'] == 0
        assert all(c['energized'] for c in after['consumers'])
        assert client.get('/').status_code == 200
        print('Stopped FalkorDB: 503, no fault or actuation, site available; seconds:', round(time.monotonic()-started, 3), flush=True)
    finally:
        compose('start', 'falkordb')
    ready()
    client.post('/api/sim/reset', headers=headers).raise_for_status()
    print('Running-stack acceptance passed; app left nominal and healthy.', flush=True)


if __name__ == '__main__':
    main()
