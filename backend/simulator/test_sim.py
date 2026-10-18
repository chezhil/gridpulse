"""Unified HTTP/WS contracts with explicitly substituted graph fixtures."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend import graph
from backend.simulator.grid_sim import GridSimulator


def login(client, role='operator'):
    result=client.post('/api/auth/login', json={'role':role,'demo':True})
    assert result.status_code==200
    assert result.json()['role']==role
    return {'Authorization':'Bearer '+result.json()['access_token']}


def test_frontend_and_auth(runtime):
    with TestClient(app) as client:
        assert 'GridPulse' in client.get('/').text
        # The console is one page (index.html + app.js); legacy pages redirect into it.
        for path in ['/login.html','/dashboard.html','/scenarios.html','/audit.html','/lab.html',
                     '/assets/app.js','/assets/features.js','/assets/lab.js','/assets/lab-editor.js']:
            assert client.get(path).status_code==200, path
        assert client.post('/api/sim/reset').status_code==401
        for role in ['engineer','auditor']:
            headers=login(client,role)
            assert client.post('/api/sim/trigger-scenario/1',headers=headers).status_code==403
            assert client.post('/api/mcp/reset',headers=headers).status_code==403
        assert client.post('/api/auth/login',json={'role':'invalid','demo':True}).status_code==422


@pytest.mark.parametrize('scenario', [1,2,3])
def test_complete_scenarios_and_reset(runtime, scenario):
    sim, store=runtime
    async def check():
        await sim.trigger_scenario(scenario)
        task=sim._active_task
        same=await sim.trigger_scenario(scenario)
        assert sim._active_task is task
        await task
        assert sim._scenario_status=='MITIGATED', sim._agent_log
        assert sim._consumers['HOSP_ST_JUDE']['energized']
        assert sim._bess['active_injection_mw']==45
        assert not sim._consumers['IND_METRO_12']['energized']
        assert sim.get_snapshot()['metrics']['graphblas_latency_ms']==0.25
        logs=store.query_logs(run_id=sim._run_id)
        assert any(e['action']=='MITIGATION_VERIFIED' for e in logs)
        intents=[e for e in logs if e['outcome']=='REQUESTED']
        assert len(intents)==2 and all(e['evidence']['evidence'] for e in intents)
        await sim.reset()
        assert sim._scenario_status=='NOMINAL'
        assert sim.get_snapshot()['metrics']['city_load_pct']==100
        assert sim._bess['active_injection_mw']==0
        assert len(store.query_logs())>len(logs)
    asyncio.run(check())


def test_reset_cancels_pending_run(runtime):
    sim, store=runtime
    async def check():
        await sim.trigger_scenario(1)
        await asyncio.sleep(0.01)
        await sim.reset()
        await asyncio.sleep(0.6)
        assert sim._scenario_id==0
        assert sim._scenario_status=='NOMINAL'
        assert sim._bess['active_injection_mw']==0
        assert sim._consumers['IND_METRO_12']['energized']
    asyncio.run(check())


def test_graph_failure_blocks_actuation(runtime, monkeypatch):
    sim,store=runtime
    def fail(*a,**kw):
        raise graph.GraphUnavailableError('fixture unavailable')
    monkeypatch.setattr(graph,'init_graph',fail)
    with TestClient(app) as client:
        response=client.post('/api/sim/trigger-scenario/1',headers=login(client))
        assert response.status_code==503
        assert sim._scenario_id==0
        assert sim._consumers['IND_METRO_12']['energized']
        assert sim._bess['active_injection_mw']==0


def test_websocket_auth_and_snapshot(runtime):
    with TestClient(app) as client:
        headers=login(client)
        token=headers['Authorization'].split()[1]
        with client.websocket_connect('/ws/telemetry?token='+token) as ws:
            event=ws.receive_json()
            assert event['scenario']['status']=='NOMINAL'
            assert len(event['spans'])==9
            assert event['metrics']['graphblas_latency_ms'] is None
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/telemetry') as ws:
                ws.receive_json()


def test_audit_survives_logger_restart(runtime):
    sim,store=runtime
    from backend.audit.logger import AuditLogger
    sim.record('TEST','GRID','persistence',evidence={'source':'fixture'})
    assert AuditLogger(store.db_path).query_logs()[0]['reason']=='persistence'


def test_http_trigger_to_websocket_mitigation(runtime):
    with TestClient(app) as client:
        headers=login(client)
        token=headers['Authorization'].split()[1]
        with client.websocket_connect('/ws/telemetry?token='+token) as ws:
            assert ws.receive_json()['scenario']['status']=='NOMINAL'
            assert client.post('/api/sim/trigger-scenario/1',headers=headers).status_code==200
            assert client.post('/api/sim/trigger-scenario/2',headers=headers).status_code==409
            events=[]
            for _ in range(12):
                event=ws.receive_json()
                events.append(event)
                if event['scenario']['status']=='MITIGATED':
                    break
            assert events[-1]['scenario']['status']=='MITIGATED'
            state=client.get('/api/sim/state').json()
            assert state['run_id']==events[-1]['run_id']
            assert state['metrics']['bess_injection_mw']==45
            assert client.post('/api/sim/reset',headers=headers).status_code==200
            assert client.get('/api/sim/state').json()['scenario']['status']=='NOMINAL'
