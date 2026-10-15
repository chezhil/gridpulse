"""
GridPulse SCADA MCP Test Suite
Tests deterministic SCADA tools, safety guardrails, BESS constraints,
Cypher audit hash validation, multi-scenario mitigation, and WebSocket telemetry resilience.
"""

import asyncio
import pytest
import json
from unittest.mock import AsyncMock, MagicMock

from backend.mcp.schemas import (
    HYDRO_PLANT_ID,
    TOWER_IDS,
    SPAN_IDS,
    SUBSTATION_ID,
    BESS_ID,
    TIER1_HOSPITAL_ID,
    TIER3_BREAKER_ID,
    SPAN_MAPPING,
    TelemetryPayload,
)
from backend.mcp.server import (
    falkordb_trace_blast_radius,
    falkordb_find_mitigation_assets,
    scada_trip_breaker,
    scada_dispatch_bess,
    generate_cypher_audit_hash,
    get_grid_model,
)
from backend.mcp.interceptor import GridPulseInterceptor
from backend.mcp.ws_manager import WebSocketTelemetryManager


def setup_function():
    """Resets grid model before each test."""
    get_grid_model().reset()


def test_canonical_ids_conformity():
    """Ensures all canonical IDs match SPEC.md and GITHUB_RULES.md exactly."""
    assert HYDRO_PLANT_ID == "HYDRO_ALPINE_01"
    assert TOWER_IDS == ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]
    assert SPAN_IDS == ["TX_880", "TX_881", "TX_882", "TX_883", "TX_884", "TX_885", "TX_886", "TX_887", "TX_888"]
    assert SUBSTATION_ID == "SUB_METRO"
    assert BESS_ID == "BESS_METRO_01"
    assert TIER1_HOSPITAL_ID == "HOSP_ST_JUDE"
    assert TIER3_BREAKER_ID == "BRK_IND_12"
    assert SPAN_MAPPING["TX_882"] == ("T3", "T4")


def test_tier1_critical_asset_protection_guardrail():
    """MANDATORY: Tripping a breaker attached to Tier-1 Hosp St. Jude MUST be rejected."""
    model = get_grid_model()
    valid_hash = generate_cypher_audit_hash("QUERY", {}, {})

    # Attempt to trip hospital breaker
    result = scada_trip_breaker(
        breaker_id="BRK_HOSP_01",
        reason_code="TEST_SHED",
        cypher_audit_hash=valid_hash,
    )

    assert result.status == "REJECTED_PROTECTED_ASSET"
    assert result.shed_mw == 0.0
    assert "CRITICAL ASSET SHIELD" in result.detail
    assert model.breakers["BRK_HOSP_01"]["state"] == "CLOSED"

    # Verify audit record was created capturing the rejected attempt
    last_record = model.audit_log[-1]
    assert last_record.outcome == "REJECTED"
    assert last_record.target == "BRK_HOSP_01"


def test_bess_dispatch_guardrails_soc_and_capacity():
    """Tests SoC > 40% and max discharge <= 60 MW guardrails."""
    model = get_grid_model()

    # Valid dispatch
    valid_res = scada_dispatch_bess(BESS_ID, inject_mw=45.0, target_circuit="LOWER_BACKUP")
    assert valid_res.status == "DISCHARGING"
    assert valid_res.injected_mw == 45.0

    # Over capacity (> 60 MW)
    over_res = scada_dispatch_bess(BESS_ID, inject_mw=75.0, target_circuit="LOWER_BACKUP")
    assert over_res.status == "REJECTED_OVER_CAPACITY"

    # Negative or 0 MW
    zero_res = scada_dispatch_bess(BESS_ID, inject_mw=0.0, target_circuit="LOWER_BACKUP")
    assert zero_res.status == "REJECTED_OVER_CAPACITY"

    # Low SoC threshold (<= 40%)
    model.bess_soc_pct = 35.0
    low_soc_res = scada_dispatch_bess(BESS_ID, inject_mw=20.0, target_circuit="LOWER_BACKUP")
    assert low_soc_res.status == "REJECTED_LOW_SOC"


def test_cypher_audit_hash_binding():
    """Tests deterministic Cypher audit hashing and tamper detection."""
    q = "MATCH (t:Tower {id:'T3'})-[:SPAN*1..6]->(sub:Substation) RETURN sub;"
    p = {"origin": "T3"}
    res = [{"sub": "SUB_METRO", "load": 985.0}]

    h1 = generate_cypher_audit_hash(q, p, res)
    h2 = generate_cypher_audit_hash(q, p, res)
    assert h1 == h2  # Deterministic

    # Tampering with results changes hash
    tampered_res = [{"sub": "SUB_METRO", "load": 400.0}]
    h_tampered = generate_cypher_audit_hash(q, p, tampered_res)
    assert h1 != h_tampered

    # Breaker trip rejects missing/invalid hash
    bad_res = scada_trip_breaker(TIER3_BREAKER_ID, "TEST", cypher_audit_hash="")
    assert bad_res.status == "FAILED"


