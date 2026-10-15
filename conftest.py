"""App-contract fixtures. These do not execute Cypher or measure FalkorDB."""
import copy
import pytest


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from backend import graph
    from backend.graph import topology as t
    from backend.simulator.grid_sim import grid_simulator as sim
    from backend.audit.logger import AuditLogger
    from backend.graph.thermal import project_loading
    import backend.simulator.grid_sim as sim_module

    store = AuditLogger(str(tmp_path / 'audit.db'))
    monkeypatch.setattr(sim_module, 'audit_logger', store)
    import backend.audit.logger as audit_module
    monkeypatch.setattr(audit_module, 'audit_logger', store)
    sim._init_grid_data()
    sim._lock = __import__('asyncio').Lock()
    sim._active_task = None
    sim._scenario_id = 0
    sim._scenario_status = 'NOMINAL'
    sim._evidence = []
    sim._active_cypher = ''
    sim._graphblas_latency_ms = None
    state = {'version': 0}
    evidence = [{'cypher': '// FIXTURE ONLY: no Cypher executed', 'params': {}, 'rows': [],
                 'evidence_hash': 'fixture', 'server_ms': 0.25}]

    def snapshot():
        return {'circuits': [{**r['props'], 'span_id': r['id'], 'circuit': r['circuit'],
                    'src': r['from'], 'dst': r['to'], 'rel_type': r['rel_type'],
                    'status': 'TRIPPED' if sim._spans[r['id']]['upper_status' if r['circuit']=='UPPER_PRIMARY' else 'lower_status'] in ('FAULT_TRIPPED','TRIPPED') else 'ENERGIZED'} for r in t.span_rows()],
            'feeds': [{'src': r['from'], 'dst': r['to'], 'output_mw':840} for r in t.feeds_rows()],
            'substations': [copy.deepcopy(sim._substation)],
            'consumers': [{**c, 'substation_id':'SUB_METRO'} for c in sim._consumers.values()],
            'bess': [{**sim._bess, 'substation_id':'SUB_METRO'}], 'state_version':state['version']}

    def trace(origin_id, **kwargs):
        proj = project_loading(snapshot())
        return {'summary': {'required_relief_mw':proj['required_relief_mw']},
            'projection':proj, 'evidence':copy.deepcopy(evidence), 'timing':{'graph_server_ms':0.25}}

    def assets(substation_id, required_mw):
        b = sim._bess
        batteries = [{**b, 'available_mw':b['max_discharge_mw']-b['active_injection_mw']}] if b['soc_pct']>40 else []
        loads = [dict(c) for c in sim._consumers.values() if c['priority_tier']==3 and c['energized']]
        total = sum(c['mw_demand'] for c in loads)+sum(b['available_mw'] for b in batteries)
        return {'bess':{'eligible':batteries},'sheddable_loads':{'eligible':loads},
            'meets_requirement':total>=required_mw,'evidence':copy.deepcopy(evidence)}

    def breaker(breaker_id):
        consumers = [c for c in sim._consumers.values() if c['breaker_id']==breaker_id]
        allowed = bool(consumers) and all(c['priority_tier']==3 and c['energized'] for c in consumers)
        return {'trip_allowed':allowed,'reasons':[] if allowed else ['PROTECTED_OR_UNKNOWN'],
                'evidence':copy.deepcopy(evidence)}

    def update(update):
        state['version'] += 1
        return {'state_version':state['version']}

    monkeypatch.setattr(graph, 'read_state_version', lambda client=None: state['version'])
    monkeypatch.setattr(graph.memory, 'recall', lambda *a, **k: {'incidents': [], 'playbooks': [], 'evidence': []})
    monkeypatch.setattr(graph.memory, 'record_incident', lambda *a, **k: {
        'incident_id': 'INC_FIXTURE', 'signature': 'FIXTURE', 'playbook': None, 'evidence': []})
    monkeypatch.setattr(graph, 'init_graph', lambda **kw: {})
    monkeypatch.setattr(graph, 'reset_grid', lambda: {})
    monkeypatch.setattr(graph, 'apply_state_update', update)
    monkeypatch.setattr(graph, 'trace_cascade', trace)
    monkeypatch.setattr(graph, 'find_mitigation_assets', assets)
    monkeypatch.setattr(graph, 'check_breaker', breaker)
    monkeypatch.setattr(graph, 'verify_tier1_protection', lambda *a: {
        'all_protected':project_loading(snapshot())['outcome']=='WITHIN_LIMITS', 'evidence':copy.deepcopy(evidence)})
    return sim, store
