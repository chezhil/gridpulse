"""Operational memory stored in the grid graph itself.

Every finished incident becomes part of the graph, linked to the grid
elements it happened on, so later runs (and the AI agent) can ask
"what happened near here before, and what fixed it?" with one multi-hop query.

    (:Incident)-[:AT]->(:Tower|:Substation|:HydroPlant)
    (:Incident)-[:TOOK {seq}]->(:Action)-[:TARGETED]->(:Consumer|:BESS)
    (:Incident)-[:RESULTED_IN]->(:Outcome)
    (:Playbook)-[:LEARNED_FROM]->(:Incident)
    (:Playbook)-[:APPLIES_TO]->(:Tower|:Substation)

Episodic memory is the Incident/Action/Outcome chain; procedural memory is a
Playbook, keyed by the incident's trigger signature, that counts how often a
plan worked. Memory labels and relationship types are outside the canonical
topology, so grid resets keep them.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .client import GraphClient, InvalidRequestError, get_client
from .queries import _timing, validate_id

MEMORY_LABELS = ("Incident", "Action", "Outcome", "Playbook")

RECORD_INCIDENT = """\
CREATE (i:Incident {id: $id, run_id: $run_id, source: $source, scenario: $scenario, origin_id: $origin_id,
                    signature: $signature, trips_json: $trips, overloads_json: $overloads,
                    required_relief_mw: $relief, state_version: $state_version, started_at: $now})
WITH i
OPTIONAL MATCH (n) WHERE n.id IN $at_ids AND (n:Tower OR n:Substation OR n:HydroPlant)
WITH i, collect(n) AS places
FOREACH (p IN places | CREATE (i)-[:AT]->(p))
CREATE (i)-[:RESULTED_IN]->(:Outcome {status: $status, final_outcome: $final_outcome, note: $note, recorded_at: $now})
RETURN i.id AS id, size(places) AS linked"""

RECORD_ACTION = """\
MATCH (i:Incident {id: $incident_id})
UNWIND $actions AS a
CREATE (i)-[:TOOK {seq: a.seq}]->(act:Action {id: a.id, kind: a.kind, target: a.target, mw: a.mw})
WITH act, a
OPTIONAL MATCH (t) WHERE t.id = a.target_node AND (t:Consumer OR t:BESS)
FOREACH (x IN CASE WHEN t IS NULL THEN [] ELSE [t] END | CREATE (act)-[:TARGETED]->(x))
RETURN count(act) AS actions"""

UPDATE_PLAYBOOK = """\
MATCH (i:Incident {id: $incident_id})
MERGE (p:Playbook {id: $signature})
ON CREATE SET p.success_count = 0, p.failure_count = 0, p.created_at = $now, p.trigger = $trigger
SET p.success_count = p.success_count + $success, p.failure_count = p.failure_count + $failure, p.last_used = $now
FOREACH (_ IN CASE WHEN $success = 1 THEN [1] ELSE [] END | SET p.plan_json = $plan, p.last_success_incident = $incident_id)
MERGE (p)-[:LEARNED_FROM]->(i)
WITH p
OPTIONAL MATCH (n) WHERE n.id IN $at_ids AND (n:Tower OR n:Substation OR n:HydroPlant)
FOREACH (x IN CASE WHEN n IS NULL THEN [] ELSE [n] END | MERGE (p)-[:APPLIES_TO]->(x))
RETURN p.success_count AS success_count, p.failure_count AS failure_count"""

RECALL = """\
MATCH (o) WHERE o.id IN $ids AND (o:Tower OR o:Substation OR o:HydroPlant)
MATCH (o)-[:SPAN|TERMINATES_AT|FEEDS*0..{hops}]-(n)
WITH collect(DISTINCT n.id) AS near
MATCH (i:Incident)-[:AT]->(p) WHERE p.id IN near
WITH DISTINCT i, near
OPTIONAL MATCH (i)-[:RESULTED_IN]->(out:Outcome)
OPTIONAL MATCH (i)-[t:TOOK]->(a:Action)
WITH i, out, a, t ORDER BY t.seq
WITH i, out, collect(CASE WHEN a IS NULL THEN NULL ELSE {{kind: a.kind, target: a.target, mw: a.mw}} END) AS actions
OPTIONAL MATCH (i)-[:AT]->(place)
RETURN i.id AS id, i.started_at AS started_at, i.source AS source, i.scenario AS scenario, i.origin_id AS origin_id,
       i.signature AS signature, i.required_relief_mw AS required_relief_mw, out.status AS status,
       out.final_outcome AS final_outcome, actions, collect(DISTINCT place.id) AS places
