"""One cancellable deterministic scenario pipeline using graph evidence."""
import asyncio
from backend import graph
from backend.simulator.grid_sim import grid_simulator as sim
from backend.mcp import server


class GridPulseInterceptor:
    async def trigger_scenario(self, scenario_id, operator_id='SCADA_OPERATOR'):
        return await sim.trigger_scenario(scenario_id, operator_id)

    async def reset(self, operator_id='OPERATOR_RESET'):
        return await sim.reset(operator_id)

    async def run(self, run_id, scenario_id):
        try:
            async with sim._lock:
                if sim._run_id != run_id:
                    return
                sim.inject_fault(scenario_id)
            await asyncio.sleep(0.25)
            async with sim._lock:
                if sim._run_id != run_id:
                    return
                sim._scenario_status = 'ANALYZING'
                # Span origins resolve at their destination: TX_882 reaches SUB_METRO in 6 hops.
                origin = {1: 'TX_882', 2: 'TX_884', 3: 'SUB_METRO'}[scenario_id]
                trace = server.falkordb_trace_blast_radius(origin)
                sim.apply_projection(trace['projection'])
                server.sync()
                sim._evidence = trace['evidence']
                sim._active_cypher = '\n\n'.join(e['cypher'] for e in trace['evidence'])
                sim._graphblas_latency_ms = trace['timing']['graph_server_ms']
                sim.record('CASCADE_TRACE', origin, 'Current-state radial-dc/1 projection', evidence=trace)
                required = trace['summary']['required_relief_mw']
                assets = server.falkordb_find_mitigation_assets('SUB_METRO', required)
                if not assets['meets_requirement']:
                    raise ValueError('Available safe mitigation cannot meet required relief')
            await asyncio.sleep(0.25)
            async with sim._lock:
                if sim._run_id != run_id:
                    return
                sim._scenario_status = 'MITIGATING'
                # Prefer battery support up to the documented 45 MW, then isolated Tier-3 relief.
                batteries = assets['bess']['eligible']
                battery = next((b for b in batteries if b['id'] == 'BESS_METRO_01'), None)
                planned = min(45.0, battery['available_mw']) if battery else 0.0
                remaining = max(0, required - planned)
                for load in assets['sheddable_loads']['eligible']:
                    if remaining <= 0:
                        break
                    server.scada_trip_breaker(load['breaker_id'], 'Projected cascade overload relief')
                    remaining -= load['mw_demand']
                if remaining > 0:
                    raise ValueError('Insufficient safe Tier-3 shedding capacity')
                if planned > 0 and required > 0:
                    server.scada_dispatch_bess(battery['id'], planned)
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
                sim._agent_log = 'Graph-verified mitigation complete. Tier-1 assets remain protected.'
                sim.record('MITIGATION_VERIFIED', 'SUB_METRO', sim._agent_log,
                           evidence={'trace': post, 'tier1': protection})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if sim._run_id == run_id:
                sim._scenario_status = 'FAILED'
                sim._agent_log = f'Mitigation failed: {exc}'
                sim.record('MITIGATION_FAILED', 'SUB_METRO', str(exc), 'FAILED')


_interceptor = GridPulseInterceptor()


def get_interceptor():
    return _interceptor
