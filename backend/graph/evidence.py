"""Deterministic hashing of query evidence.

The hash covers the exact Cypher text, its parameters and the returned rows,
so an audit record can prove which graph result a decision was based on.
Anyone holding the three values can recompute it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def evidence_hash(cypher: str, params: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    payload = canonical_json({"cypher": cypher, "params": params, "rows": rows})
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_evidence(record: Dict[str, Any]) -> bool:
    """True when ``record['evidence_hash']`` matches its cypher/params/rows."""
    try:
        expected = evidence_hash(record["cypher"], record["params"], record["rows"])
    except KeyError:
        return False
    return record.get("evidence_hash") == expected