ORDER BY started_at DESC
LIMIT $limit"""

RECALL_PLAYBOOKS = """\
MATCH (o) WHERE o.id IN $ids AND (o:Tower OR o:Substation OR o:HydroPlant)
MATCH (o)-[:SPAN|TERMINATES_AT|FEEDS*0..{hops}]-(n)
WITH collect(DISTINCT n.id) AS near
MATCH (p:Playbook)-[:APPLIES_TO]->(x) WHERE x.id IN near
RETURN DISTINCT p.id AS signature, p.trigger AS trigger, p.plan_json AS plan_json, p.success_count AS success_count,
       p.failure_count AS failure_count, p.last_used AS last_used
ORDER BY success_count DESC, last_used DESC"""

LIST_INCIDENTS = """\
MATCH (i:Incident)
OPTIONAL MATCH (i)-[:RESULTED_IN]->(out:Outcome)
OPTIONAL MATCH (i)-[t:TOOK]->(a:Action)
WITH i, out, a, t ORDER BY t.seq
WITH i, out, collect(CASE WHEN a IS NULL THEN NULL ELSE {kind: a.kind, target: a.target, mw: a.mw} END) AS actions
RETURN i.id AS id, i.started_at AS started_at, i.source AS source, i.scenario AS scenario, i.origin_id AS origin_id,
       i.signature AS signature, i.required_relief_mw AS required_relief_mw, out.status AS status,
       out.final_outcome AS final_outcome, actions
ORDER BY started_at DESC
LIMIT $limit"""

LIST_PLAYBOOKS = """\
MATCH (p:Playbook)
OPTIONAL MATCH (p)-[:APPLIES_TO]->(x)
RETURN p.id AS signature, p.trigger AS trigger, p.plan_json AS plan_json, p.success_count AS success_count,
       p.failure_count AS failure_count, p.last_used AS last_used, collect(x.id) AS applies_to
ORDER BY success_count DESC"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def signature_for(trips: List[Dict[str, str]], demand_scale: Optional[float] = None) -> str:
    """Stable trigger key: the tripped circuits, plus a demand-surge marker."""
    parts = sorted(f"TRIP:{t['span_id']}:{t['circuit']}" for t in trips)
    if demand_scale and demand_scale > 1.0:
        parts.append("DEMAND_SURGE")
    return "|".join(parts) or "UNSPECIFIED"


def _places(client: GraphClient, origin_id: str) -> List[str]:
    """Span origins are anchored at both endpoints; node origins at themselves."""
    q = client.query(
        "memory_places",
        "OPTIONAL MATCH (a)-[r:SPAN|TERMINATES_AT]->(b) WHERE r.id = $id "
        "WITH collect(DISTINCT a.id) + collect(DISTINCT b.id) AS ends RETURN ends",
        {"id": origin_id},
    )
    ends = q.rows[0]["ends"] if q.rows else []
    return sorted(set(ends)) or [origin_id]


