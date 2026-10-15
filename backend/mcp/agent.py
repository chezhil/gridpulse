"""LLM advisory agent (Groq): the model investigates, the graph decides.

The model gets read-only graph tools (trace, mitigation discovery, breaker guard,
Tier-1 audit, reroute paths, criticality, plan evaluation) and must finish by
calling ``submit_plan``. The proposed plan is then checked deterministically
by :func:`graph.evaluate_plan` - the same guards actuation uses. Nothing the
model says can authorize an action: execution (opt-in, operator role only)
re-evaluates the plan against current state and goes through the guarded
SCADA tools, each of which re-checks the graph and writes audit records.

Uses Groq's chat completions API with function calling. Requires
``GROQ_API_KEY``; the model is ``GRIDPULSE_AGENT_MODEL`` (default
``openai/gpt-oss-120b``).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from backend import graph

MODEL = os.environ.get('GRIDPULSE_AGENT_MODEL', 'openai/gpt-oss-120b')
MAX_TURNS = 12
MAX_TOKENS = 8192
# Groq occasionally rejects a malformed tool call (400 tool_use_failed); retry the turn this many times.
TOOL_CALL_RETRIES = 2

SYSTEM_PROMPT = """You are the GridPulse grid-security advisor for a 500 kV corridor feeding a metropolitan substation.
Your job: investigate the current grid state with the tools, then propose the smallest mitigation that brings every
circuit and substation back within limits while keeping every Tier-1 consumer (the St. Jude trauma center) supplied.

Rules:
- Start by calling recall_memory on the fault origin: past incidents and playbooks near it show what worked before.
  A playbook is a hint, not an authority; still check it with evaluate_plan against the current state.
- Ground every claim in tool results. Cite circuit, substation, breaker and BESS IDs exactly as the tools return them.
- Only Tier-3 breakers that check_breaker allows may be tripped. BESS dispatch is an absolute MW setpoint within
  max_discharge_mw and needs SOC above 40%.
