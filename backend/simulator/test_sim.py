"""
Comprehensive Unit & Integration Test Suite for GridPulse Simulator, Auth, and Audit.
Covers:
- Demo login and authorization enforcement on state-changing routes
- Denial of unauthorized mutations
- Critical Tier-1 asset shielding (Hospital invariant)
- Lightning strike cascade interception (Scenario 1)
- Wildfire smoke ionization (Scenario 2)
- Extreme heatwave peak surge (Scenario 3)
- Reset cancellation of active scenario
- Persistent append-only audit trail and evidence hashing
"""

import os
import sys
import pytest
import asyncio
from pathlib import Path
from fastapi.testclient import TestClient

# Ensure repo root is on python path
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.main import app
from backend.simulator.grid_sim import grid_simulator
from backend.audit.logger import audit_logger, AuditLogger
from backend.auth.router import SESSIONS, DEFAULT_DEMO_TOKEN

client = TestClient(app)


def test_app_health():
    """Verify application startup, root and health endpoints."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "HEALTHY"
    assert "run_id" in data


def test_auth_demo_login():
    """Verify demo login and operator bypass functionality."""
    res = client.post("/api/auth/login", json={
        "username": "demo_operator",
        "role": "NERC_OPERATOR",
        "is_demo_bypass": True
    })
    assert res.status_code == 200
    data = res.json()
    assert data["is_demo"] is True
    assert "access_token" in data
    assert data["role"] == "NERC_OPERATOR"
    assert "NERC-CIP" in data["session_badge"]


def test_auth_unauthorized_mutation_denied():
    """Verify state-changing routes reject unauthorized requests."""
    # Without token
    res = client.post("/api/sim/trigger-scenario/1")
    assert res.status_code == 401

    # With invalid token
    res = client.post(
        "/api/sim/trigger-scenario/1",
        headers={"Authorization": "Bearer invalid-dummy-token"}
    )
    assert res.status_code == 401


def test_tier1_hospital_invariant_protection():
    """Verify invariant: Tier-1 hospital breaker CANNOT be tripped under any circumstances."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    # Attempt to trip Hospital breaker BRK_HOSP_01
    res = client.post(
        "/api/sim/actuate/trip-breaker",
        headers=headers,
        json={
            "breaker_id": "BRK_HOSP_01",
            "reason": "Test unauthorized trip attempt",
            "cypher_audit_hash": "dummy_hash_001"
        }
    )
    assert res.status_code == 403
    assert "CRITICAL INVARIANT VIOLATION" in res.json()["detail"]

    # Verify hospital remains online
    state = grid_simulator.get_snapshot()
    assert state["metrics"]["tier1_hospital_status"] == "PROTECTED"
    assert state["consumers"][0]["id"] == "HOSP_ST_JUDE"
    assert state["consumers"][0]["energized"] is True


def test_tier3_industrial_breaker_trip():
    """Verify Tier-3 industrial breaker can be opened for load shedding."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post(
        "/api/sim/actuate/trip-breaker",
        headers=headers,
        json={
            "breaker_id": "BRK_IND_12",
            "reason": "Shed 48MW industrial load",
            "cypher_audit_hash": "dummy_hash_002"
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCESS"
    assert data["breaker_id"] == "BRK_IND_12"
    assert data["shed_mw"] == 48.0


def test_bess_dispatch_bounds():
    """Verify BESS dispatch limits and error handling."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    # Within bounds (45MW)
    res = client.post(
        "/api/sim/actuate/dispatch-bess",
        headers=headers,
        json={
            "bess_id": "BESS_METRO_01",
            "inject_mw": 45.0,
            "target_circuit": "LOWER_BACKUP"
        }
    )
    assert res.status_code == 200
    assert res.json()["status"] == "SUCCESS"
    assert res.json()["injected_mw"] == 45.0

    # Exceeding capacity (>60MW)
    res = client.post(
        "/api/sim/actuate/dispatch-bess",
        headers=headers,
        json={
            "bess_id": "BESS_METRO_01",
            "inject_mw": 95.0,
            "target_circuit": "LOWER_BACKUP"
        }
    )
    assert res.status_code == 422 or res.status_code == 400


from httpx import AsyncClient, ASGITransport

