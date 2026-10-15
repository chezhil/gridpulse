"""Claude advisory agent: the model investigates, the graph decides.

Claude gets read-only graph tools (trace, mitigation discovery, breaker guard,
Tier-1 audit, reroute paths, criticality, plan evaluation) and must finish by
calling ``submit_plan``. The proposed plan is then checked deterministically
by :func:`graph.evaluate_plan` - the same guards actuation uses. Nothing the
model says can authorize an action: execution (opt-in, operator role only)
re-evaluates the plan against current state and goes through the guarded
SCADA tools, each of which re-checks the graph and writes audit records.

Requires Anthropic credentials (``ANTHROPIC_API_KEY`` or an ``ant auth`` profile).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from backend import graph

MODEL = os.environ.get('GRIDPULSE_AGENT_MODEL', 'claude-opus-5-5')
MAX_TURNS = 12
MAX_TOKENS = 16000

SYSTEM_PROMPT = """You are the GridPulse grid-security advisor for a 500 kV corridor feeding a metropolitan substation.
Your job: investigate the current grid state with the tools, then propose the smallest mitigation that brings every
circuit and substation back within limits while keeping every Tier-1 consumer (the St. Jude trauma center) supplied.

Rules:
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


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {'name': name, 'description': description,
            'input_schema': {'type': 'object', 'properties': properties, 'required': required,
                             'additionalProperties': False}}


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
    'evaluate_plan': lambda a: graph.evaluate_plan({'trip_breakers': a.get('trip_breakers', []),
                                                    'bess_dispatch': a.get('bess_dispatch', {})}),
}


class AgentUnavailable(RuntimeError):
    """No usable Anthropic client (missing credentials or SDK)."""


class AgentFailed(RuntimeError):
    """The model did not produce a usable plan."""


def make_client():
    try:
        import anthropic
    except ImportError as exc:
        raise AgentUnavailable('anthropic SDK not installed; install backend/requirements.txt') from exc
    try:
        return anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        raise AgentUnavailable(f'Anthropic client could not be created: {exc}') from exc


def _create(client, messages):
    import anthropic
    kwargs = dict(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
                  output_config={'effort': 'medium'})
    try:
        # Server-side refusal fallback: a policy decline is re-run on a fallback model within the same call.
        return client.beta.messages.create(betas=['server-side-fallback-2026-07-01'], fallbacks='default', **kwargs)
    except TypeError as exc:
        # The SDK raises TypeError when no API key, token or profile can be resolved.
        if 'authentication' in str(exc).lower():
            raise AgentUnavailable('No Anthropic credentials configured; set ANTHROPIC_API_KEY') from exc
        raise
    except anthropic.AuthenticationError as exc:
        raise AgentUnavailable('Anthropic credentials missing or invalid; set ANTHROPIC_API_KEY') from exc
    except anthropic.PermissionDeniedError as exc:
        raise AgentUnavailable(f'Anthropic API key lacks permission: {exc.message}') from exc
    except anthropic.RateLimitError as exc:
        raise AgentFailed('Anthropic rate limit reached; retry shortly') from exc
    except anthropic.APIStatusError as exc:
        raise AgentFailed(f'Anthropic API error {exc.status_code}: {exc.message}') from exc
    except anthropic.APIConnectionError as exc:
        raise AgentFailed('Could not reach the Anthropic API') from exc


def advise(question: Optional[str] = None, client=None) -> Dict[str, Any]:
    """Run the advisory loop and return the proposal with its deterministic validation."""
    client = client or make_client()
    started = time.perf_counter()
    prompt = question or ('Assess the current grid state. Identify any projected overloads or islanding and '
                          'propose a mitigation plan.')
    messages: List[Dict[str, Any]] = [{'role': 'user', 'content': prompt}]
    calls: List[Dict[str, Any]] = []
    usage = {'input_tokens': 0, 'output_tokens': 0}
    submitted = None
    for _turn in range(MAX_TURNS):
        response = _create(client, messages)
        usage['input_tokens'] += getattr(response.usage, 'input_tokens', 0) or 0
        usage['output_tokens'] += getattr(response.usage, 'output_tokens', 0) or 0
        if response.stop_reason == 'refusal':
            raise AgentFailed('The model declined this request')
        if response.stop_reason == 'max_tokens':
            raise AgentFailed('The model response was cut off at max_tokens')
        messages.append({'role': 'assistant', 'content': response.content})
        tool_uses = [b for b in response.content if b.type == 'tool_use']
        if not tool_uses:
            if response.stop_reason == 'pause_turn':
                continue
            messages.append({'role': 'user', 'content': 'Finish by calling submit_plan with your proposal.'})
            continue
        results = []
        for block in tool_uses:
            args = block.input if isinstance(block.input, dict) else {}
            if block.name == 'submit_plan':
                submitted = args
                results.append({'type': 'tool_result', 'tool_use_id': block.id, 'content': 'Plan received.'})
                continue
            handler = HANDLERS.get(block.name)
            try:
                if handler is None:
                    raise ValueError(f'unknown tool {block.name}')
                result = handler(args)
                calls.append({'tool': block.name, 'input': args,
                              'evidence_hashes': [e.get('evidence_hash') for e in result.get('evidence', [])]})
                results.append({'type': 'tool_result', 'tool_use_id': block.id,
                                'content': json.dumps(_slim(result), default=str)})
            except Exception as exc:  # graph errors go back to the model as tool errors
                calls.append({'tool': block.name, 'input': args, 'error': str(exc)})
                results.append({'type': 'tool_result', 'tool_use_id': block.id, 'is_error': True,
                                'content': getattr(exc, 'message', None) or str(exc)})
        messages.append({'role': 'user', 'content': results})
        if submitted is not None:
            break
    if submitted is None:
        raise AgentFailed(f'No plan submitted within {MAX_TURNS} turns')

    plan = {'trip_breakers': list(submitted.get('trip_breakers') or []),
            'bess_dispatch': dict(submitted.get('bess_dispatch') or {})}
    validation = graph.evaluate_plan(plan)
    return {
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
