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


def falkordb_trace_blast_radius(origin_node_id='TX_882', max_hops=6):
    sync()
    return graph.trace_cascade(origin_node_id, max_hops=max_hops)


def falkordb_find_mitigation_assets(substation_id='SUB_METRO', required_mw=0):
    sync()
    return graph.find_mitigation_assets(substation_id, required_mw)


def _actuate(action, target, request, authorize, apply, reason):
    evidence = None
    try:
        sync()
        evidence = authorize()
        # Persist complete executed query/params/results BEFORE changing state.
        intent = sim.record(action, target, reason, 'REQUESTED', evidence=evidence, requested_action=request)
        result = apply()
        sync()
        sim.record(action, target, reason, evidence={**evidence, 'intent_id': intent['id'], 'result': result},
                   requested_action=request)
        return {**result, 'evidence': evidence, 'evidence_hash': intent['evidence_hash']}
    except Exception as exc:
        sim.record(action, target, str(exc), 'FAILED', evidence=evidence, requested_action=request)
        raise


def scada_trip_breaker(breaker_id, reason_code, cypher_audit_hash=None):
    def authorize():
        check = graph.check_breaker(breaker_id)
        if not check['trip_allowed']:
            sim.record('SCADA_TRIP_BREAKER', breaker_id, 'Graph guard rejected breaker', 'REJECTED', evidence=check)
            raise PermissionError(', '.join(check['reasons']))
        # The caller's hash is never sufficient. Fresh graph guard evidence is authoritative.
        return check
    return _actuate('SCADA_TRIP_BREAKER', breaker_id, {'breaker_id': breaker_id}, authorize,
        lambda: sim.apply_breaker_trip(breaker_id, reason_code, ''), reason_code)


def scada_dispatch_bess(bess_id, inject_mw, target_circuit='LOWER_BACKUP'):
    def authorize():
        assets = graph.find_mitigation_assets('SUB_METRO', inject_mw)
        candidate = next((b for b in assets['bess']['eligible'] if b['id'] == bess_id), None)
        if candidate is None or inject_mw > candidate['available_mw']:
            sim.record('SCADA_DISPATCH_BESS', bess_id, 'Graph guard rejected dispatch', 'REJECTED', evidence=assets)
            raise ValueError('Battery unavailable or insufficient discharge headroom')
        if target_circuit != 'LOWER_BACKUP':
            raise ValueError('Dispatch target must be LOWER_BACKUP')
        return assets
    return _actuate('SCADA_DISPATCH_BESS', bess_id,
        {'bess_id': bess_id, 'inject_mw': inject_mw, 'target_circuit': target_circuit}, authorize,
        lambda: sim.apply_bess_dispatch(bess_id, inject_mw, target_circuit), 'Graph-authorized battery support')
