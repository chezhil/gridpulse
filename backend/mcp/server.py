"""SCADA tools backed exclusively by executed FalkorDB queries.

No second grid model, synthesized traces, supplied-hash authorization, or
simulated query timings. All actuation targets the educational simulator.
Call mutation tools while holding the simulator's asyncio state lock.
"""
from backend import graph
from backend.simulator.grid_sim import grid_simulator as sim


def generate_cypher_audit_hash(query, params, results):
    return graph.evidence_hash(query, params, results)


def sync():
    return graph.apply_state_update(sim.graph_update())


def falkordb_trace_blast_radius(origin_node_id='TX_882', max_hops=10):
    sync()
    return graph.trace_cascade(origin_node_id, max_hops=max_hops)


def falkordb_find_mitigation_assets(substation_id='SUB_METRO', required_mw=0):
    sync()
    return graph.find_mitigation_assets(substation_id, required_mw)


def _actuate(action, target, request, authorize, apply, reason, operator_id):
    evidence = None
    intent = None
    result = None
    try:
        sync()
        evidence = authorize()
        # Persist complete executed query/params/results BEFORE changing state.
        intent = sim.record(action, target, reason, 'REQUESTED', evidence=evidence, requested_action=request, operator_id=operator_id)
        result = apply()
        sync()
        post = graph.trace_cascade('SUB_METRO')
        sim.apply_projection(post['projection'])
        sync()
        evidence = {**evidence, 'post_action_trace': post}
        completion = sim.record(action, target, reason, evidence={**evidence, 'intent_id': intent['id'], 'result': result},
                   requested_action=request, operator_id=operator_id)
        return {**result, 'evidence': completion['evidence'], 'evidence_hash': completion['evidence_hash']}
    except Exception as exc:
        failure = {'guard': evidence, 'intent_id': intent['id'] if intent else None,
                   'applied_result': result, 'state_changed': result is not None}
        if result is not None:
            sim._scenario_status = 'FAILED'
            sim._substation['status'] = 'OVERLOAD_RISK'
            sim._agent_log = f'Action applied but confirmation failed: {exc}. Reset required.'
        sim.record(action, target, str(exc), 'FAILED', evidence=failure, requested_action=request, operator_id=operator_id)
        raise


def scada_trip_breaker(breaker_id, reason_code, cypher_audit_hash=None, operator_id='SCADA_MCP_AGENT'):
    def authorize():
        check = graph.check_breaker(breaker_id)
        if not check['trip_allowed']:
            sim.record('SCADA_TRIP_BREAKER', breaker_id, 'Graph guard rejected breaker', 'REJECTED', evidence=check, operator_id=operator_id)
            raise PermissionError(', '.join(check['reasons']))
        # The caller's hash is never sufficient. Fresh graph guard evidence is authoritative.
        return check
    return _actuate('SCADA_TRIP_BREAKER', breaker_id, {'breaker_id': breaker_id}, authorize,
        lambda: sim.apply_breaker_trip(breaker_id, reason_code, ''), reason_code, operator_id)


def scada_dispatch_bess(bess_id, inject_mw, target_circuit='LOWER_BACKUP', operator_id='SCADA_MCP_AGENT'):
    def authorize():
        assets = graph.find_mitigation_assets('SUB_METRO', inject_mw)
        candidates = assets['bess']['eligible'] + [b for b in assets['bess'].get('ineligible', [])
            if b.get('reasons') == ['NO_DISCHARGE_HEADROOM']]
        candidate = next((b for b in candidates if b['id'] == bess_id), None)
        # The simulator command is an absolute setpoint, not an incremental injection.
        capacity = candidate['available_mw'] + candidate.get('active_injection_mw', 0) if candidate else 0
        if candidate is None or inject_mw > capacity:
            sim.record('SCADA_DISPATCH_BESS', bess_id, 'Graph guard rejected dispatch', 'REJECTED', evidence=assets, operator_id=operator_id)
            raise ValueError('Battery unavailable or insufficient discharge headroom')
        if target_circuit != 'LOWER_BACKUP':
            raise ValueError('Dispatch target must be LOWER_BACKUP')
        return assets
    return _actuate('SCADA_DISPATCH_BESS', bess_id,
        {'bess_id': bess_id, 'inject_mw': inject_mw, 'target_circuit': target_circuit}, authorize,
        lambda: sim.apply_bess_dispatch(bess_id, inject_mw, target_circuit), 'Graph-authorized battery support', operator_id)
