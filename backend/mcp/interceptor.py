"""One cancellable scenario pipeline using graph evidence, memory and an optional AI agent.

Each phase holds the simulator lock and runs its blocking graph/SQLite work in
a worker thread, so the event loop (WebSocket telemetry, HTTP) stays
responsive. State is only mutated while the lock is held.

Modes:

* ``deterministic`` - trace, then reuse a proven playbook from graph memory if
  the graph guard still approves it, otherwise battery-first/Tier-3 greedy relief.
* ``agent`` - the LLM agent investigates with read-only graph tools (its steps
  stream into telemetry) and proposes a plan. The plan executes only if the
  graph guard approves it against current state; otherwise, or if the agent
  is unavailable, the deterministic pipeline takes over.

Every finished run is written to graph memory as an Incident.
"""
import asyncio
import time
from datetime import datetime, timezone

from backend import graph
from backend.simulator.grid_sim import grid_simulator as sim
from backend.mcp import server

ORIGINS = {1: 'TX_882', 2: 'TX_884', 3: 'SUB_METRO'}
QUESTIONS = {
    1: 'Lightning has tripped the primary circuit of span TX_882 (T3->T4). Investigate and propose a mitigation.',
    2: 'Wildfire smoke has tripped the primary circuit of span TX_884 (T5->T6). Investigate and propose a mitigation.',
    3: 'A 5 PM heatwave has raised every metro load by 20%. Investigate and propose a mitigation.',
}


def _same_plan(a, b):
    norm = lambda p: (sorted(p.get('trip_breakers') or []), {k: round(float(v), 1) for k, v in sorted((p.get('bess_dispatch') or {}).items())})
    return norm(a) == norm(b)


def _step(entry):
    sim._agent_steps.append({**entry, 'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds')})


