"""Complete in-process app tests against a real isolated FalkorDB graph.

Only audit location and graph name are isolated. No query answers are replaced.
"""
import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from backend import graph
from backend.main import app
from backend.simulator.grid_sim import grid_simulator as sim
from backend.audit.logger import AuditLogger


@pytest.fixture
def live_runtime(monkeypatch, tmp_path):
    settings = graph.GraphSettings.from_env()
    client = graph.GraphClient(graph.GraphSettings(**{
        **settings.__dict__, 'graph_name': 'gridpulse_runtime_test_' + uuid.uuid4().hex,
        'connect_attempts': 1}))
    try:
        client.connect()
    except graph.GraphUnavailableError:
        if os.environ.get('GRIDPULSE_REQUIRE_FALKORDB'):
            raise
        pytest.skip('FalkorDB unavailable: live runtime tests require a real server')
    previous = graph.get_client()
    graph.set_client(client)
    store = AuditLogger(str(tmp_path / 'audit.db'))
    monkeypatch.setattr('backend.simulator.grid_sim.audit_logger', store)
    monkeypatch.setattr('backend.audit.logger.audit_logger', store)
    sim.__init__()
    try:
        yield sim, store, client
    finally:
        client._graph.delete()
        graph.set_client(previous)


@pytest.mark.parametrize('scenario', [1, 2, 3])
def test_real_scenario_evidence_and_guards(live_runtime, scenario):
    sim, store, client = live_runtime
    graph.init_graph(reset=True)

    async def run():
        await sim.trigger_scenario(scenario)
        original_task = sim._active_task
        assert (await sim.trigger_scenario(scenario))['run_id'] == sim._run_id
        assert sim._active_task is original_task
        with pytest.raises(ValueError):
            await sim.trigger_scenario(scenario % 3 + 1)
        await original_task
        assert sim._scenario_status == 'MITIGATED', sim._agent_log
        assert graph.verify_tier1_protection()['all_protected']
        logs = store.query_logs(run_id=sim._run_id)
        trace = next(e['evidence'] for e in logs if e['action'] == 'CASCADE_TRACE')
        assert trace['timing']['graph_server_ms'] > 0
        assert all(graph.verify_evidence(e) for e in trace['evidence'])
        if scenario == 3:
            assert sum(c['mw_demand'] for c in sim._consumers.values()) == pytest.approx(1008)
            assert trace['projection']['substations'][0]['net_demand_mw'] == pytest.approx(1008)
        assert len([e for e in logs if e['outcome'] == 'REQUESTED']) == 2
        assert AuditLogger(store.db_path).query_logs(run_id=sim._run_id) == logs
        await sim.reset()
        await sim.trigger_scenario(1)
        await asyncio.sleep(.01)
        await sim.reset()
        await asyncio.sleep(.6)
        assert sim._scenario_status == 'NOMINAL'
        assert graph.verify_tier1_protection()['all_protected']
    asyncio.run(run())


def test_real_http_roles_capacity_and_ws_reconnect(live_runtime):
    from backend.mcp import server
    sim, store, _ = live_runtime
    with TestClient(app) as c:
        assert c.post('/api/sim/reset', headers={'Authorization': 'Bearer gp-demo-nerc-session-token'}).status_code == 401
        tokens = {}
        for role in ['operator', 'engineer', 'auditor']:
            token = c.post('/api/auth/login', json={'demo': True, 'role': role}).json()['access_token']
            tokens[role] = token
            headers = {'Authorization': 'Bearer ' + token}
            if role != 'operator':
                for endpoint, body in [('/api/sim/reset', {}), ('/api/sim/trigger-scenario/1', {}),
                    ('/api/mcp/tools/scada_trip_breaker', {'breaker_id': 'BRK_IND_12', 'reason_code': 'test', 'cypher_audit_hash': 'forged'}),
                    ('/api/mcp/tools/scada_dispatch_bess', {'bess_id': 'BESS_METRO_01', 'inject_mw': 45})]:
                    assert c.post(endpoint, json=body, headers=headers).status_code == 403
            for _ in range(2):
                with c.websocket_connect('/ws/telemetry?token=' + token) as ws:
                    assert ws.receive_json()['run_id'] == sim._run_id
        headers = {'Authorization': 'Bearer ' + tokens['operator']}
        assert c.post('/api/mcp/tools/scada_trip_breaker', headers=headers, json={
            'breaker_id': 'BRK_HOSP_01', 'reason_code': 'test', 'cypher_audit_hash': 'forged'}).status_code == 403
        assert sim._consumers['HOSP_ST_JUDE']['energized']
        sim._bess['soc_pct'] = 30
        assert c.post('/api/mcp/tools/scada_dispatch_bess', headers=headers, json={
            'bess_id': 'BESS_METRO_01', 'inject_mw': 45}).status_code == 400
        assert sim._bess['active_injection_mw'] == 0
        assert c.post('/api/sim/trigger-scenario/1', headers=headers).status_code == 200
        import time
        for _ in range(30):
            if sim._scenario_status == 'FAILED':
                break
            time.sleep(.1)
        assert sim._scenario_status == 'FAILED'
        assert sim._consumers['IND_METRO_12']['energized']
        assert not sim._bess['active_injection_mw']
        assert any(e['outcome'] == 'REJECTED' for e in store.query_logs())


def test_real_battery_setpoints_are_repeatable(live_runtime):
    from backend.mcp import server
    graph.init_graph(reset=True)
    sim, store, _ = live_runtime
    for amount in [45, 45, 60, 40]:
        server.scada_dispatch_bess('BESS_METRO_01', amount, operator_id='OP_SETPOINT')
        assert sim._bess['active_injection_mw'] == amount
        snapshot, _ = graph.read_grid_snapshot()
        assert snapshot['bess'][0]['active_injection_mw'] == amount
        assert sim._substation['current_load_mw'] == 840 - amount
    assert all(e['operator_id'] == 'OP_SETPOINT' for e in store.query_logs())


def test_session_validation_rejects_restart_tokens(live_runtime):
    from backend.auth.router import SESSIONS
    with TestClient(app) as c:
        token = c.post('/api/auth/login', json={'demo': True}).json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        info = c.get('/api/auth/session', headers=headers)
        assert info.status_code == 200 and 'token' not in info.json()
        SESSIONS.pop(token)
        assert c.get('/api/auth/session', headers=headers).status_code == 401


def test_real_insufficient_discharge_capacity_has_no_actuation(live_runtime):
    sim, store, client = live_runtime
    graph.init_graph(reset=True)
    client.query('limit_test_battery', "MATCH (b:BESS {id:'BESS_METRO_01'}) SET b.max_discharge_mw = 10", read_only=False)
    sim._bess['max_discharge_mw'] = 10
    async def run():
        await sim.trigger_scenario(1)
        await sim._active_task
        assert sim._scenario_status == 'FAILED'
        assert sim._bess['active_injection_mw'] == 0
        assert sim._consumers['IND_METRO_12']['energized']
        assert not any(e['outcome'] == 'REQUESTED' for e in store.query_logs())
    asyncio.run(run())