@pytest.mark.asyncio
async def test_scenario_1_lightning_run():
    """Verify end-to-end execution of Scenario 1 (Lightning on TX-882)."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Reset first
        res = await ac.post("/api/sim/reset", headers=headers)
        assert res.status_code == 200

        # Trigger Scenario 1
        res = await ac.post("/api/sim/trigger-scenario/1", headers=headers)
        assert res.status_code == 200

        # Wait for scenario progression (lightning -> fault -> intercept -> mitigate)
        await asyncio.sleep(2.5)

        state = grid_simulator.get_snapshot()
        assert state["scenario"]["id"] == 1
        assert state["scenario"]["status"] == "MITIGATED"
        # Upper circuit tripped, lower circuit rerouted with BESS
        tx882 = next(s for s in state["spans"] if s["id"] == "TX_882")
        assert tx882["upper_status"] == "FAULT_TRIPPED"
        assert tx882["lower_status"] == "REROUTED_BESS"
        # Hospital is 100% online
        assert state["metrics"]["tier1_hospital_status"] == "PROTECTED"
        assert state["bess"]["active_injection_mw"] == 45.0


@pytest.mark.asyncio
async def test_reset_during_run():
    """Verify that reset cleanly cancels active tasks and restores nominal state."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Start Scenario 2
        res = await ac.post("/api/sim/trigger-scenario/2", headers=headers)
        assert res.status_code == 200

        # Immediately reset
        res = await ac.post("/api/sim/reset", headers=headers)
        assert res.status_code == 200
        state = res.json()
        assert state["scenario"]["status"] == "NOMINAL"
        assert state["scenario"]["id"] == 0

        # Wait to ensure no stale task mutated the state
        await asyncio.sleep(1.5)
        state_after = grid_simulator.get_snapshot()
        assert state_after["scenario"]["status"] == "NOMINAL"
        assert state_after["bess"]["status"] == "STANDBY"
        assert state_after["bess"]["active_injection_mw"] == 0.0


@pytest.mark.asyncio
async def test_scenario_3_heatwave():
    """Verify Scenario 3 heatwave surge and mitigation."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Trigger Scenario 3
        res = await ac.post("/api/sim/trigger-scenario/3", headers=headers)
        assert res.status_code == 200

        await asyncio.sleep(2.0)

        state = grid_simulator.get_snapshot()
        assert state["scenario"]["id"] == 3
        assert state["scenario"]["status"] == "MITIGATED"
        assert state["metrics"]["tier1_hospital_status"] == "PROTECTED"


@pytest.mark.asyncio
async def test_scenario_2_wildfire():
    """Verify Scenario 2 wildfire smoke ionization and proactive de-energization."""
    token = DEFAULT_DEMO_TOKEN
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/sim/trigger-scenario/2", headers=headers)
        assert res.status_code == 200

        await asyncio.sleep(2.0)

        state = grid_simulator.get_snapshot()
        assert state["scenario"]["id"] == 2
        assert state["scenario"]["status"] == "MITIGATED"
        assert state["metrics"]["tier1_hospital_status"] == "PROTECTED"
        tx884 = next(s for s in state["spans"] if s["id"] == "TX_884")
        assert tx884["upper_status"] == "PROACTIVELY_DEENERGIZED"
        assert tx884["lower_status"] == "REROUTED_BESS"


def test_websocket_telemetry_stream():
    """Verify WebSocket /ws/telemetry connects and emits canonical payload."""
    import json
    with client.websocket_connect("/ws/telemetry") as ws:
        data = ws.receive_text()
        payload = json.loads(data)
        assert "timestamp" in payload
        assert "scenario" in payload
        assert "metrics" in payload
        assert "spans" in payload
        assert "active_cypher" in payload
        assert "agent_log" in payload
        assert payload["metrics"]["tier1_hospital_status"] == "PROTECTED"


def test_audit_logs_persistence():
    """Verify audit records are stored and retrievable via API."""
    res = client.get("/api/audit/logs?limit=10")
    assert res.status_code == 200
    logs = res.json()
    assert len(logs) > 0
    first_log = logs[0]
    assert "evidence_hash" in first_log
    assert "timestamp" in first_log
    assert "outcome" in first_log

    # Test database persistence across separate logger instance
    temp_logger = AuditLogger(audit_logger.db_path)
    records = temp_logger.query_logs(limit=5)
    assert len(records) > 0
    assert records[0]["id"] == first_log["id"]


