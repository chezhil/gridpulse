"""
GridPulse Deterministic SCADA Audit Logger & NERC Compliance Module.
Provides append-only persistence (SQLite) for all grid control actions,
Cypher evidence hashes, and operator/MCP triggers.
"""

import os
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

DEFAULT_DB_PATH = os.environ.get(
    "GRIDPULSE_AUDIT_DB",
    str(Path(__file__).resolve().parent.parent.parent / "data" / "audit_log.db")
)


class AuditEventModel(BaseModel):
    id: str
    timestamp: str
    run_id: str
    scenario_id: Optional[int] = None
    target: str
    action: str
    reason: str
    evidence: Optional[Dict[str, Any]] = None
    evidence_hash: str
    requested_action: Optional[Dict[str, Any]] = None
    outcome: str  # SUCCESS | FAILED | REJECTED
    operator_id: str


class AuditLogger:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    scenario_id INTEGER,
                    target TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT,
                    evidence_hash TEXT NOT NULL,
                    requested_action_json TEXT,
                    outcome TEXT NOT NULL,
                    operator_id TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_run_id ON audit_events(run_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_scenario ON audit_events(scenario_id)")
            conn.commit()

    @staticmethod
    def calculate_evidence_hash(evidence_data: Any) -> str:
        """Deterministically hash the evidence payload (Cypher query, parameters, result IDs)."""
        if not evidence_data:
            return hashlib.sha256(b"NO_EVIDENCE_SUPPLIED").hexdigest()
        canonical_json = json.dumps(evidence_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def log_event(
        self,
        event_id: str,
        run_id: str,
        target: str,
        action: str,
        reason: str,
        outcome: str,
        operator_id: str = "SCADA_MCP_AGENT",
        scenario_id: Optional[int] = None,
        evidence: Optional[Dict[str, Any]] = None,
        evidence_hash: Optional[str] = None,
        requested_action: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None
    ) -> Dict[str, Any]:
        """Record an append-only audit event to persistent storage."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        computed_hash = evidence_hash or self.calculate_evidence_hash(evidence)

        ev_json = json.dumps(evidence) if evidence is not None else None
        req_json = json.dumps(requested_action) if requested_action is not None else None

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO audit_events (
                    id, timestamp, run_id, scenario_id, target, action, reason,
                    evidence_json, evidence_hash, requested_action_json, outcome, operator_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id, ts, run_id, scenario_id, target, action, reason,
                ev_json, computed_hash, req_json, outcome, operator_id
            ))
            conn.commit()

        return {
            "id": event_id,
            "timestamp": ts,
            "run_id": run_id,
            "scenario_id": scenario_id,
            "target": target,
            "action": action,
            "reason": reason,
            "evidence": evidence,
            "evidence_hash": computed_hash,
            "requested_action": requested_action,
            "outcome": outcome,
            "operator_id": operator_id
        }

    def query_logs(
        self,
        limit: int = 50,
        scenario_id: Optional[int] = None,
        run_id: Optional[str] = None,
        target: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve recent audit logs with optional filters."""
        query = "SELECT id, timestamp, run_id, scenario_id, target, action, reason, evidence_json, evidence_hash, requested_action_json, outcome, operator_id FROM audit_events WHERE 1=1"
        params: List[Any] = []

        if scenario_id is not None:
            query += " AND scenario_id = ?"
            params.append(scenario_id)
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if target:
            query += " AND target = ?"
            params.append(target)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        results = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                ev_json = json.loads(row[7]) if row[7] else None
                req_json = json.loads(row[9]) if row[9] else None
                results.append({
                    "id": row[0],
                    "timestamp": row[1],
                    "run_id": row[2],
                    "scenario_id": row[3],
                    "target": row[4],
                    "action": row[5],
                    "reason": row[6],
                    "evidence": ev_json,
                    "evidence_hash": row[8],
                    "requested_action": req_json,
                    "outcome": row[10],
                    "operator_id": row[11]
                })

        return results


# Global singleton logger
audit_logger = AuditLogger()

# FastAPI Router
audit_router = APIRouter()


@audit_router.get("/logs", response_model=List[AuditEventModel])
def get_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    scenario_id: Optional[int] = Query(None),
    run_id: Optional[str] = Query(None),
    target: Optional[str] = Query(None)
):
    """
    GET /api/audit/logs
    Returns deterministic, timestamped NERC CIP compliance audit logs
    demonstrating the chain of evidence for grid actions.
    """
    try:
        return audit_logger.query_logs(
            limit=limit,
            scenario_id=scenario_id,
            run_id=run_id,
            target=target
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query audit logs: {str(e)}")
