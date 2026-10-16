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
MAX_TURNS = 8
MAX_TOKENS = 8192
# Groq occasionally rejects a malformed tool call (400 tool_use_failed); retry the turn this many times.
TOOL_CALL_RETRIES = 2

SYSTEM_PROMPT = """You are the GridPulse grid-security agent for a 500 kV grid stored in FalkorDB.
Goal: the smallest mitigation that brings every circuit, substation and plant back within limits while every
Tier-1 consumer (hospitals) stays supplied.

The user message includes a GRAPH BRIEFING: results of FalkorDB queries already run for you (memory of past
incidents and playbooks near the fault, the blast-radius trace with the power-flow projection, and the
batteries / sheddable Tier-3 loads available). Treat it as ground truth for the current state.

Rules:
- Ground every claim in the briefing or tool results; cite IDs exactly.
- Prefer battery support (absolute MW setpoint, at most the listed available MW) before shedding Tier-3 loads,
  and shed only what is needed. Never trip a breaker that is not listed as sheddable.
- A remembered playbook is a hint: reuse it if it fits, but always verify.
- Call evaluate_plan on your candidate plan. If REJECTED, read the reasons and revise.
- Use other tools only if the briefing is missing something (e.g. the fault is hypothetical: trace_cascade with
  assume_tripped).
- Finish by calling submit_plan exactly once (empty plan if nothing is needed, with the reason).
- You cannot actuate anything; the graph guard re-checks your plan and an operator decides."""


