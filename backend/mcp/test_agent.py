"""Advisory agent loop with a scripted model client against a real FalkorDB graph.

No Anthropic API call is made; the fake client replays fixed tool calls so the
loop, tool execution, evidence capture and deterministic validation are tested.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import graph
from backend.main import app
from backend.mcp import agent
from backend.mcp.test_live_runtime import live_runtime  # noqa: F401  (fixture)

TRIP = {'span_id': 'TX_882', 'circuit': 'UPPER_PRIMARY', 'status': 'TRIPPED', 'flow_mw': 0.0}
GOOD_PLAN = {'trip_breakers': ['BRK_IND_12'], 'bess_dispatch': {'BESS_METRO_01': 45.0}}


def tool_use(i, name, args):
    return SimpleNamespace(type='tool_use', id=f'tu{i}', name=name, input=args)


def response(blocks, stop='tool_use'):
    return SimpleNamespace(stop_reason=stop, content=blocks, usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, 'messages': list(kwargs['messages'])})
        return self.script.pop(0)


def script(plan):
    return [
        response([tool_use(1, 'trace_cascade', {'origin_id': 'TX_882'})]),
        response([SimpleNamespace(type='text', text='Checking the plan.'), tool_use(2, 'evaluate_plan', plan)]),
        response([tool_use(3, 'submit_plan', {**plan, 'summary': 'Shed BRK_IND_12 and dispatch 45 MW.',
                                              'rationale': 'Backup circuit TX_882 projected at 105%.'})]),
    ]


def test_agent_loop_runs_tools_and_validates(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    graph.apply_state_update({'spans': [TRIP]})
    fake = FakeClient(script(GOOD_PLAN))
    advice = agent.advise(client=fake)
    assert advice['validation']['verdict'] == 'APPROVED'
    assert advice['plan'] == GOOD_PLAN
    assert [c['tool'] for c in advice['tool_calls']] == ['trace_cascade', 'evaluate_plan']
    assert all(c['evidence_hashes'] for c in advice['tool_calls'])
    first = fake.requests[0]
    assert first['model'] == agent.MODEL and first['fallbacks'] == 'default'
    assert {t['name'] for t in first['tools']} >= {'trace_cascade', 'evaluate_plan', 'submit_plan'}
    # Tool results went back to the model as a single user message per turn.
    results = fake.requests[1]['messages'][-1]['content']
    assert results[0]['type'] == 'tool_result' and 'OVERLOAD_PREDICTED' in results[0]['content']


def test_bad_plan_is_rejected_not_executed(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    graph.apply_state_update({'spans': [TRIP]})
    bad = {'trip_breakers': ['BRK_HOSP_01'], 'bess_dispatch': {}}
    advice = agent.advise(client=FakeClient(script(bad)))
    assert advice['validation']['verdict'] == 'REJECTED'
    assert any('SERVES_TIER_1' in r for r in advice['validation']['reasons'])


def test_refusal_and_no_plan_fail_explicitly(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    with pytest.raises(agent.AgentFailed):
        agent.advise(client=FakeClient([response([], stop='refusal')]))
    endless = [response([SimpleNamespace(type='text', text='hmm')], stop='end_turn')] * agent.MAX_TURNS
    with pytest.raises(agent.AgentFailed):
        agent.advise(client=FakeClient(endless))


def test_http_advise_execute_and_unavailable(live_runtime, monkeypatch):  # noqa: F811
    sim, store, _ = live_runtime
    graph.init_graph(reset=True)
    sim._spans['TX_882']['upper_status'] = 'FAULT_TRIPPED'
    with TestClient(app) as c:
        op = c.post('/api/auth/login', json={'demo': True, 'role': 'operator'}).json()['access_token']
        auditor = c.post('/api/auth/login', json={'demo': True, 'role': 'auditor'}).json()['access_token']
        sim._spans['TX_882']['upper_status'] = 'FAULT_TRIPPED'

        def unavailable():
            raise agent.AgentUnavailable('no credentials')
        monkeypatch.setattr(agent, 'make_client', unavailable)
        r = c.post('/api/mcp/agent/advise', json={}, headers={'Authorization': 'Bearer ' + op})
        assert r.status_code == 503 and r.json()['detail']['code'] == 'AGENT_UNAVAILABLE'

        assert c.post('/api/mcp/agent/advise', json={'execute': True},
                      headers={'Authorization': 'Bearer ' + auditor}).status_code == 403

        monkeypatch.setattr(agent, 'make_client', lambda: FakeClient(script(GOOD_PLAN)))
        r = c.post('/api/mcp/agent/advise', json={'execute': True}, headers={'Authorization': 'Bearer ' + op})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['validation']['verdict'] == 'APPROVED' and len(body['executed']) == 2
    assert not sim._consumers['IND_METRO_12']['energized']
    assert sim._bess['active_injection_mw'] == 45.0
    assert sim._consumers['HOSP_ST_JUDE']['energized']
    actions = [e['action'] for e in store.query_logs()]
    assert 'AGENT_PROPOSAL' in actions and 'SCADA_TRIP_BREAKER' in actions
    assert store.verify_chain()['valid']


def test_missing_credentials_are_reported_as_unavailable(monkeypatch):
    for var in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_PROFILE'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv('ANTHROPIC_CONFIG_DIR', '/nonexistent-gridpulse-test')
    try:
        client = agent.make_client()
    except agent.AgentUnavailable:
        return
    with pytest.raises(agent.AgentUnavailable):
        agent._create(client, [{'role': 'user', 'content': 'x'}])
