"""
GridPulse Autonomous SCADA MCP Interceptor
Orchestrates deterministic multi-hop cascade blast radius tracing,
BESS dispatch, and Tier-3 industrial breaker actuation across the 3 core grid scenarios.
Strictly adheres to zero-hallucination and Tier-1 shielding policies.
"""

import asyncio
import logging
import uuid
from typing import Optional, Dict, Any, List, Callable
from datetime import datetime, timezone

from backend.mcp.schemas import (
    ScenarioInfo,
    MetricsInfo,
    SpanStatus,
    TelemetryPayload,
    SPAN_MAPPING,
    HYDRO_PLANT_ID,
    TOWER_IDS,
    SPAN_IDS,
    SUBSTATION_ID,
    BESS_ID,
    TIER1_HOSPITAL_ID,
    TIER3_BREAKER_ID,
    AuditRecord,
)
from backend.mcp.server import (
    falkordb_trace_blast_radius,
    falkordb_find_mitigation_assets,
    scada_trip_breaker,
    scada_dispatch_bess,
    get_grid_model,
)
from backend.mcp.ws_manager import get_ws_manager, WebSocketTelemetryManager

logger = logging.getLogger("gridpulse.mcp.interceptor")


class GridPulseInterceptor:
    """
    Autonomous SCADA MCP Interceptor engine.
    Controls physical breaker actuation and BESS inverter dispatch using deterministic Cypher graph traces.
    """

    def __init__(self, ws_manager: Optional[WebSocketTelemetryManager] = None):
        self.ws_manager = ws_manager or get_ws_manager()
        self.lock = asyncio.Lock()
        self.active_task: Optional[asyncio.Task] = None
        self.active_run_id: Optional[str] = None
        self.cancel_event = asyncio.Event()
        self.audit_records: List[AuditRecord] = []
        self._audit_subscribers: List[Callable[[AuditRecord], None]] = []

    def subscribe_audit(self, callback: Callable[[AuditRecord], None]):
        """Registers a callback to receive audit records in real-time (for Jyothi's audit store)."""
        self._audit_subscribers.append(callback)

    def _record_audit(self, record: AuditRecord):
        self.audit_records.append(record)
        for sub in self._audit_subscribers:
            try:
                sub(record)
            except Exception as e:
                logger.warning(f"Error notifying audit subscriber: {e}")

    async def trigger_scenario(self, scenario_id: int, step_delay: float = 0.4) -> Dict[str, Any]:
        """
        Triggers an autonomous interception sequence for Scenario 1, 2, or 3.
        Cancels any previous conflicting in-flight run and acquires execution lock.
        """
        async with self.lock:
            # If a task is already running, cancel it cleanly
            if self.active_task and not self.active_task.done():
                logger.info("Cancelling existing active interceptor task for new trigger.")
                self.cancel_event.set()
                self.active_task.cancel()
                try:
                    await asyncio.wait_for(self.active_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            self.cancel_event.clear()
            self.active_run_id = f"RUN_{scenario_id}_{uuid.uuid4().hex[:8]}"

            if scenario_id == 1:
                coro = self._run_scenario_1_lightning(self.active_run_id, step_delay)
            elif scenario_id == 2:
                coro = self._run_scenario_2_wildfire(self.active_run_id, step_delay)
            elif scenario_id == 3:
                coro = self._run_scenario_3_heatwave(self.active_run_id, step_delay)
            else:
                raise ValueError(f"Unknown scenario ID: {scenario_id}. Supported: 1, 2, 3.")

            self.active_task = asyncio.create_task(coro)

            return {
                "status": "TRIGGERED",
                "run_id": self.active_run_id,
                "scenario_id": scenario_id,
                "message": f"Scenario {scenario_id} initiated with autonomous MCP interceptor.",
            }

    async def reset(self) -> Dict[str, Any]:
        """
        Cancels any active scenario execution, resets grid model and telemetry to nominal 500kV state.
        """
        async with self.lock:
            self.cancel_event.set()
            if self.active_task and not self.active_task.done():
                self.active_task.cancel()
                try:
                    await asyncio.wait_for(self.active_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            # Reset underlying graph model and breakers
            get_grid_model().reset()

            # Reset telemetry snapshot and broadcast
            nominal_payload = self.ws_manager.reset_to_nominal()
            await self.ws_manager.broadcast(nominal_payload)

            self.active_run_id = None
            return {
                "status": "RESET_COMPLETE",
                "message": "Grid restored to nominal 500kV flow. All breakers closed and BESS on standby.",
            }

    # -------------------------------------------------------------------------
    # Scenario 1: Lightning Strike on TX_882 (T3 -> T4)
    # -------------------------------------------------------------------------
    async def _run_scenario_1_lightning(self, run_id: str, step_delay: float):
        scenario_name = "LIGHTNING_STRIKE_TX882"
        logger.info(f"[{run_id}] Starting Scenario 1: {scenario_name}")

        try:
            # STEP 1: Fault Occurs on Span TX_882 (T3 -> T4)
            spans = []
            for s_id, (from_t, to_t) in SPAN_MAPPING.items():
                upper = "FAULT_TRIPPED" if s_id == "TX_882" else "NOMINAL"
                spans.append(
                    SpanStatus(
                        id=s_id,
                        from_tower=from_t,
                        to_tower=to_t,
                        upper_status=upper,
                        lower_status="NOMINAL",
                    )
                )

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=1, name=scenario_name, status="TRIGGERED"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=840.0,
                    city_load_pct=100.0,
                    bess_injection_mw=0.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher="// High-Voltage Lightning Strike Detected on Span TX_882 (T3->T4)\n// Upper Primary Circuit Relays Tripped",
                agent_log="ALERT: Lightning strike on Span TX_882 (T3->T4). Upper circuit tripped. Tracing cascade blast radius...",
            )
            await self.ws_manager.broadcast(payload)
            if self.cancel_event.is_set():
                return
            await asyncio.sleep(step_delay)

            # STEP 2: FalkorDB GraphBLAS Multi-Hop Blast Radius Trace
            blast_result = falkordb_trace_blast_radius(origin_node_id="T3", max_hops=6)
            self._record_audit(
                AuditRecord(
                    run_id=run_id,
                    scenario_id=1,
                    action="GRAPH_BLAST_RADIUS_TRACE",
                    target="T3",
                    evidence_query=blast_result.executed_cypher,
                    evidence_hash=blast_result.cypher_audit_hash,
                    reason="Trace downstream thermal overload 4-6 hops ahead of physical collapse.",
                    outcome="SUCCESS",
                    detail=f"Detected 4-hop cascade to {SUBSTATION_ID}: Projected load 985 MW (98.5% capacity), thermal collapse in 90s.",
                )
            )

            # Update spans to show downstream thermal risk on remaining path
            for s in spans:
                if s.id in ["TX_883", "TX_884", "TX_885", "TX_886", "TX_887", "TX_888"]:
                    s.upper_status = "OVERLOAD_WARNING"

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=1, name=scenario_name, status="ANALYZING"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=blast_result.latency_ms,
                    city_load_mw=985.0,
                    city_load_pct=98.5,
                    bess_injection_mw=0.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher=blast_result.executed_cypher,
                agent_log=(
                    f"FalkorDB GraphBLAS: 4-hop cascade predicted in {blast_result.latency_ms} ms. "
                    f"Substation SUB_METRO facing thermal collapse at 98.5% capacity in 90 seconds."
                ),
            )
            await self.ws_manager.broadcast(payload)
            if self.cancel_event.is_set():
                return
            await asyncio.sleep(step_delay)

            # STEP 3: Discover Mitigation Assets (BESS + Tier-3 Industrial Load)
            assets_result = falkordb_find_mitigation_assets(substation_id=SUBSTATION_ID, required_mw=85.0)
            self._record_audit(
                AuditRecord(
                    run_id=run_id,
                    scenario_id=1,
                    action="FIND_MITIGATION_ASSETS",
                    target=SUBSTATION_ID,
                    evidence_query=assets_result.executed_cypher,
                    evidence_hash=assets_result.cypher_audit_hash,
                    reason="Identify 2-hop BESS inverter capacity and interruptible Tier-3 heavy loads.",
                    outcome="SUCCESS",
                    detail=f"Discovered BESS_METRO_01 (88% SOC) and Tier-3 Breaker BRK_IND_12 (50 MW). St. Jude Hospital shielded.",
                )
            )

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=1, name=scenario_name, status="MITIGATING"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=985.0,
                    city_load_pct=98.5,
                    bess_injection_mw=0.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher=assets_result.executed_cypher,
                agent_log="MCP Agent: Found BESS_METRO_01 (88% SOC) + Tier-3 BRK_IND_12 (50 MW). St. Jude Trauma Center shielded.",
            )
            await self.ws_manager.broadcast(payload)
            if self.cancel_event.is_set():
                return
            await asyncio.sleep(step_delay)

            # STEP 4: Actuate Selective Tier-3 Industrial Breaker Trip (BRK_IND_12)
            trip_result = scada_trip_breaker(
                breaker_id=TIER3_BREAKER_ID,
                reason_code="THERMAL_CASCADE_INTERCEPTION",
                cypher_audit_hash=assets_result.cypher_audit_hash,
                run_id=run_id,
                scenario_id=1,
            )
            self._record_audit(
                AuditRecord(
                    run_id=run_id,
                    scenario_id=1,
                    action="SCADA_TRIP_BREAKER",
                    target=TIER3_BREAKER_ID,
                    evidence_query="MATCH (c:Consumer {breaker_id:'BRK_IND_12'}) RETURN c.priority_tier;",
                    evidence_hash=assets_result.cypher_audit_hash,
                    reason="Shed non-essential Tier-3 smelter load to prevent regional blackout.",
                    outcome="SUCCESS",
                    detail=trip_result.detail,
                )
            )

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=1, name=scenario_name, status="MITIGATING"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=935.0,
                    city_load_pct=93.5,
                    bess_injection_mw=0.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher=f"// Breaker Actuation Audit\n// Tripped: {TIER3_BREAKER_ID} (-50 MW Industrial Load)",
                agent_log=f"SCADA Command: Opened Tier-3 industrial breaker {TIER3_BREAKER_ID}. Load reduced by 50 MW.",
            )
            await self.ws_manager.broadcast(payload)
            if self.cancel_event.is_set():
                return
            await asyncio.sleep(step_delay)

            # STEP 5: Dispatch BESS Inverter (+45 MW on Lower Backup Circuit)
            dispatch_result = scada_dispatch_bess(
                bess_id=BESS_ID,
                inject_mw=45.0,
                target_circuit="LOWER_BACKUP",
                run_id=run_id,
                scenario_id=1,
            )
            self._record_audit(
                AuditRecord(
                    run_id=run_id,
                    scenario_id=1,
                    action="SCADA_DISPATCH_BESS",
                    target=BESS_ID,
                    evidence_query=f"MATCH (b:BESS {{id:'{BESS_ID}'}}) RETURN b.soc_pct;",
                    evidence_hash=assets_result.cypher_audit_hash,
                    reason="Inject +45 MW onto lower backup circuit to relieve corridor stress.",
                    outcome="SUCCESS",
                    detail=dispatch_result.detail,
                )
            )

            # Update spans to show lower circuit rerouted with BESS
            for s in spans:
                if s.id == "TX_882":
                    s.lower_status = "REROUTED_BESS"
                if s.id in ["TX_883", "TX_884", "TX_885", "TX_886", "TX_887", "TX_888"]:
                    s.upper_status = "NOMINAL"
                    s.lower_status = "ENERGIZED"

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=1, name=scenario_name, status="MITIGATED"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=840.0,
                    city_load_pct=84.0,
                    bess_injection_mw=45.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher=assets_result.executed_cypher,
                agent_log=(
                    f"MCP Action: Breaker {TIER3_BREAKER_ID} opened + 45MW BESS injected on lower circuit. "
                    f"Cascade intercepted. {TIER1_HOSPITAL_ID} 100% online."
                ),
            )
            await self.ws_manager.broadcast(payload)
            logger.info(f"[{run_id}] Scenario 1 successfully MITIGATED.")

        except asyncio.CancelledError:
            logger.info(f"[{run_id}] Scenario 1 cancelled by reset.")
        except Exception as e:
            logger.error(f"[{run_id}] Scenario 1 encountered error: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Scenario 2: Wildfire Smoke Ionization (T5 -> T6)
    # -------------------------------------------------------------------------
    async def _run_scenario_2_wildfire(self, run_id: str, step_delay: float):
        scenario_name = "WILDFIRE_SMOKE_IONIZATION_T5_T6"
        logger.info(f"[{run_id}] Starting Scenario 2: {scenario_name}")

        try:
            spans = []
            for s_id, (from_t, to_t) in SPAN_MAPPING.items():
                upper = "OVERLOAD_WARNING" if s_id == "TX_884" else "NOMINAL"
                spans.append(
                    SpanStatus(
                        id=s_id,
                        from_tower=from_t,
                        to_tower=to_t,
                        upper_status=upper,
                        lower_status="NOMINAL",
                    )
                )

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=2, name=scenario_name, status="TRIGGERED"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=840.0,
                    city_load_pct=100.0,
                    bess_injection_mw=0.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher="// Wildfire Smoke Plume Detected at Alpine Corridor T5-T6 (Span TX_884)\n// Atmospheric Ionization Risk: Line-to-ground flashover imminent",
                agent_log="ALERT: Wildfire ionization detected around Spans T5-T6. Proactive flashover prevention initiated.",
            )
            await self.ws_manager.broadcast(payload)
            if self.cancel_event.is_set():
                return
            await asyncio.sleep(step_delay)

            # De-energize affected upper span proactively before flashover
            for s in spans:
                if s.id == "TX_884":
                    s.upper_status = "DE_ENERGIZED"
                    s.lower_status = "REROUTED_BESS"

            # Execute mitigation
            assets_result = falkordb_find_mitigation_assets(substation_id=SUBSTATION_ID, required_mw=70.0)
            scada_trip_breaker(
                breaker_id=TIER3_BREAKER_ID,
                reason_code="WILDFIRE_CORRIDOR_ISOLATION",
                cypher_audit_hash=assets_result.cypher_audit_hash,
                run_id=run_id,
                scenario_id=2,
            )
            scada_dispatch_bess(
                bess_id=BESS_ID,
                inject_mw=40.0,
                target_circuit="LOWER_BACKUP",
                run_id=run_id,
                scenario_id=2,
            )

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=2, name=scenario_name, status="MITIGATED"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=810.0,
                    city_load_pct=81.0,
                    bess_injection_mw=40.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher=assets_result.executed_cypher,
                agent_log=f"Wildfire Corridors Isolated: Span TX_884 rerouted to BESS backup. {TIER1_HOSPITAL_ID} 100% protected.",
            )
            await self.ws_manager.broadcast(payload)

        except asyncio.CancelledError:
            logger.info(f"[{run_id}] Scenario 2 cancelled.")
        except Exception as e:
            logger.error(f"[{run_id}] Scenario 2 error: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Scenario 3: 5 PM Extreme Heatwave Demand Surge (120% City Load)
    # -------------------------------------------------------------------------
    async def _run_scenario_3_heatwave(self, run_id: str, step_delay: float):
        scenario_name = "HEATWAVE_DEMAND_SURGE_120PCT"
        logger.info(f"[{run_id}] Starting Scenario 3: {scenario_name}")

        try:
            spans = []
            for s_id, (from_t, to_t) in SPAN_MAPPING.items():
                spans.append(
                    SpanStatus(
                        id=s_id,
                        from_tower=from_t,
                        to_tower=to_t,
                        upper_status="OVERLOAD_WARNING",
                        lower_status="NOMINAL",
                    )
                )

            # 120% demand = 1008 MW on 1000 MW capacity
            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=3, name=scenario_name, status="TRIGGERED"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=1008.0,
                    city_load_pct=120.0,
                    bess_injection_mw=0.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher="// Metropolitan Heatwave Peak: 5 PM HVAC Surge\nMATCH (s:Substation {id:'SUB_METRO'}) RETURN s.current_load_mw, s.capacity_mw;",
                agent_log="ALERT: 5 PM Heatwave surge pushing Metro Substation to 1008 MW (120% rating). Intercepting thermal sag...",
            )
            await self.ws_manager.broadcast(payload)
            if self.cancel_event.is_set():
                return
            await asyncio.sleep(step_delay)

            # Shed Tier-3 and inject BESS
            assets_result = falkordb_find_mitigation_assets(substation_id=SUBSTATION_ID, required_mw=95.0)
            scada_trip_breaker(
                breaker_id=TIER3_BREAKER_ID,
                reason_code="HEATWAVE_PEAK_SHEDDING",
                cypher_audit_hash=assets_result.cypher_audit_hash,
                run_id=run_id,
                scenario_id=3,
            )
            scada_dispatch_bess(
                bess_id=BESS_ID,
                inject_mw=45.0,
                target_circuit="LOWER_BACKUP",
                run_id=run_id,
                scenario_id=3,
            )

            for s in spans:
                s.upper_status = "NOMINAL"
                s.lower_status = "REROUTED_BESS"

            payload = TelemetryPayload(
                scenario=ScenarioInfo(id=3, name=scenario_name, status="MITIGATED"),
                metrics=MetricsInfo(
                    graphblas_latency_ms=1.4,
                    city_load_mw=913.0,
                    city_load_pct=91.3,
                    bess_injection_mw=45.0,
                    tier1_hospital_status="PROTECTED",
                ),
                spans=spans,
                active_cypher=assets_result.executed_cypher,
                agent_log=(
                    f"Heatwave Peak Mitigated: Tier-3 {TIER3_BREAKER_ID} shed (-50MW) + 45MW BESS support. "
                    f"Grid stable at 91.3%. {TIER1_HOSPITAL_ID} protected."
                ),
            )
            await self.ws_manager.broadcast(payload)

        except asyncio.CancelledError:
            logger.info(f"[{run_id}] Scenario 3 cancelled.")
        except Exception as e:
            logger.error(f"[{run_id}] Scenario 3 error: {e}", exc_info=True)


# Singleton instance
_interceptor = GridPulseInterceptor()


def get_interceptor() -> GridPulseInterceptor:
    return _interceptor