def _compact(name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Small, decision-relevant view of a tool result (the free Groq tier is token-rate-limited)."""
    hashes = [e.get('evidence_hash', '')[:19] for e in result.get('evidence', [])][:3]
    if name == 'trace_cascade':
        proj = result.get('projection', {})
        sm = result.get('summary', {})
        return {'outcome': proj.get('outcome'), 'required_relief_mw': proj.get('required_relief_mw'),
                'overloads': [{k: o[k] for k in ('element', 'kind', 'loading_pct')} for o in proj.get('overloads', [])][:8],
                'islanded_substations': proj.get('islanded_substations'), 'downstream_substations': sm.get('downstream_substations'),
                'tier1_at_risk': sm.get('tier1_at_risk'), 'beyond_max_hops': result.get('reachability', {}).get('beyond_max_hops'),
                'evidence': hashes}
    if name == 'find_mitigation_assets':
        return {'batteries': [{'id': b['id'], 'available_mw': b['available_mw'], 'soc_pct': b.get('soc_pct')} for b in result['bess']['eligible']],
                'ineligible_batteries': [{'id': b['id'], 'reasons': b['reasons']} for b in result['bess']['ineligible']],
                'sheddable_loads': [{'id': c['id'], 'breaker_id': c['breaker_id'], 'mw': c['mw_demand']} for c in result['sheddable_loads']['eligible']],
                'totals': result.get('totals'), 'meets_requirement': result.get('meets_requirement'), 'evidence': hashes}
    if name == 'evaluate_plan':
        proj = result.get('projection', {})
        return {'verdict': result.get('verdict'), 'reasons': result.get('reasons'), 'relief_mw': result.get('relief_mw'),
                'projected_outcome': proj.get('outcome'),
                'remaining_overloads': [f"{o['element']} {o['loading_pct']}%" for o in proj.get('overloads', [])][:6], 'evidence': hashes}
    if name == 'recall_memory':
        return {'incidents': [{k: i.get(k) for k in ('id', 'signature', 'status', 'source', 'actions', 'places')} for i in result.get('incidents', [])][:4],
                'playbooks': result.get('playbooks', [])[:3], 'suggestions': result.get('suggestions', [])[:3], 'evidence': hashes}
    if name == 'check_breaker':
        return {k: result.get(k) for k in ('breaker_id', 'trip_allowed', 'reasons', 'shed_mw')} | {'evidence': hashes}
    if name == 'verify_tier1_protection':
        return {'outcome': result.get('outcome'), 'assets': [{k: a.get(k) for k in ('id', 'protected', 'at_risk')} for a in result.get('assets', [])],
                'evidence': hashes}
    if name == 'reroute_paths':
        return {'outcome': result.get('outcome'), 'paths': [{'nodes': p['nodes'], 'impedance_pu': p['impedance_pu']} for p in result.get('paths', [])][:3]}
    if name == 'criticality_ranking':
        return {'ranking': [{k: r[k] for k in ('id', 'betweenness')} for r in result.get('ranking', [])][:8]}
    if name == 'find_islands':
        return {'components': [{k: c[k] for k in ('size', 'has_generator')} for c in result.get('components', [])]}
    return {k: v for k, v in result.items() if k not in ('evidence', 'timing')}


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
        sg = result.get('suggestions') or []
        return (f"{len(result.get('incidents', []))} past incidents, {len(result.get('playbooks', []))} playbooks nearby"
                + (f"; best learned plan {sg[0]['signature']} (score {sg[0]['score']})" if sg else ''))
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
    _tool('trace_cascade', 'Blast radius of a span/node and the power-flow projection; assume_tripped adds hypothetical trips.',
          {'origin_id': {'type': 'string', 'description': 'Span or node id, e.g. TX_882 or SUB_METRO'},
           'max_hops': {'type': 'integer', 'minimum': 1, 'maximum': 12, 'description': 'default 10; TX_882 needs 6 and HYDRO_ALPINE_01 needs 10 to reach SUB_METRO'},
           'assume_tripped': {'type': 'array', 'items': CIRCUIT_REF}}, ['origin_id']),
    _tool('find_mitigation_assets', 'Usable batteries and sheddable Tier-3 loads at a substation.',
          {'substation_id': {'type': 'string'}, 'required_mw': {'type': 'number', 'minimum': 0}},
          ['substation_id', 'required_mw']),
    _tool('check_breaker', 'Whether a breaker may be tripped (Tier-1/2 guard) and the MW it sheds.',
          {'breaker_id': {'type': 'string'}}, ['breaker_id']),
    _tool('verify_tier1_protection', 'Supply and projected risk for every Tier-1 consumer.',
          {'substation_id': {'type': 'string'}}, []),
    _tool('reroute_paths', 'Lowest-impedance energised paths between two nodes (algo.SPpaths).',
          {'source_id': {'type': 'string'}, 'target_id': {'type': 'string'},
           'k': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, []),
    _tool('criticality_ranking', 'Buses ranked by betweenness centrality.',
          {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}, []),
    _tool('recall_memory', 'Past incidents and learned playbooks within N hops of a span or node.',
          {'origin_id': {'type': 'string'}, 'hops': {'type': 'integer', 'minimum': 0, 'maximum': 6}}, ['origin_id']),
    _tool('find_islands', 'Disconnected parts of the grid (algo.WCC).',
          {}, []),
    _tool('evaluate_plan', 'Check a plan against the actuation guards (what-if, never actuates): APPROVED or REJECTED with reasons.',
          PLAN_PROPS, []),
    _tool('submit_plan', 'Submit the final mitigation proposal. Call exactly once, last.',
          {**PLAN_PROPS, 'summary': {'type': 'string', 'description': 'Two or three sentences for the operator.'},
           'rationale': {'type': 'string', 'description': 'Why this plan, citing tool results.'}},
          ['summary', 'rationale', 'trip_breakers', 'bess_dispatch']),
]

HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    'trace_cascade': lambda a: graph.trace_cascade(a['origin_id'], max_hops=a.get('max_hops', 10),
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
        # Retries are handled in _create so rate-limit waits show up as agent steps.
        return groq.Groq(api_key=key, max_retries=0, timeout=60.0)
    except groq.GroqError as exc:
        raise AgentUnavailable('No Groq credentials configured; set GROQ_API_KEY') from exc


RATE_LIMIT_BUDGET_S = 90.0


def _retry_after(exc) -> float:
    try:
        return min(max(float(exc.response.headers.get('retry-after', '5')), 1.0), 30.0)
    except (AttributeError, TypeError, ValueError):
        return 5.0


def _create(client, messages, on_step: Optional[Callable[[Dict[str, Any]], None]] = None):
    import groq
    kwargs = dict(model=MODEL, messages=messages, tools=TOOLS, tool_choice='auto',
                  max_completion_tokens=MAX_TOKENS, temperature=0.2)
    if MODEL.startswith('openai/gpt-oss'):
        kwargs['reasoning_effort'] = 'medium'
    waited = 0.0
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except groq.RateLimitError as exc:
            # Free-tier tokens-per-minute limit: wait as told, visibly, within a bounded budget.
            delay = _retry_after(exc)
            if waited + delay > RATE_LIMIT_BUDGET_S:
                raise AgentFailed('Groq rate limit: try again in a minute') from exc
            if on_step:
                on_step({'kind': 'wait', 'text': f'Groq free-tier rate limit: waiting {delay:.0f}s'})
            time.sleep(delay)
            waited += delay
            continue
        except groq.BadRequestError as exc:
            if 'tool_use_failed' in str(exc) and attempt < TOOL_CALL_RETRIES:
                attempt += 1
                continue
            raise AgentFailed(f'Groq rejected the request: {exc.message}') from exc
        except groq.AuthenticationError as exc:
            raise AgentUnavailable('Groq credentials missing or invalid; set GROQ_API_KEY') from exc
        except groq.PermissionDeniedError as exc:
            raise AgentUnavailable(f'Groq API key lacks permission: {exc.message}') from exc
        except groq.APIStatusError as exc:
            raise AgentFailed(f'Groq API error {exc.status_code}: {exc.message}') from exc
        except groq.APIConnectionError as exc:
            if attempt < TOOL_CALL_RETRIES:
                attempt += 1
                time.sleep(1.0)
                continue
            raise AgentFailed('Could not reach the Groq API') from exc


_ID_IN_TEXT = __import__('re').compile(r"\b(TX_\d{3}|T\d{1,2}|SUB_[A-Z0-9_]+|HYDRO_[A-Z0-9_]+|BR\d{3}|B\d{3})\b")


def briefing(question: str, origin_id: Optional[str] = None,
             on_step: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
    """Run the core graph queries up front and return a compact text briefing for the model.

    This is the graph acting as the agent's context layer: memory recall, blast radius with
    power-flow projection, and mitigation assets, each streamed as a step with its Cypher.
    """
    origin = origin_id or next(iter(_ID_IN_TEXT.findall(question.upper())), None) or 'SUB_METRO'
    lines = [f'Fault origin: {origin}.']

    def step(tool, args, result):
        if on_step:
            on_step({'kind': 'context', 'tool': tool, 'input': args, 'result': _headline(tool, result),
                     'cypher': [e.get('cypher') for e in result.get('evidence', [])][:3],
                     'evidence_hashes': [e.get('evidence_hash') for e in result.get('evidence', [])][:3],
                     'server_ms': round(sum(e.get('server_ms', 0) for e in result.get('evidence', [])), 3)})

    try:
        mem = graph.memory.recall(origin, 2)
        snapshot, _ = graph.read_grid_snapshot()
        trips = [{'span_id': c['span_id'], 'circuit': c['circuit']} for c in snapshot['circuits'] if c['status'] == 'TRIPPED']
        learned = graph.memory.suggest(origin, trips)
        mem['suggestions'] = [{k: sg[k] for k in ('signature', 'plan', 'score', 'why')} for sg in learned['suggestions']]
        mem['evidence'] = mem['evidence'] + learned['evidence']
        step('recall_memory', {'origin_id': origin, 'hops': 2}, mem)
        lines.append('MEMORY (past incidents nearby; suggestions = learned plans ranked for this fault) '
                     + json.dumps(_compact('recall_memory', mem), default=str, separators=(',', ':')))
    except graph.GraphError as exc:
        lines.append(f'MEMORY unavailable: {exc}')
    try:
        trace = graph.trace_cascade(origin, max_hops=10)
        step('trace_cascade', {'origin_id': origin, 'max_hops': 10}, trace)
        lines.append('TRACE (current stored state) ' + json.dumps(_compact('trace_cascade', trace), default=str, separators=(',', ':')))
        subs = trace['summary']['downstream_substations'] or [s['id'] for s in trace['projection']['substations'] if s['overload']]
        relief = trace['projection'].get('required_relief_mw') or 0.0
        for sid in subs[:2]:
            assets = graph.find_mitigation_assets(sid, relief)
            step('find_mitigation_assets', {'substation_id': sid, 'required_mw': relief}, assets)
            lines.append(f'ASSETS at {sid} ' + json.dumps(_compact('find_mitigation_assets', assets), default=str, separators=(',', ':')))
    except graph.GraphError as exc:
        lines.append(f'TRACE unavailable for {origin}: {exc}')
    return '\n'.join(lines)


def _plan_from(submitted):
    """The submit_plan arguments as a plan, or AgentFailed so the caller can fall back."""
    breakers = submitted.get('trip_breakers') or []
    dispatch = submitted.get('bess_dispatch') or {}
    if (not isinstance(breakers, list) or not all(isinstance(b, str) for b in breakers)
            or not isinstance(dispatch, dict)
            or not all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
                       for k, v in dispatch.items())):
        raise AgentFailed('Submitted plan is malformed: trip_breakers must be a list of ids and '
                          'bess_dispatch an object of id -> MW')
    return {'trip_breakers': list(breakers), 'bess_dispatch': dict(dispatch)}


def advise(question: Optional[str] = None, client=None,
           on_step: Optional[Callable[[Dict[str, Any]], None]] = None, origin_id: Optional[str] = None) -> Dict[str, Any]:
    """Run the advisory loop and return the proposal with its deterministic validation.

    ``on_step`` (optional) is called with a small dict for every model turn and
    tool call, so callers can stream the agent's progress.
    """
    client = client or make_client()
    started = time.perf_counter()
    prompt = question or ('Assess the current grid state. Identify any projected overloads or islanding and '
                          'propose a mitigation plan.')
    context = briefing(prompt, origin_id, on_step)
    messages: List[Dict[str, Any]] = [{'role': 'system', 'content': SYSTEM_PROMPT},
                                      {'role': 'user', 'content': f'{prompt}\n\nGRAPH BRIEFING (FalkorDB, just queried):\n{context}'}]
    calls: List[Dict[str, Any]] = []
    usage = {'input_tokens': 0, 'output_tokens': 0}
    submitted = None
    for _turn in range(MAX_TURNS):
        response = _create(client, messages, on_step)
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
                content = json.dumps(_compact(name, result), default=str, separators=(',', ':'))
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

    plan = _plan_from(submitted)
    try:
        validation = graph.evaluate_plan(plan)
    except graph.InvalidRequestError as exc:
        raise AgentFailed(f'Submitted plan is malformed: {exc}') from exc
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
