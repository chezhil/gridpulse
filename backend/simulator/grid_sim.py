"""Single simulator state; graph topology and dc-pf/1 model are canonical.

This is a deterministic educational simulator, not a validated power-flow or
thermal solver. Scenario time is for UI presentation; query times are measured.
"""
import asyncio
import copy
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field
from backend.graph import topology
from backend.audit.logger import audit_logger


class BreakerActionRequest(BaseModel):
    breaker_id: str
    reason: str
    cypher_audit_hash: str


class BessDispatchRequest(BaseModel):
    bess_id: str
    inject_mw: float = Field(..., gt=0, le=60, allow_inf_nan=False)
    target_circuit: str = 'LOWER_BACKUP'


class GridSimulator:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._active_task = None
        self._run_id = uuid.uuid4().hex
        self._scenario_id = 0
        self._scenario_name = 'STEADY_STATE_NOMINAL'
        self._scenario_status = 'NOMINAL'
        self._active_cypher = ''
        self._agent_log = 'Educational simulator ready. Awaiting graph readiness.'
        self._graphblas_latency_ms = None
        self._evidence = []
        self._mode = 'deterministic'
        self._agent_steps = []
        self._memory = None
        self._init_grid_data()

    def _init_grid_data(self):
        self._hydro = copy.deepcopy(topology.HYDRO[0])
        self._towers = {t['id']: copy.deepcopy(t) for t in topology.TOWERS}
        self._substation = copy.deepcopy(topology.SUBSTATIONS[0])
        self._bess = copy.deepcopy(topology.BESS_UNITS[0])
        self._consumers = {c['id']: copy.deepcopy(c) for c in topology.CONSUMERS}
        self._circuit_flows = {(r['id'], r['circuit']): r['props']['flow_mw'] for r in topology.span_rows()}
        self._spans = {s['id']: {**s, 'upper_status': 'NOMINAL', 'lower_status': 'NOMINAL',
                        'flow_mw': 840.0, 'sag_m': topology.NOMINAL_SAG_M}
                       for s in topology.SPAN_SEGMENTS}

    def graph_update(self):
        """Only graph-writable properties, preserving canonical IDs and enums."""
        demand = sum(c['mw_demand'] for c in self._consumers.values() if c['energized'])
        self._substation['current_load_mw'] = max(0, demand - self._bess['active_injection_mw'])
        circuits = []
        for s in self._spans.values():
            for circuit, field in [('UPPER_PRIMARY', 'upper_status'), ('LOWER_BACKUP', 'lower_status')]:
                circuits.append({'span_id': s['id'], 'circuit': circuit,
                    'status': 'TRIPPED' if s[field] in ('FAULT_TRIPPED', 'DE_ENERGIZED', 'TRIPPED') else 'ENERGIZED',
                    'flow_mw': 0.0 if s[field] in ('FAULT_TRIPPED', 'DE_ENERGIZED', 'TRIPPED') else self._circuit_flows[(s['id'], circuit)]})
        return {'source': self._run_id, 'spans': circuits,
            'towers': [{k: t[k] for k in ('id', 'status', 'insulator_temp_c')} for t in self._towers.values()],
            'substations': [{k: self._substation[k] for k in ('id', 'current_load_mw', 'status', 'transformer_temp_c')}],
            'bess': [{k: self._bess[k] for k in ('id', 'soc_pct', 'status', 'active_injection_mw')}],
            'consumers': [{k: c[k] for k in ('id', 'energized', 'mw_demand')} for c in self._consumers.values()]}

    def apply_projection(self, projection):
        for row in projection['circuits']:
            self._circuit_flows[(row['span_id'], row['circuit'])] = row['flow_mw']
        for span in self._spans.values():
            span['flow_mw'] = sum(self._circuit_flows[(span['id'], c)] for c in topology.CIRCUITS)
        for sub in projection['substations']:
            if sub['id'] == self._substation['id']:
                self._substation['status'] = sub['projected_status']
                self._substation['current_load_mw'] = sub['net_demand_mw']

    def get_snapshot(self):
        demand = sum(c['mw_demand'] for c in self._consumers.values() if c['energized'])
        net = max(0, demand - self._bess['active_injection_mw'])
        hospital = 'PROTECTED' if self._consumers['HOSP_ST_JUDE']['energized'] else 'CRITICAL_OFFLINE'
        if self._substation['status'] != 'ONLINE' and hospital == 'PROTECTED':
            hospital = 'AT_RISK'
        return copy.deepcopy({'timestamp': datetime.now(timezone.utc).isoformat(), 'run_id': self._run_id,
            'scenario': {'id': self._scenario_id, 'name': self._scenario_name, 'status': self._scenario_status},
            'metrics': {'graphblas_latency_ms': self._graphblas_latency_ms,
                'city_load_mw': round(demand, 3), 'city_load_pct': round(demand / 840 * 100, 2),
                'substation_load_mw': round(net, 3), 'bess_injection_mw': self._bess['active_injection_mw'],
                'tier1_hospital_status': hospital},
            'spans': list(self._spans.values()), 'towers': list(self._towers.values()),
            'substation': self._substation, 'bess': self._bess, 'consumers': list(self._consumers.values()),
            'active_cypher': self._active_cypher, 'agent_log': self._agent_log, 'evidence': self._evidence,
            'mode': self._mode, 'agent_steps': self._agent_steps[-40:], 'memory': self._memory,
            'simulation_model': 'dc-pf/1'})

    def record(self, action, target, reason, outcome='SUCCESS', evidence=None, requested_action=None,
               operator_id='SCADA_MCP_AGENT'):
        return audit_logger.log_event(event_id=uuid.uuid4().hex, run_id=self._run_id,
            scenario_id=self._scenario_id, target=target, action=action, reason=reason,
            outcome=outcome, evidence=evidence, requested_action=requested_action, operator_id=operator_id,
            state_version=self._graph_state_version())

    @staticmethod
    def _graph_state_version():
        # The graph state an action was based on; None when the graph is unreachable.
        from backend import graph
        try:
            return graph.read_state_version()
        except graph.GraphError:
            return None

    async def _cancel(self):
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        self._active_task = None

    async def reset(self, operator_id='OPERATOR_RESET'):
        from backend import graph
        async with self._lock:
            await self._cancel()
            self._init_grid_data()
            self._run_id = uuid.uuid4().hex
            self._scenario_id = 0
            self._scenario_name = 'STEADY_STATE_NOMINAL'
            self._scenario_status = 'NOMINAL'
            self._graphblas_latency_ms = None
            self._active_cypher = ''
            self._evidence = []
            self._agent_steps = []
            self._memory = None
            self._mode = 'deterministic'
            try:
                await asyncio.to_thread(graph.reset_grid)
                self._agent_log = 'Simulator and FalkorDB reset to nominal. Audit history preserved.'
                await asyncio.to_thread(self.record, 'GRID_RESET', 'GRID_TOPOLOGY', self._agent_log, operator_id=operator_id)
            except Exception as exc:
                self._scenario_status = 'FAILED'
                self._agent_log = f'Reset incomplete: {exc}'
                await asyncio.to_thread(self.record, 'GRID_RESET', 'GRID_TOPOLOGY', str(exc), 'FAILED', operator_id=operator_id)
                raise
            return self.get_snapshot()

    async def trigger_scenario(self, scenario_id, operator_id='SCADA_OPERATOR', mode='deterministic'):
        from backend import graph
        from backend.mcp.interceptor import get_interceptor
        if scenario_id not in (1, 2, 3):
            raise ValueError('Scenario must be 1, 2, or 3')
        if mode not in ('deterministic', 'agent'):
            raise ValueError("mode must be 'deterministic' or 'agent'")
        async with self._lock:
            if self._active_task and not self._active_task.done():
                if self._scenario_id == scenario_id:
                    return self.get_snapshot()
                raise ValueError('Another scenario is running; reset before starting a different scenario')
            if self._scenario_id:
                raise ValueError('Reset the grid before starting another scenario')
            # Fail before injecting a fault if the graph cannot be reached.
            # Blocking graph I/O runs off the event loop; the lock keeps state consistent.
            await asyncio.to_thread(graph.init_graph)
            await asyncio.to_thread(graph.apply_state_update, self.graph_update())
            self._run_id = uuid.uuid4().hex
            self._scenario_id = scenario_id
            self._scenario_name = {1: 'LIGHTNING_STRIKE_TX882', 2: 'WILDFIRE_IONIZATION_T5_T6',
                                   3: 'HEATWAVE_PEAK_SURGE_120PCT'}[scenario_id]
            await asyncio.to_thread(self.record, 'SCENARIO_TRIGGER', f'SCENARIO_{scenario_id}', self._scenario_name,
                        operator_id=operator_id, requested_action={'scenario_id': scenario_id})
            self._scenario_status = 'FAULT_DETECTED'
            self._mode = mode
            self._agent_steps = []
            self._memory = None
            self._active_task = asyncio.create_task(get_interceptor().run(self._run_id, scenario_id, mode, operator_id))
            return self.get_snapshot()

    def inject_fault(self, scenario_id):
        if scenario_id in (1, 2):
            span = 'TX_882' if scenario_id == 1 else 'TX_884'
            self._spans[span]['upper_status'] = 'FAULT_TRIPPED'
            for tower in (('T3', 'T4') if scenario_id == 1 else ('T5', 'T6')):
                self._towers[tower]['status'] = 'FAULT'
                if scenario_id == 2:
                    self._towers[tower]['insulator_temp_c'] = 94.0
        else:
            for c in self._consumers.values():
                c['mw_demand'] *= 1.2
        self._substation['status'] = 'OVERLOAD_RISK'
        self._agent_log = f'{self._scenario_name}: tracing current graph state.'

    def apply_breaker_trip(self, breaker_id, reason, cypher_audit_hash, operator_id='SCADA_MCP_AGENT'):
        consumers = [c for c in self._consumers.values() if c['breaker_id'] == breaker_id]
        if not consumers:
            raise ValueError(f'Unknown breaker {breaker_id}')
        if any(c['priority_tier'] != 3 for c in consumers):
            raise PermissionError('Only isolated Tier-3 breakers may be opened')
        shed = sum(c['mw_demand'] for c in consumers if c['energized'])
        for c in consumers:
            c['energized'] = False
        return {'status': 'OPEN', 'breaker_id': breaker_id, 'shed_mw': shed}

    def apply_bess_dispatch(self, bess_id, inject_mw, target_circuit='LOWER_BACKUP', operator_id='SCADA_MCP_AGENT'):
        if bess_id != self._bess['id']:
            raise ValueError('Unknown BESS')
        if (not math.isfinite(inject_mw) or not 0 < inject_mw <= self._bess['max_discharge_mw']
                or self._bess['soc_pct'] <= 40 or target_circuit != 'LOWER_BACKUP'):
            raise ValueError('Invalid dispatch: check SOC, positive capacity bounds and LOWER_BACKUP target')
        self._bess['active_injection_mw'] = inject_mw
        self._bess['status'] = 'DISCHARGING'
        return {'status': 'DISCHARGING', 'bess_id': bess_id, 'injected_mw': inject_mw}


grid_simulator = GridSimulator()