- Prefer battery support before load shedding; shed only what the relief requirement needs.
- Before finishing, call evaluate_plan on your candidate plan. If it is REJECTED, revise it.
- Finish by calling submit_plan exactly once. If no mitigation is needed, submit an empty plan and say why.
- You cannot actuate anything; a deterministic guard re-checks your plan and an operator decides."""


def _slim(result: Dict[str, Any]) -> Dict[str, Any]:
    """Drop bulky evidence rows before sending a tool result to the model; keep the hashes."""
    out = {k: v for k, v in result.items() if k not in ('evidence', 'assumptions')}
    if 'evidence' in result:
        out['evidence_hashes'] = [e.get('evidence_hash') for e in result['evidence']]
    proj = out.get('projection')
    if isinstance(proj, dict):
        out['projection'] = {
            **{k: proj[k] for k in ('model', 'outcome', 'required_relief_mw', 'overloads', 'islanded_substations') if k in proj},
            'circuits_over_80pct': [c for c in proj.get('circuits', []) if c['loading_pct'] > 80],
            'substations': proj.get('substations'),
        }
    return out


def _headline(name: str, result: Dict[str, Any]) -> str:
    """One-line human summary of a tool result, for live step displays."""
    if name == 'trace_cascade':
        sm = result.get('summary', {})
        return (f"{sm.get('projection_outcome')}; relief {sm.get('required_relief_mw')} MW; "
                f"downstream {sm.get('downstream_substations')}; tier-1 at risk {sm.get('tier1_at_risk')}")
    if name == 'find_mitigation_assets':
        t = result.get('totals', {})
        return f"battery {t.get('bess_available_mw')} MW, sheddable {t.get('sheddable_mw')} MW, meets={result.get('meets_requirement')}"
    if name == 'evaluate_plan':
        return f"{result.get('verdict')} {result.get('reasons') or ''}".strip()
    if name == 'check_breaker':
        return f"trip_allowed={result.get('trip_allowed')} {result.get('reasons') or ''}".strip()
    if name == 'recall_memory':
        return f"{len(result.get('incidents', []))} past incidents, {len(result.get('playbooks', []))} playbooks nearby"
    if name == 'verify_tier1_protection':
        return str(result.get('outcome'))
    if name == 'find_islands':
        return f"{len(result.get('components', []))} component(s)"
    return 'ok'


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required,
                       'additionalProperties': False}}}


CIRCUIT_REF = {'type': 'object', 'properties': {'span_id': {'type': 'string'},
               'circuit': {'type': 'string', 'enum': ['UPPER_PRIMARY', 'LOWER_BACKUP']}},
               'required': ['span_id', 'circuit'], 'additionalProperties': False}
PLAN_PROPS = {'trip_breakers': {'type': 'array', 'items': {'type': 'string'}},
              'bess_dispatch': {'type': 'object', 'additionalProperties': {'type': 'number'}}}

TOOLS = [
    _tool('trace_cascade', 'Downstream blast radius of a span or node plus the dc-pf/1 loading projection of the '
          'current graph state. Optional assume_tripped adds hypothetical circuit trips.',
          {'origin_id': {'type': 'string', 'description': 'Span (TX_880..TX_888) or node id, e.g. SUB_METRO'},
           'max_hops': {'type': 'integer', 'minimum': 1, 'maximum': 12},
           'assume_tripped': {'type': 'array', 'items': CIRCUIT_REF}}, ['origin_id']),
    _tool('find_mitigation_assets', 'Eligible BESS units and sheddable Tier-3 loads at a substation, with reasons '
          'for anything ineligible.',
          {'substation_id': {'type': 'string'}, 'required_mw': {'type': 'number', 'minimum': 0}},
          ['substation_id', 'required_mw']),
    _tool('check_breaker', 'Whether tripping a breaker is allowed (Tier-1/Tier-2 guard) and how much load it sheds.',
          {'breaker_id': {'type': 'string'}}, ['breaker_id']),
    _tool('verify_tier1_protection', 'Current energisation and projected risk for every Tier-1 consumer.',
          {'substation_id': {'type': 'string'}}, []),
    _tool('reroute_paths', 'Lowest-impedance energised paths between two nodes (FalkorDB algo.SPpaths).',
          {'source_id': {'type': 'string'}, 'target_id': {'type': 'string'},
           'k': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, []),
    _tool('criticality_ranking', 'Grid buses ranked by betweenness centrality with any N-1-critical circuits.',
          {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}, []),
    _tool('recall_memory', 'Past incidents (actions and outcomes) and learned playbooks within N grid hops of a span '
          'or node, from the graph memory.',
          {'origin_id': {'type': 'string'}, 'hops': {'type': 'integer', 'minimum': 0, 'maximum': 6}}, ['origin_id']),
    _tool('find_islands', 'Topologically disconnected parts of the grid (FalkorDB algo.WCC) and whether each has a generator.',
          {}, []),
    _tool('evaluate_plan', 'Deterministic what-if check of a mitigation plan against the actuation guards. '
          'Returns APPROVED or REJECTED with reasons and the projected state. Never actuates.',
          PLAN_PROPS, []),
    _tool('submit_plan', 'Submit the final mitigation proposal. Call exactly once, last.',
          {**PLAN_PROPS, 'summary': {'type': 'string', 'description': 'Two or three sentences for the operator.'},
           'rationale': {'type': 'string', 'description': 'Why this plan, citing tool results.'}},
          ['summary', 'rationale', 'trip_breakers', 'bess_dispatch']),
]

HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    'trace_cascade': lambda a: graph.trace_cascade(a['origin_id'], max_hops=a.get('max_hops', 6),
                                                   assume_tripped=a.get('assume_tripped')),
    'find_mitigation_assets': lambda a: graph.find_mitigation_assets(a['substation_id'], a['required_mw']),
    'check_breaker': lambda a: graph.check_breaker(a['breaker_id']),
    'verify_tier1_protection': lambda a: graph.verify_tier1_protection(a.get('substation_id')),
    'reroute_paths': lambda a: graph.reroute_paths(a.get('source_id', 'HYDRO_ALPINE_01'),
                                                   a.get('target_id', 'SUB_METRO'), a.get('k', 3)),
    'criticality_ranking': lambda a: graph.criticality_ranking(a.get('limit', 10)),
    'recall_memory': lambda a: graph.memory.recall(a['origin_id'], a.get('hops', 2)),
    'find_islands': lambda a: graph.connected_components(),
    'evaluate_plan': lambda a: graph.evaluate_plan({'trip_breakers': a.get('trip_breakers', []),
                                                    'bess_dispatch': a.get('bess_dispatch', {})}),
}


class AgentUnavailable(RuntimeError):
    """No usable model client (missing credentials or SDK)."""


class AgentFailed(RuntimeError):
    """The model did not produce a usable plan."""


def make_client():
    try:
        import groq
    except ImportError as exc:
        raise AgentUnavailable('groq SDK not installed; install backend/requirements.txt') from exc
    key = os.environ.get('GROQ_API_KEY', '').strip()
    if not key:  # compose passes an empty string when .env has no key
        raise AgentUnavailable('No Groq credentials configured; set GROQ_API_KEY')
    try:
        return groq.Groq(api_key=key)
    except groq.GroqError as exc:
        raise AgentUnavailable('No Groq credentials configured; set GROQ_API_KEY') from exc


def _create(client, messages):
    import groq
    kwargs = dict(model=MODEL, messages=messages, tools=TOOLS, tool_choice='auto',
                  max_completion_tokens=MAX_TOKENS, temperature=0.2)
    if MODEL.startswith('openai/gpt-oss'):
        kwargs['reasoning_effort'] = 'medium'
    for attempt in range(TOOL_CALL_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except groq.BadRequestError as exc:
            if 'tool_use_failed' in str(exc) and attempt < TOOL_CALL_RETRIES:
                continue
            raise AgentFailed(f'Groq rejected the request: {exc.message}') from exc
        except groq.AuthenticationError as exc:
            raise AgentUnavailable('Groq credentials missing or invalid; set GROQ_API_KEY') from exc
        except groq.PermissionDeniedError as exc:
            raise AgentUnavailable(f'Groq API key lacks permission: {exc.message}') from exc
        except groq.RateLimitError as exc:
            raise AgentFailed('Groq rate limit reached; retry shortly') from exc
        except groq.APIStatusError as exc:
            raise AgentFailed(f'Groq API error {exc.status_code}: {exc.message}') from exc
        except groq.APIConnectionError as exc:
            raise AgentFailed('Could not reach the Groq API') from exc


def advise(question: Optional[str] = None, client=None,
           on_step: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Run the advisory loop and return the proposal with its deterministic validation.

    ``on_step`` (optional) is called with a small dict for every model turn and
    tool call, so callers can stream the agent's progress.
    """
    client = client or make_client()
    started = time.perf_counter()
    prompt = question or ('Assess the current grid state. Identify any projected overloads or islanding and '
                          'propose a mitigation plan.')
    messages: List[Dict[str, Any]] = [{'role': 'system', 'content': SYSTEM_PROMPT},
                                      {'role': 'user', 'content': prompt}]
    calls: List[Dict[str, Any]] = []
    usage = {'input_tokens': 0, 'output_tokens': 0}
    submitted = None
    for _turn in range(MAX_TURNS):
        response = _create(client, messages)
        if response.usage is not None:
            usage['input_tokens'] += response.usage.prompt_tokens or 0
            usage['output_tokens'] += response.usage.completion_tokens or 0
        choice = response.choices[0]
        if choice.finish_reason == 'length':
            raise AgentFailed('The model response was cut off at max_completion_tokens')
        message = choice.message
        tool_calls = message.tool_calls or []
        if message.content and on_step:
            on_step({'kind': 'thought', 'text': message.content[:600]})
        messages.append({'role': 'assistant', 'content': message.content or '',
                         **({'tool_calls': [{'id': tc.id, 'type': 'function',
                                             'function': {'name': tc.function.name,
                                                          'arguments': tc.function.arguments}}
                                            for tc in tool_calls]} if tool_calls else {})})
        if not tool_calls:
            messages.append({'role': 'user', 'content': 'Finish by calling submit_plan with your proposal.'})
            continue
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or '{}')
                if not isinstance(args, dict):
                    raise ValueError('arguments must be a JSON object')
            except ValueError as exc:
                calls.append({'tool': name, 'input': tc.function.arguments, 'error': f'invalid arguments: {exc}'})
                messages.append({'role': 'tool', 'tool_call_id': tc.id, 'name': name,
                                 'content': f'ERROR: invalid JSON arguments ({exc}); call the tool again.'})
                continue
            if name == 'submit_plan':
                submitted = args
                if on_step:
                    on_step({'kind': 'submit', 'plan': {k: args.get(k) for k in ('trip_breakers', 'bess_dispatch')},
                             'summary': args.get('summary', '')})
                messages.append({'role': 'tool', 'tool_call_id': tc.id, 'name': name, 'content': 'Plan received.'})
                continue
            handler = HANDLERS.get(name)
            try:
                if handler is None:
                    raise ValueError(f'unknown tool {name}')
                result = handler(args)
                calls.append({'tool': name, 'input': args,
                              'evidence_hashes': [e.get('evidence_hash') for e in result.get('evidence', [])]})
                content = json.dumps(_slim(result), default=str)
                if on_step:
                    on_step({'kind': 'tool', 'tool': name, 'input': args, 'result': _headline(name, result),
                             'cypher': [e.get('cypher') for e in result.get('evidence', [])][:3],
                             'evidence_hashes': calls[-1]['evidence_hashes'][:3],
                             'server_ms': round(sum(e.get('server_ms', 0) for e in result.get('evidence', [])), 3)})
            except Exception as exc:  # graph errors go back to the model as tool errors
                calls.append({'tool': name, 'input': args, 'error': str(exc)})
                if on_step:
                    on_step({'kind': 'tool_error', 'tool': name, 'input': args, 'error': str(exc)[:300]})
                content = 'ERROR: ' + (getattr(exc, 'message', None) or str(exc))
            messages.append({'role': 'tool', 'tool_call_id': tc.id, 'name': name, 'content': content})
        if submitted is not None:
            break
    if submitted is None:
        raise AgentFailed(f'No plan submitted within {MAX_TURNS} turns')

    plan = {'trip_breakers': list(submitted.get('trip_breakers') or []),
            'bess_dispatch': dict(submitted.get('bess_dispatch') or {})}
    validation = graph.evaluate_plan(plan)
    return {
        'provider': 'groq',
        'model': MODEL,
        'summary': submitted.get('summary', ''),
        'rationale': submitted.get('rationale', ''),
        'plan': plan,
        'validation': {k: validation[k] for k in ('verdict', 'reasons', 'state_version', 'relief_mw', 'breaker_checks', 'tier1')}
                      | {'projection_outcome': validation['projection']['outcome']},
        'validation_evidence': validation['evidence'],
        'tool_calls': calls,
        'usage': usage,
        'elapsed_s': round(time.perf_counter() - started, 2),
    }