def record_incident(
    incident: Dict[str, Any], client: Optional[GraphClient] = None
) -> Dict[str, Any]:
    """Store a finished incident and update the matching playbook.

    ``incident`` keys: ``run_id``, ``source`` (INTERCEPTOR|AGENT), ``scenario``,
    ``origin_id``, ``trips`` [{span_id, circuit}], ``demand_scale``,
    ``overloads`` [str], ``required_relief_mw``, ``state_version``,
    ``status`` (MITIGATED|FAILED), ``final_outcome``, ``note``, ``plan``
    {trip_breakers: [...], bess_dispatch: {id: mw}}, ``breaker_targets``
    {breaker_id: consumer_id}.
    """
    client = client or get_client()
    started = time.perf_counter()
    origin = validate_id(incident.get("origin_id"), "origin_id")
    status = incident.get("status")
    if status not in ("MITIGATED", "FAILED"):
        raise InvalidRequestError("status must be MITIGATED or FAILED", value=status)
    trips = incident.get("trips") or []
    signature = signature_for(trips, incident.get("demand_scale"))
    at_ids = _places(client, origin)
    incident_id = "INC_" + uuid.uuid4().hex[:10].upper()
    now = _now()
    plan = incident.get("plan") or {"trip_breakers": [], "bess_dispatch": {}}
    outcomes = [client.query("memory_record_incident", RECORD_INCIDENT, {
        "id": incident_id, "run_id": incident.get("run_id") or "", "source": incident.get("source") or "INTERCEPTOR",
        "scenario": incident.get("scenario") or "", "origin_id": origin, "signature": signature,
        "trips": json.dumps(trips), "overloads": json.dumps(incident.get("overloads") or []),
        "relief": incident.get("required_relief_mw"), "state_version": incident.get("state_version"),
        "now": now, "at_ids": at_ids, "status": status, "final_outcome": incident.get("final_outcome") or "",
        "note": (incident.get("note") or "")[:500],
    }, read_only=False, retries=0)]
    actions = [{"seq": i, "id": f"{incident_id}_A{i}", "kind": "TRIP_BREAKER", "target": b, "mw": None,
                "target_node": (incident.get("breaker_targets") or {}).get(b)}
               for i, b in enumerate(plan.get("trip_breakers") or [])]
    actions += [{"seq": len(actions) + i, "id": f"{incident_id}_A{len(actions) + i}", "kind": "DISPATCH_BESS", "target": bid,
                 "mw": float(mw), "target_node": bid}
                for i, (bid, mw) in enumerate(sorted((plan.get("bess_dispatch") or {}).items()))]
    if actions:
        outcomes.append(client.query("memory_record_actions", RECORD_ACTION, {"incident_id": incident_id, "actions": actions},
                                     read_only=False, retries=0))
    pb = client.query("memory_update_playbook", UPDATE_PLAYBOOK, {
        "incident_id": incident_id, "signature": signature, "trigger": signature, "now": now,
        "success": 1 if status == "MITIGATED" else 0, "failure": 0 if status == "MITIGATED" else 1,
        "plan": json.dumps(plan), "at_ids": at_ids}, read_only=False, retries=0)
    outcomes.append(pb)
    return {"incident_id": incident_id, "signature": signature, "linked_to": at_ids,
            "playbook": pb.rows[0] if pb.rows else None, "evidence": [o.evidence() for o in outcomes],
            "timing": _timing(outcomes, started)}


def recall(origin_id: str, hops: int = 2, limit: int = 10, client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Past incidents and playbooks within ``hops`` grid hops of ``origin_id``."""
    client = client or get_client()
    started = time.perf_counter()
    validate_id(origin_id, "origin_id")
    if isinstance(hops, bool) or not isinstance(hops, int) or not 0 <= hops <= 6:
        raise InvalidRequestError("hops must be an integer between 0 and 6", value=hops)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise InvalidRequestError("limit must be an integer between 1 and 100", value=limit)
    ids = _places(client, origin_id)
    iq = client.query("memory_recall_incidents", RECALL.format(hops=hops), {"ids": ids, "limit": limit})
    pq = client.query("memory_recall_playbooks", RECALL_PLAYBOOKS.format(hops=hops), {"ids": ids})
    incidents = [{**r, "actions": [a for a in r["actions"] if a]} for r in iq.rows]
    playbooks = [{**{k: v for k, v in r.items() if k != "plan_json"}, "plan": json.loads(r["plan_json"]) if r["plan_json"] else None}
                 for r in pq.rows]
    outcomes = [iq, pq]
    return {"origin_id": origin_id, "anchors": ids, "hops": hops, "incidents": incidents, "playbooks": playbooks,
            "evidence": [o.evidence() for o in outcomes], "timing": _timing(outcomes, started)}


def list_incidents(limit: int = 50, client: Optional[GraphClient] = None) -> Dict[str, Any]:
    client = client or get_client()
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise InvalidRequestError("limit must be an integer between 1 and 500", value=limit)
    q = client.query("memory_list_incidents", LIST_INCIDENTS, {"limit": limit})
    return {"incidents": [{**r, "actions": [a for a in r["actions"] if a]} for r in q.rows], "evidence": [q.evidence()]}


def list_playbooks(client: Optional[GraphClient] = None) -> Dict[str, Any]:
    client = client or get_client()
    q = client.query("memory_list_playbooks", LIST_PLAYBOOKS)
    return {"playbooks": [{**{k: v for k, v in r.items() if k != "plan_json"},
                           "plan": json.loads(r["plan_json"]) if r["plan_json"] else None} for r in q.rows],
            "evidence": [q.evidence()]}
