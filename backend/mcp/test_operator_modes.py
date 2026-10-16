"""Deterministic playbook reuse and AI-operator mode against a real FalkorDB graph."""
import asyncio

from backend import graph
from backend.mcp import agent
from backend.mcp.test_agent import FakeClient, GOOD_PLAN, script
from backend.mcp.test_live_runtime import live_runtime  # noqa: F401  (fixture)


def run_scenario(sim, scenario=1, mode='deterministic'):
    async def go():
        await sim.trigger_scenario(scenario, mode=mode)
        await sim._active_task
    asyncio.run(go())


def test_second_run_reuses_playbook_from_memory(live_runtime):  # noqa: F811
    sim, store, _ = live_runtime
    graph.init_graph(reset=True)
    run_scenario(sim)
    assert sim._scenario_status == 'MITIGATED', sim._agent_log
    assert 'playbook' not in sim._agent_log
    first = sim.get_snapshot()['memory']
    assert first['recorded_incident'] and first['playbook']['success_count'] == 1

    asyncio.run(sim.reset())
    run_scenario(sim)
    assert sim._scenario_status == 'MITIGATED', sim._agent_log
    assert 'playbook reused' in sim._agent_log and 'generalised' not in sim._agent_log
    snap = sim.get_snapshot()['memory']
    assert snap['incidents'] and snap['playbook']['success_count'] == 2
    incidents = graph.memory.list_incidents()['incidents']
    assert [i['source'] for i in incidents] == ['PLAYBOOK', 'INTERCEPTOR']
    assert sim._consumers['HOSP_ST_JUDE']['energized']


def test_agent_mode_executes_guard_approved_plan(live_runtime, monkeypatch):  # noqa: F811
    sim, store, _ = live_runtime
    graph.init_graph(reset=True)
    monkeypatch.setattr(agent, 'make_client', lambda: FakeClient(script(GOOD_PLAN)))
    run_scenario(sim, mode='agent')
    assert sim._scenario_status == 'MITIGATED', sim._agent_log
    kinds = [s['kind'] for s in sim._agent_steps]
    assert kinds[0] == 'start' and 'tool' in kinds and 'verdict' in kinds and kinds[-1] == 'done'
    tool_step = next(s for s in sim._agent_steps if s['kind'] == 'tool')
    assert tool_step['tool'] == 'trace_cascade' and tool_step['cypher'] and tool_step['evidence_hashes']
    assert graph.memory.list_incidents()['incidents'][0]['source'] == 'AGENT'
    assert any(e['action'] == 'AGENT_PROPOSAL' for e in store.query_logs())
    assert sim._bess['active_injection_mw'] == 45.0 and not sim._consumers['IND_METRO_12']['energized']


def test_agent_mode_falls_back_when_unavailable_or_rejected(live_runtime, monkeypatch):  # noqa: F811
    sim, store, _ = live_runtime
    graph.init_graph(reset=True)

    def unavailable():
        raise agent.AgentUnavailable('no key')
    monkeypatch.setattr(agent, 'make_client', unavailable)
    run_scenario(sim, mode='agent')
    assert sim._scenario_status == 'MITIGATED'
    assert sim._agent_steps[-1]['kind'] == 'fallback'

    asyncio.run(sim.reset())
    bad = {'trip_breakers': ['BRK_HOSP_01'], 'bess_dispatch': {}}
    monkeypatch.setattr(agent, 'make_client', lambda: FakeClient(script(bad)))
    run_scenario(sim, mode='agent')
    assert sim._scenario_status == 'MITIGATED'
    assert any(s['kind'] == 'verdict' and s['verdict'] == 'REJECTED' for s in sim._agent_steps)
    assert sim._consumers['HOSP_ST_JUDE']['energized']
    assert any(e['action'] == 'AGENT_PROPOSAL' and e['outcome'] == 'REJECTED' for e in store.query_logs())


def test_wildfire_reuses_the_lightning_playbook_by_similarity(live_runtime):  # noqa: F811
    sim, store, _ = live_runtime
    graph.init_graph(reset=True)
    run_scenario(sim, 1)  # lightning on TX_882: solved by search, playbook learned
    asyncio.run(sim.reset())
    run_scenario(sim, 2)  # wildfire on TX_884: different fault, one hop away, same downstream substation
    assert sim._scenario_status == 'MITIGATED', sim._agent_log
    assert 'generalised from a similar fault' in sim._agent_log
    mem = sim.get_snapshot()['memory']
    assert mem['suggestions'][0]['signature'] == 'TRIP:TX_882:UPPER_PRIMARY'
    assert mem['reused_playbook'] == 'TRIP:TX_882:UPPER_PRIMARY' and mem['reused_credit']['success_count'] == 2
    curve = graph.memory.learning_curve()
    first, second = curve['incidents']
    assert first['learned_from'] in ('', None) and second['learned_from'] == 'TRIP:TX_882:UPPER_PRIMARY'
    assert second['steps'] < first['steps']
    assert sim._consumers['HOSP_ST_JUDE']['energized']