class GridPulseInterceptor:
    def __init__(self):
        self._ctx = {}

    async def trigger_scenario(self, scenario_id, operator_id='SCADA_OPERATOR', mode='deterministic'):
        return await sim.trigger_scenario(scenario_id, operator_id, mode)

    async def reset(self, operator_id='OPERATOR_RESET'):
        return await sim.reset(operator_id)

    # --- phases (run in worker threads while the lock is held) -------------

    def _inject(self, run_id, scenario_id):
        sim.inject_fault(scenario_id)
        server.sync()
        trips = [{'span_id': s['id'], 'circuit': c}
                 for s in sim._spans.values()
                 for c, f in (('UPPER_PRIMARY', 'upper_status'), ('LOWER_BACKUP', 'lower_status'))
                 if s[f] in ('FAULT_TRIPPED', 'DE_ENERGIZED', 'TRIPPED')]
        self._ctx[run_id] = {'origin': ORIGINS[scenario_id], 'trips': trips,
                             'demand_scale': 1.2 if scenario_id == 3 else None, 'source': 'INTERCEPTOR',
                             'scenario': sim._scenario_name, 't0': time.perf_counter(), 'steps': 0}

    def _analyze(self, run_id, scenario_id):
        ctx = self._ctx[run_id]
        sim._scenario_status = 'ANALYZING'
        origin = ctx['origin']
        trace = server.falkordb_trace_blast_radius(origin)
        sim.apply_projection(trace['projection'])
        server.sync()
        sim._evidence = trace['evidence']
        sim._active_cypher = '\n\n'.join(e['cypher'] for e in trace['evidence'])
        sim._graphblas_latency_ms = trace['timing']['graph_server_ms']
        sim.record('CASCADE_TRACE', origin, 'Current-state dc-pf/1 projection', evidence=trace)
        ctx.update(overloads=[o['element'] for o in trace['projection']['overloads']],
                   relief=trace['summary']['required_relief_mw'], state_version=trace.get('state_version'),
                   downstream=trace['summary'].get('downstream_substations') or [])
        try:
            recall = graph.memory.recall(origin)
            learned = graph.memory.suggest(origin, ctx['trips'], ctx['demand_scale'])
            sim._memory = {'incidents': recall['incidents'][:5], 'playbooks': recall['playbooks'][:3],
                           'suggestions': learned['suggestions']}
            ctx['suggestions'] = learned['suggestions']
        except graph.GraphError:
            ctx['suggestions'] = []
        if not trace['projection'].get('relief_feasible', True):
            raise ValueError('No substation demand reduction can relieve the projected overload')
        required = trace['summary']['required_relief_mw']
        assets = server.falkordb_find_mitigation_assets('SUB_METRO', required)
        return required, assets

    def _playbook_plan(self, run_id):
        """The best remembered plan (same or similar fault) that the guard approves against the current state."""
        ctx = self._ctx[run_id]
        for suggestion in ctx.get('suggestions') or []:
            ctx['steps'] += 1
            verdict = graph.evaluate_plan(suggestion['plan'])
            if verdict['verdict'] == 'APPROVED':
                ctx['reused'] = suggestion
                return suggestion['plan']
        return None

    @staticmethod
    def _greedy_plan(required, assets):
        """Battery support up to the documented 45 MW, then isolated Tier-3 relief."""
        battery = next((b for b in assets['bess']['eligible'] if b['id'] == 'BESS_METRO_01'), None)
        # Dispatch is an absolute setpoint; available_mw is headroom on top of what is already injected.
        active = (battery.get('active_injection_mw') or 0.0) if battery else 0.0
        setpoint = min(45.0, active + battery['available_mw']) if battery else 0.0
        added = max(setpoint - active, 0.0)
        remaining = max(0, required - added)
        breakers = []
        for load in assets['sheddable_loads']['eligible']:
            if remaining <= 0:
                break
            breakers.append(load['breaker_id'])
            remaining -= load['mw_demand']
        if remaining > 0:
            raise ValueError('Insufficient safe Tier-3 shedding capacity')
        return {'trip_breakers': breakers,
                'bess_dispatch': {battery['id']: setpoint} if added > 0 and required > 0 else {}}

    def _execute(self, run_id, plan, reason):
        sim._scenario_status = 'MITIGATING'
        # Battery first: if it is refused, no customer has been shed yet.
        for bess_id, mw in (plan.get('bess_dispatch') or {}).items():
            server.scada_dispatch_bess(bess_id, mw)
        for breaker_id in plan.get('trip_breakers') or []:
            server.scada_trip_breaker(breaker_id, reason)
        server.sync()
        post = graph.trace_cascade('SUB_METRO')
        protection = graph.verify_tier1_protection('SUB_METRO')
        if post['projection']['outcome'] != 'WITHIN_LIMITS' or not protection['all_protected']:
            raise ValueError('Post-action verification failed: residual overload or Tier-1 risk')
        sim.apply_projection(post['projection'])
        # Render actual projected circuit flows from the graph-backed model.
        for s in sim._spans.values():
            rows = [c for c in post['projection']['circuits'] if c['span_id'] == s['id']]
            s['flow_mw'] = sum(c['flow_mw'] for c in rows)
            if s['upper_status'] == 'FAULT_TRIPPED' and sim._bess['active_injection_mw'] > 0:
                s['lower_status'] = 'REROUTED_BESS'
        sim._substation['status'] = 'ONLINE'
        server.sync()
        sim._scenario_status = 'MITIGATED'
        self._ctx[run_id]['plan'] = plan
        sim.record('MITIGATION_VERIFIED', 'SUB_METRO', sim._agent_log,
                   evidence={'trace': post, 'tier1': protection, 'plan': plan})

    def _remember(self, run_id, status, note):
        ctx = self._ctx.pop(run_id, None)
        if not ctx:
            return
        breakers = {c['breaker_id']: c['id'] for c in sim._consumers.values()}
        try:
            plan = ctx.get('plan') or {}
            shed = sum(c['mw_demand'] for c in sim._consumers.values() if c['breaker_id'] in (plan.get('trip_breakers') or []))
            stored = graph.memory.record_incident({
                'mitigation_ms': round((time.perf_counter() - ctx['t0']) * 1000.0, 1), 'steps': ctx.get('steps'),
                'shed_mw': round(shed, 3), 'downstream': ctx.get('downstream'),
                'reused_playbook': (ctx.get('reused') or {}).get('signature'),
                'run_id': run_id, 'source': ctx['source'], 'scenario': ctx['scenario'], 'origin_id': ctx['origin'],
                'trips': ctx['trips'], 'demand_scale': ctx['demand_scale'], 'overloads': ctx.get('overloads'),
                'required_relief_mw': ctx.get('relief'), 'state_version': ctx.get('state_version'),
                'status': status, 'final_outcome': 'WITHIN_LIMITS' if status == 'MITIGATED' else 'UNRESOLVED',
                'note': note, 'plan': ctx.get('plan'), 'breaker_targets': breakers})
            sim._memory = {**(sim._memory or {}), 'recorded_incident': stored['incident_id'],
                           'playbook': stored['playbook'], 'signature': stored['signature'],
                           'reused_playbook': stored.get('reused_playbook'), 'reused_credit': stored.get('reused_credit')}
        except graph.GraphError as exc:
            sim._agent_log += f' (memory not recorded: {exc})'

    # --- orchestration ------------------------------------------------------

    async def _deterministic(self, run_id, required, assets):
        async with sim._lock:
            if sim._run_id != run_id:
                return False
            plan = await asyncio.to_thread(self._playbook_plan, run_id)
            if plan:
                reused = self._ctx[run_id]['reused']
                sim._agent_log = (f"Reusing playbook {reused['signature']} from graph memory ({reused['why']}); "
                                  'the graph guard re-approved it for the current state.')
                self._ctx[run_id]['source'] = 'PLAYBOOK'
            else:
                self._ctx[run_id]['steps'] += 2  # asset discovery + greedy search
                plan = self._greedy_plan(required, assets)
            await asyncio.to_thread(self._execute, run_id, plan, 'Projected cascade overload relief')
            reused = self._ctx[run_id].get('reused')
            sim._agent_log = (f"Graph-verified mitigation complete (playbook reused: {reused['signature']}"
                              f"{'' if reused['exact'] else ', generalised from a similar fault'}). Tier-1 assets remain protected."
                              if reused else 'Graph-verified mitigation complete. Tier-1 assets remain protected.')
            return True

    async def _agent(self, run_id, scenario_id):
        """Returns True if the agent's plan was executed; False to fall back."""
        from backend.mcp import agent, quota
        _step({'kind': 'start', 'text': QUESTIONS[scenario_id]})
        try:
            quota.consume(self._ctx[run_id].get('operator'))
        except quota.QuotaExceeded as exc:
            _step({'kind': 'fallback', 'text': f'{exc}. Deterministic pipeline takes over.'})
            return False
        sim._agent_log = 'AI operator investigating with graph tools.'
        try:
            # The model runs without the lock so telemetry, reset and other reads keep working.
            advice = await asyncio.to_thread(agent.advise, QUESTIONS[scenario_id], None, _step, ORIGINS[scenario_id])
        except (agent.AgentUnavailable, agent.AgentFailed) as exc:
            _step({'kind': 'fallback', 'text': f'Agent unavailable: {exc}. Deterministic pipeline takes over.'})
            return False
        _step({'kind': 'verdict', 'verdict': advice['validation']['verdict'], 'reasons': advice['validation']['reasons'],
               'summary': advice['summary']})
        async with sim._lock:
            if sim._run_id != run_id:
                return True
            await asyncio.to_thread(sim.record, 'AGENT_PROPOSAL', 'SUB_METRO', advice['summary'] or 'Agent proposal',
                                    'SUCCESS' if advice['validation']['verdict'] == 'APPROVED' else 'REJECTED',
                                    {k: advice[k] for k in ('model', 'plan', 'validation', 'tool_calls')}, advice['plan'])
            if advice['validation']['verdict'] != 'APPROVED':
                _step({'kind': 'fallback', 'text': 'Graph guard rejected the agent plan. Deterministic pipeline takes over.'})
                return False
            await asyncio.to_thread(server.sync)
            recheck = await asyncio.to_thread(graph.evaluate_plan, advice['plan'])
            if recheck['verdict'] != 'APPROVED':
                _step({'kind': 'fallback', 'text': f"State changed; plan no longer approved {recheck['reasons']}."})
                return False
            self._ctx[run_id]['source'] = 'AGENT'
            self._ctx[run_id]['steps'] = len(advice['tool_calls']) + 1
            same = [sg for sg in self._ctx[run_id].get('suggestions') or [] if _same_plan(sg['plan'], advice['plan'])]
            if same:
                self._ctx[run_id]['reused'] = same[0]
            sim._agent_log = f"Executing agent plan approved by the graph guard: {advice['summary']}"
            await asyncio.to_thread(self._execute, run_id, advice['plan'], 'Agent plan approved by graph guard')
            sim._agent_log = f"AI operator mitigation verified. {advice['summary']}"
            _step({'kind': 'done', 'text': 'Plan executed and verified; Tier-1 assets protected.'})
            return True

    async def run(self, run_id, scenario_id, mode='deterministic', operator=None):
        try:
            async with sim._lock:
                if sim._run_id != run_id:
                    return
                await asyncio.to_thread(self._inject, run_id, scenario_id)
                self._ctx[run_id]['operator'] = operator
            await asyncio.sleep(0.25)
            async with sim._lock:
                if sim._run_id != run_id:
                    return
                required, assets = await asyncio.to_thread(self._analyze, run_id, scenario_id)
            await asyncio.sleep(0.25)
            done = False
            if mode == 'agent':
                done = await self._agent(run_id, scenario_id)
            if not done and sim._run_id == run_id:
                await self._deterministic(run_id, required, assets)
            if sim._run_id == run_id and sim._scenario_status == 'MITIGATED':
                await asyncio.to_thread(self._remember, run_id, 'MITIGATED', sim._agent_log)
        except asyncio.CancelledError:
            self._ctx.pop(run_id, None)
            raise
        except Exception as exc:
            if sim._run_id == run_id:
                sim._scenario_status = 'FAILED'
                sim._agent_log = f'Mitigation failed: {exc}'
                await asyncio.to_thread(sim.record, 'MITIGATION_FAILED', 'SUB_METRO', str(exc), 'FAILED')
                await asyncio.to_thread(self._remember, run_id, 'FAILED', str(exc))
            else:
                self._ctx.pop(run_id, None)


_interceptor = GridPulseInterceptor()


def get_interceptor():
    return _interceptor
