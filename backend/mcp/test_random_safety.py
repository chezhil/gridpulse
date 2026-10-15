"""Randomised safety properties of the real interceptor pipeline against FalkorDB.

Each seed injects random circuit trips, demand and battery state, runs the
deterministic interceptor end to end, and checks invariants that must hold
whether mitigation succeeds or fails.
"""
import asyncio
import random

import pytest

from backend import graph
from backend.graph import topology
from backend.mcp.test_live_runtime import live_runtime  # noqa: F401  (fixture)

SEEDS = range(16)


def randomise(sim, rng):
    trips = rng.sample([(s['id'], c) for s in topology.SPAN_SEGMENTS for c in topology.CIRCUITS], rng.choice([1, 1, 2]))
    scale = rng.uniform(0.85, 1.25)
    sim._bess['soc_pct'] = round(rng.uniform(25, 95), 1)

    def inject(_scenario_id):
        for span_id, circuit in trips:
            sim._spans[span_id]['upper_status' if circuit == 'UPPER_PRIMARY' else 'lower_status'] = (
                'FAULT_TRIPPED' if circuit == 'UPPER_PRIMARY' else 'TRIPPED')
        for c in sim._consumers.values():
            c['mw_demand'] = round(c['mw_demand'] * scale, 3)
        sim._substation['status'] = 'OVERLOAD_RISK'
    return trips, scale, inject


@pytest.mark.parametrize('seed', SEEDS)
def test_random_faults_keep_safety_invariants(live_runtime, monkeypatch, seed):  # noqa: F811
    sim, store, _ = live_runtime
    graph.init_graph(reset=True)
    rng = random.Random(seed)
    trips, scale, inject = randomise(sim, rng)
    monkeypatch.setattr(sim, 'inject_fault', inject)
    soc = sim._bess['soc_pct']

    async def run():
        await sim.trigger_scenario(1)
        await sim._active_task
    asyncio.run(run())

    context = f'seed={seed} trips={trips} scale={scale:.3f} soc={soc} status={sim._scenario_status} log={sim._agent_log}'
    assert sim._scenario_status in ('MITIGATED', 'FAILED'), context
    # Tier-1 and Tier-2 loads are never shed, whatever happens.
    for cid in ('HOSP_ST_JUDE', 'RES_VALLEY_SUBURBS', 'COM_METRO_CORE'):
        assert sim._consumers[cid]['energized'], context
    # Battery limits hold.
    assert 0 <= sim._bess['active_injection_mw'] <= sim._bess['max_discharge_mw'], context
    if sim._bess['active_injection_mw'] > 0:
        assert soc > 40, context
    logs = store.query_logs(limit=500)
    if sim._scenario_status == 'MITIGATED':
        assert graph.trace_cascade('SUB_METRO')['projection']['outcome'] == 'WITHIN_LIMITS', context
        assert graph.verify_tier1_protection()['all_protected'], context
    else:
        assert any(e['action'] == 'MITIGATION_FAILED' for e in logs), context
    # Every completed actuation was preceded by a durable intent for the same target.
    for done in (e for e in logs if e['outcome'] == 'SUCCESS' and e['action'].startswith('SCADA_')):
        assert any(e['outcome'] == 'REQUESTED' and e['target'] == done['target'] and e['seq'] < done['seq']
                   for e in logs), context
    assert store.verify_chain()['valid'], context
