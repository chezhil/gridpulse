"""Advisory agent loop with a scripted model client against a real FalkorDB graph.

No Groq API call is made; the fake client replays fixed tool calls in Groq's
chat-completions shape so the loop, tool execution, evidence capture and
deterministic validation are tested.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import graph
from backend.main import app
from backend.mcp import agent
from backend.mcp.test_live_runtime import live_runtime  # noqa: F401  (fixture)

TRIP = {'span_id': 'TX_882', 'circuit': 'UPPER_PRIMARY', 'status': 'TRIPPED', 'flow_mw': 0.0}
GOOD_PLAN = {'trip_breakers': ['BRK_IND_12'], 'bess_dispatch': {'BESS_METRO_01': 45.0}}


def call(i, name, args, raw=None):
    return SimpleNamespace(id=f'call{i}', type='function',
                           function=SimpleNamespace(name=name, arguments=raw if raw is not None else json.dumps(args)))


def response(tool_calls=None, content=None, finish='tool_calls'):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=message)],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, 'messages': list(kwargs['messages'])})
        return self.script.pop(0)


def script(plan):
    return [
        response([call(1, 'trace_cascade', {'origin_id': 'TX_882'})]),
        response([call(2, 'evaluate_plan', plan)], content='Checking the plan.'),
        response([call(3, 'submit_plan', {**plan, 'summary': 'Shed BRK_IND_12 and dispatch 45 MW.',
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
    assert first['model'] == agent.MODEL and first['tool_choice'] == 'auto'
    assert first['messages'][0]['role'] == 'system'
    assert {t['function']['name'] for t in first['tools']} >= {'trace_cascade', 'evaluate_plan', 'submit_plan'}
    # The tool result went back as a role=tool message tied to the call id.
    last = fake.requests[1]['messages'][-1]
    assert last['role'] == 'tool' and last['tool_call_id'] == 'call1' and 'OVERLOAD_PREDICTED' in last['content']
    assert advice['provider'] == 'groq'


def test_bad_plan_is_rejected_not_executed(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    graph.apply_state_update({'spans': [TRIP]})
    bad = {'trip_breakers': ['BRK_HOSP_01'], 'bess_dispatch': {}}
    advice = agent.advise(client=FakeClient(script(bad)))
    assert advice['validation']['verdict'] == 'REJECTED'
    assert any('SERVES_TIER_1' in r for r in advice['validation']['reasons'])


def test_truncation_bad_json_and_no_plan_fail_explicitly(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    with pytest.raises(agent.AgentFailed):
        agent.advise(client=FakeClient([response(content='...', finish='length')]))
    endless = [response(content='hmm', finish='stop')] * agent.MAX_TURNS
    with pytest.raises(agent.AgentFailed):
        agent.advise(client=FakeClient(endless))
    # Malformed arguments go back to the model as a tool error, and the loop continues.
    fake = FakeClient([response([call(1, 'trace_cascade', None, raw='{not json')])] + script(GOOD_PLAN))
    advice = agent.advise(client=fake)
    assert advice['tool_calls'][0]['error'].startswith('invalid arguments')
    assert fake.requests[1]['messages'][-1]['content'].startswith('ERROR: invalid JSON')


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
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    with pytest.raises(agent.AgentUnavailable):
        agent.make_client()
