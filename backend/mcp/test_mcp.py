"""SCADA guardrail regression tests; graph answers are fixture substitutions."""
import asyncio
import pytest
from backend.mcp import server
from backend import graph


def test_supplied_hash_cannot_trip_hospital(runtime):
    sim,store=runtime
    with pytest.raises(PermissionError):
        server.scada_trip_breaker('BRK_HOSP_01','test','forged-hash')
    assert sim._consumers['HOSP_ST_JUDE']['energized']
    assert store.query_logs()[0]['outcome']=='FAILED'


@pytest.mark.parametrize('amount,circuit', [(0,'LOWER_BACKUP'),(-1,'LOWER_BACKUP'),(61,'LOWER_BACKUP'),
                                          (float('nan'),'LOWER_BACKUP'),(45,'UPPER_PRIMARY')])
def test_invalid_dispatch_no_mutation(runtime, amount,circuit):
    sim,store=runtime
    with pytest.raises((ValueError,graph.GraphError)):
        server.scada_dispatch_bess('BESS_METRO_01',amount,circuit)
    assert sim._bess['active_injection_mw']==0


def test_insufficient_battery_blocks_mitigation(runtime):
    sim,store=runtime
    sim._bess['soc_pct']=30
    async def check():
        await sim.trigger_scenario(1)
        await sim._active_task
        assert sim._scenario_status=='FAILED'
        assert sim._consumers['IND_METRO_12']['energized']
        assert sim._bess['active_injection_mw']==0
    asyncio.run(check())


def test_audit_failure_prevents_actuation(runtime,monkeypatch):
    sim,store=runtime
    def fail(**kwargs):
        raise OSError('audit unavailable')
    monkeypatch.setattr(store,'log_event',fail)
    with pytest.raises(OSError):
        server.scada_trip_breaker('BRK_IND_12','test','forged-hash')
    assert sim._consumers['IND_METRO_12']['energized']


def test_trace_failure_during_run_no_mutation(runtime,monkeypatch):
    sim,store=runtime
    def fail(*a,**kw):
        raise graph.GraphUnavailableError('fixture query failed')
    monkeypatch.setattr(graph,'trace_cascade',fail)
    async def check():
        await sim.trigger_scenario(1)
        await sim._active_task
        assert sim._scenario_status=='FAILED'
        assert sim._consumers['IND_METRO_12']['energized']
        assert sim._bess['active_injection_mw']==0
    asyncio.run(check())
