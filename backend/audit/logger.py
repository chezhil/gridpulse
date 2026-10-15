"""
GridPulse Deterministic SCADA Audit Logger & NERC Compliance Module.
Provides append-only persistence (SQLite) for all grid control actions,
Cypher evidence hashes, and operator/MCP triggers.

Records are hash-chained: each stores ``prev_hash`` (the previous record's
``record_hash``) and a ``record_hash`` over its own fields, including the
evidence hash and the graph ``state_version`` the action was based on.
Editing, deleting or reordering a chained record breaks verification
(``GET /api/audit/verify``). This is tamper-evident, not tamper-proof: someone
with write access could rebuild the whole chain.
"""

import os
import json
import sqlite3
import hashlib
import threading
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
    outcome: str  # REQUESTED | SUCCESS | FAILED | REJECTED
    operator_id: str
    seq: Optional[int] = None
    state_version: Optional[int] = None
    prev_hash: Optional[str] = None
    record_hash: Optional[str] = None


class AuditLogger:
    GENESIS = "GENESIS"
    CHAIN_COLUMNS = {"seq": "INTEGER", "state_version": "INTEGER", "prev_hash": "TEXT", "record_hash": "TEXT"}
    CHAINED_FIELDS = ("seq", "prev_hash", "id", "timestamp", "run_id", "scenario_id", "target", "action", "reason",
                      "evidence_hash", "requested_action", "outcome", "operator_id", "state_version")

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._write_lock = threading.Lock()
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
            # Older databases predate the hash chain; add its columns in place.
            existing = {row[1] for row in cursor.execute("PRAGMA table_info(audit_events)")}
            for column, kind in self.CHAIN_COLUMNS.items():
                if column not in existing:
                    cursor.execute(f"ALTER TABLE audit_events ADD COLUMN {column} {kind}")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_seq ON audit_events(seq)")
            conn.commit()

    @classmethod
    def calculate_record_hash(cls, record: Dict[str, Any]) -> str:
        payload = {k: record.get(k) for k in cls.CHAINED_FIELDS}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
        timestamp: Optional[str] = None,
        state_version: Optional[int] = None
    ) -> Dict[str, Any]:
        """Record an append-only audit event to persistent storage."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        computed_hash = evidence_hash or self.calculate_evidence_hash(evidence)

        ev_json = json.dumps(evidence) if evidence is not None else None
        req_json = json.dumps(requested_action) if requested_action is not None else None

        record = {
            "id": event_id, "timestamp": ts, "run_id": run_id, "scenario_id": scenario_id, "target": target,
            "action": action, "reason": reason, "evidence_hash": computed_hash,
            "requested_action": requested_action, "outcome": outcome, "operator_id": operator_id,
            "state_version": state_version,
        }
        with self._write_lock, sqlite3.connect(self.db_path, isolation_level=None) as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                last = cursor.execute(
                    "SELECT seq, record_hash FROM audit_events WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                record["seq"] = (last[0] + 1) if last else 1
                record["prev_hash"] = last[1] if last else self.GENESIS
                record["record_hash"] = self.calculate_record_hash(record)
                cursor.execute("""
                    INSERT INTO audit_events (
                        id, timestamp, run_id, scenario_id, target, action, reason,
                        evidence_json, evidence_hash, requested_action_json, outcome, operator_id,
                        seq, state_version, prev_hash, record_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id, ts, run_id, scenario_id, target, action, reason,
                    ev_json, computed_hash, req_json, outcome, operator_id,
                    record["seq"], state_version, record["prev_hash"], record["record_hash"]
                ))
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise

        return {
            "seq": record["seq"],
            "state_version": state_version,
            "prev_hash": record["prev_hash"],
            "record_hash": record["record_hash"],
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
        query = "SELECT id, timestamp, run_id, scenario_id, target, action, reason, evidence_json, evidence_hash, requested_action_json, outcome, operator_id, seq, state_version, prev_hash, record_hash FROM audit_events WHERE 1=1"
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

        query += " ORDER BY seq IS NULL, seq DESC, timestamp DESC LIMIT ?"
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
                    "operator_id": row[11],
                    "seq": row[12],
                    "state_version": row[13],
                    "prev_hash": row[14],
                    "record_hash": row[15],
                })

        return results

    def verify_chain(self) -> Dict[str, Any]:
        """Walk the chain in order; report the first broken link, if any."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, timestamp, run_id, scenario_id, target, action, reason, evidence_json, evidence_hash, "
                "requested_action_json, outcome, operator_id, seq, state_version, prev_hash, record_hash "
                "FROM audit_events WHERE seq IS NOT NULL ORDER BY seq"
            ).fetchall()
            legacy = conn.execute("SELECT count(*) FROM audit_events WHERE seq IS NULL").fetchone()[0]
        expected_prev, expected_seq = self.GENESIS, 1
        for row in rows:
            record = {
                "id": row[0], "timestamp": row[1], "run_id": row[2], "scenario_id": row[3], "target": row[4],
                "action": row[5], "reason": row[6], "evidence_hash": row[8],
                "requested_action": json.loads(row[9]) if row[9] else None, "outcome": row[10],
                "operator_id": row[11], "seq": row[12], "state_version": row[13], "prev_hash": row[14],
            }
            problem = None
            if row[12] != expected_seq:
                problem = f"expected seq {expected_seq}, found {row[12]}"
            elif row[14] != expected_prev:
                problem = "prev_hash does not match the previous record"
            elif self.calculate_record_hash(record) != row[15]:
                problem = "record_hash does not match the record's fields"
            elif self.calculate_evidence_hash(json.loads(row[7]) if row[7] else None) != row[8]:
                problem = "evidence_hash does not match the stored evidence"
            if problem:
                return {"valid": False, "checked": expected_seq - 1, "legacy_unchained": legacy,
                        "first_break": {"seq": row[12], "id": row[0], "problem": problem}}
            expected_prev, expected_seq = row[15], expected_seq + 1
        return {"valid": True, "checked": len(rows), "legacy_unchained": legacy,
                "head": {"seq": expected_seq - 1, "record_hash": expected_prev} if rows else None}


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


@audit_router.get("/verify")
def verify_audit_chain() -> Dict[str, Any]:
    """
    GET /api/audit/verify
    Recomputes every record hash and chain link; returns the first break if any.
    """
    try:
        return audit_logger.verify_chain()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify audit chain: {str(e)}")