def test_falkordb_blast_radius_and_mitigation_discovery():
    """Tests multi-hop blast radius trace and 2-hop BESS discovery."""
    blast = falkordb_trace_blast_radius(origin_node_id="T3", max_hops=6)
    assert len(blast.overloaded_substations) == 1
    assert blast.overloaded_substations[0].substation_id == SUBSTATION_ID
    assert blast.overloaded_substations[0].hops_from_fault == 4
    assert blast.overloaded_substations[0].thermal_collapse_seconds == 90
    assert blast.latency_ms > 0

    mitigation = falkordb_find_mitigation_assets(SUBSTATION_ID, required_mw=85.0)
    assert len(mitigation.available_bess) >= 1
    assert mitigation.available_bess[0].id == BESS_ID
    assert len(mitigation.interruptible_loads) >= 1
    assert mitigation.interruptible_loads[0].breaker_id == TIER3_BREAKER_ID
    assert TIER1_HOSPITAL_ID in mitigation.shielded_tier1_assets


@pytest.mark.asyncio
async def test_scenario_1_lightning_end_to_end():
    """Tests complete Scenario 1 lightning mitigation flow with telemetry updates."""
    ws = WebSocketTelemetryManager()
    interceptor = GridPulseInterceptor(ws_manager=ws)

    # Trigger with rapid step delay for testing
    await interceptor.trigger_scenario(scenario_id=1, step_delay=0.01)

    # Wait for completion
    if interceptor.active_task:
        await interceptor.active_task

    payload = ws.get_current_payload()
    assert payload.scenario.status == "MITIGATED"
    assert payload.scenario.id == 1
    assert payload.metrics.tier1_hospital_status == "PROTECTED"
    assert payload.metrics.bess_injection_mw == 45.0
    assert payload.metrics.city_load_pct == 84.0

    # Verify span states
    tx_882 = next(s for s in payload.spans if s.id == "TX_882")
    assert tx_882.upper_status == "FAULT_TRIPPED"
    assert tx_882.lower_status == "REROUTED_BESS"

    # Verify Tier-3 breaker is tripped and hospital is intact
    model = get_grid_model()
    assert model.breakers[TIER3_BREAKER_ID]["state"] == "OPEN"
    assert model.breakers["BRK_HOSP_01"]["state"] == "CLOSED"

    # Verify audit records were generated
    assert len(interceptor.audit_records) >= 4


@pytest.mark.asyncio
async def test_scenario_2_and_3():
    """Tests Wildfire (Scenario 2) and Heatwave (Scenario 3) mitigation pipelines."""
    ws = WebSocketTelemetryManager()
    interceptor = GridPulseInterceptor(ws_manager=ws)

    # Scenario 2: Wildfire
    await interceptor.trigger_scenario(scenario_id=2, step_delay=0.01)
    if interceptor.active_task:
        await interceptor.active_task
    assert ws.get_current_payload().scenario.status == "MITIGATED"

    # Reset
    await interceptor.reset()
    assert ws.get_current_payload().scenario.status == "NOMINAL"

    # Scenario 3: Heatwave
    await interceptor.trigger_scenario(scenario_id=3, step_delay=0.01)
    if interceptor.active_task:
        await interceptor.active_task
    assert ws.get_current_payload().scenario.status == "MITIGATED"
    assert ws.get_current_payload().metrics.bess_injection_mw == 45.0


@pytest.mark.asyncio
async def test_reset_cancels_in_flight_task():
    """Tests that reset() cancels a running scenario cleanly and restores nominal state."""
    ws = WebSocketTelemetryManager()
    interceptor = GridPulseInterceptor(ws_manager=ws)

    # Start with long step delay
    await interceptor.trigger_scenario(scenario_id=1, step_delay=5.0)
    assert interceptor.active_task is not None
    assert not interceptor.active_task.done()

    # Reset immediately
    reset_res = await interceptor.reset()
    assert reset_res["status"] == "RESET_COMPLETE"
    assert interceptor.active_task.cancelled() or interceptor.active_task.done()

    payload = ws.get_current_payload()
    assert payload.scenario.status == "NOMINAL"
    assert payload.metrics.bess_injection_mw == 0.0
    assert get_grid_model().breakers[TIER3_BREAKER_ID]["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_websocket_manager_disconnect_resilience():
    """Tests that disconnected WebSocket clients do not crash the broadcaster."""
    ws = WebSocketTelemetryManager()

    mock_client_1 = AsyncMock()
    mock_client_1.send_text = AsyncMock()

    mock_client_dead = AsyncMock()
    # Succeeds on initial connect snapshot, fails on subsequent broadcast
    mock_client_dead.send_text = AsyncMock(side_effect=[None, RuntimeError("Client closed socket")])

    await ws.connect(mock_client_1)
    await ws.connect(mock_client_dead)

    assert len(ws.active_connections) == 2

    # Broadcast should succeed and silently evict dead client
    await ws.broadcast()

    assert mock_client_1 in ws.active_connections
    assert mock_client_dead not in ws.active_connections
    assert len(ws.active_connections) == 1

