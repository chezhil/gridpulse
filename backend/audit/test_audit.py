"""Hash-chained audit log: integrity, tamper detection, legacy migration."""
import sqlite3

from backend.audit.logger import AuditLogger


def log(store, n, **extra):
    return [store.log_event(event_id=f'E{i}', run_id='R1', target='SUB_METRO', action='TEST', reason=f'r{i}',
                            outcome='SUCCESS', evidence={'i': i}, state_version=i, **extra) for i in range(n)]


def test_chain_links_and_verifies(tmp_path):
    store = AuditLogger(str(tmp_path / 'a.db'))
    events = log(store, 4)
    assert [e['seq'] for e in events] == [1, 2, 3, 4]
    assert events[0]['prev_hash'] == AuditLogger.GENESIS
    assert all(b['prev_hash'] == a['record_hash'] for a, b in zip(events, events[1:]))
    result = store.verify_chain()
    assert result['valid'] and result['checked'] == 4 and result['head']['record_hash'] == events[-1]['record_hash']
    assert store.query_logs()[0]['state_version'] == 3


def test_edits_and_deletes_are_detected(tmp_path):
    path = str(tmp_path / 'a.db')
    store = AuditLogger(path)
    log(store, 4)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE audit_events SET reason = 'edited' WHERE seq = 2")
    broken = store.verify_chain()
    assert not broken['valid'] and broken['first_break']['seq'] == 2

    store2 = AuditLogger(str(tmp_path / 'b.db'))
    log(store2, 4)
    with sqlite3.connect(store2.db_path) as conn:
        conn.execute('DELETE FROM audit_events WHERE seq = 3')
    assert store2.verify_chain()['first_break']['seq'] == 4

    store3 = AuditLogger(str(tmp_path / 'c.db'))
    log(store3, 2)
    with sqlite3.connect(store3.db_path) as conn:
        conn.execute("UPDATE audit_events SET evidence_json = '{\"i\": 99}' WHERE seq = 1")
    assert 'evidence' in store3.verify_chain()['first_break']['problem']


def test_legacy_database_is_migrated(tmp_path):
    path = str(tmp_path / 'legacy.db')
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE audit_events (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, run_id TEXT NOT NULL,
            scenario_id INTEGER, target TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT,
            evidence_hash TEXT NOT NULL, requested_action_json TEXT, outcome TEXT NOT NULL, operator_id TEXT NOT NULL)""")
        conn.execute("INSERT INTO audit_events VALUES ('OLD', '2026-10-15T00:00:00', 'R0', 0, 'X', 'A', 'r', NULL, 'h', NULL, 'SUCCESS', 'op')")
    store = AuditLogger(path)
    log(store, 2)
    result = store.verify_chain()
    assert result['valid'] and result['checked'] == 2 and result['legacy_unchained'] == 1
    assert len(store.query_logs()) == 3
