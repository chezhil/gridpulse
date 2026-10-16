"""Ask the grid: plain-English questions answered with read-only Cypher on FalkorDB.

The LLM (Groq) only writes the query. GridPulse then enforces safety itself:

* a keyword guard rejects anything that could write (CREATE, MERGE, SET, DELETE,
  REMOVE, DROP, FOREACH, LOAD CSV) or call non-algorithm procedures;
* the query runs through FalkorDB's ``GRAPH.RO_QUERY`` (the database rejects writes);
* a row cap and the client's query timeout bound the cost.

If the query fails, the error goes back to the model once for a corrected query.
The answer always shows the exact Cypher, rows, timing and an evidence hash.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

from backend import graph
from backend.graph.client import GraphClient, GraphError, InvalidRequestError
from backend.mcp.agent import AgentFailed, AgentUnavailable, MODEL, make_client

MAX_ROWS = 200
MAX_QUESTION = 500

SCHEMA = """FalkorDB property graph of an electricity grid (Cypher, openCypher dialect).
Nodes (all have string `id`):
  (:HydroPlant {name, output_mw, capacity_mw})          power plants
  (:Tower {status, world_x})                            buses / towers
  (:Substation {capacity_mw, current_load_mw, status})  load buses
  (:BESS {soc_pct, max_discharge_mw, active_injection_mw, status})   batteries
  (:Consumer {name, category, priority_tier, mw_demand, breaker_id, energized})  loads; tier 1 = critical (hospital), 3 = sheddable
  (:Incident {signature, source, scenario, started_at}) (:Action {kind, target, mw}) (:Outcome {status}) (:Playbook {success_count, failure_count})
Relationships:
  (:HydroPlant)-[:FEEDS]->(:Tower|:Substation)
  (a)-[:SPAN {id, circuit, impedance_pu, limit_mw, flow_mw, status, n1_outcome}]->(b)   transmission circuit between buses;
      each line id has circuit 'UPPER_PRIMARY' and maybe 'LOWER_BACKUP'; the last hop into a Substation uses type TERMINATES_AT
      with the same properties. Traverse transmission with [:FEEDS|SPAN|TERMINATES_AT*1..N].
  (:BESS)-[:CONNECTED {max_mw}]->(:Substation)
  (:Substation)-[:DISTRIBUTES_TO {feeder_id, priority_tier}]->(:Consumer)
  (:Incident)-[:AT]->(bus) (:Incident)-[:TOOK]->(:Action)-[:TARGETED]->(:Consumer|:BESS) (:Incident)-[:RESULTED_IN]->(:Outcome)
  (:Playbook)-[:APPLIES_TO]->(bus)
Graph algorithms you may CALL (read-only):
  CALL algo.betweenness({nodeLabels:['HydroPlant','Tower','Substation'], relationshipTypes:['FEEDS','SPAN','TERMINATES_AT']}) YIELD node, score
  CALL algo.WCC({nodeLabels:[...], relationshipTypes:[...]}) YIELD node, componentId
  MATCH (s {id:'X'}), (t {id:'Y'}) CALL algo.SPpaths({sourceNode:s, targetNode:t, relTypes:['FEEDS','SPAN','TERMINATES_AT'], weightProp:'impedance_pu', pathCount:3}) YIELD path, pathWeight
Examples:
  Q: Which substations are downstream of T5?
     MATCH (t {id:'T5'})-[:SPAN|TERMINATES_AT*1..10]->(s:Substation) RETURN DISTINCT s.id AS substation
  Q: Which critical loads are there and where?
     MATCH (s:Substation)-[:DISTRIBUTES_TO]->(c:Consumer) WHERE c.priority_tier = 1 RETURN c.id, c.name, s.id AS substation
  Q: Which circuits are above 80% of their limit?
     MATCH (a)-[r:SPAN|TERMINATES_AT]->(b) WHERE r.limit_mw > 0 AND r.flow_mw > 0.8 * r.limit_mw
     RETURN r.id, r.circuit, a.id AS from, b.id AS to, round(100.0 * r.flow_mw / r.limit_mw) AS loading_pct ORDER BY loading_pct DESC"""

INSTRUCTIONS = """Write ONE read-only Cypher query that answers the user's question on the graph described below.
Return JSON only: {"cypher": "...", "explanation": "one sentence on what the query does"}.
Rules: read-only (no CREATE/MERGE/SET/DELETE/REMOVE/DROP/FOREACH); use only labels, relationship types and
properties from the schema; use the exact ids given; alias returned columns readably; add ORDER BY when ranking;
add LIMIT 50 unless the question asks for everything. If the question cannot be answered from this graph,
return {"cypher": "", "explanation": "why not"}."""

_WRITE = re.compile(r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b", re.I)
_CALL = re.compile(r"\bCALL\s+([A-Za-z0-9_.]+)", re.I)
_ID_VALUE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")


def _collect_ids(value, found=None):
    """Every graph id mentioned anywhere in the result rows (for highlighting in the UI)."""
    found = set() if found is None else found
    if isinstance(value, str) and _ID_VALUE.match(value):
        found.add(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_ids(v, found)
    elif isinstance(value, list):
        for v in value:
            _collect_ids(v, found)
    return found


def _bare(q: str) -> str:
    """``q`` with string literals emptied, comments blanked and backtick names unquoted.

    A single left-to-right scan, so a quote inside a comment or a backtick name
    can't hide a clause the way a regex string-stripper can be fooled.
    """
    out, i, n = [], 0, len(q)
    while i < n:
        ch = q[i]
        if ch in "'\"":
            j = i + 1
            while j < n and q[j] != ch:
                j += 2 if q[j] == "\\" else 1
            if j >= n:
                raise InvalidRequestError("unterminated string literal")
            out.append("''")
            i = j + 1
        elif ch == "`":
            j = q.find("`", i + 1)
            if j < 0:
                raise InvalidRequestError("unterminated backtick name")
            # Keep the name visible to the clause checks; anything odd becomes "_".
            out.append(re.sub(r"[^A-Za-z0-9_.]", "_", q[i + 1:j]))
            i = j + 1
        elif q.startswith("//", i):
            j = q.find("\n", i)
            out.append(" ")
            i = n if j < 0 else j
        elif q.startswith("/*", i):
            j = q.find("*/", i + 2)
            if j < 0:
                raise InvalidRequestError("unterminated comment")
            out.append(" ")
            i = j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_cypher(cypher: str) -> str:
    """Reject anything that is not a single read-only query. Returns the cleaned query."""
    q = (cypher or "").strip().rstrip(";").strip()
    if not q:
        raise InvalidRequestError("empty query")
    if len(q) > 4000:
        raise InvalidRequestError("query too long")
    bare = _bare(q)  # keywords inside strings and comments don't count; backtick names do
    if ";" in bare:
        raise InvalidRequestError("only one statement is allowed")
    if _WRITE.search(bare):
        raise InvalidRequestError(f"write clause '{_WRITE.search(bare).group(1)}' is not allowed")
    for proc in _CALL.findall(bare):
        if not proc.lower().startswith("algo."):
            raise InvalidRequestError(f"procedure '{proc}' is not allowed (only algo.*)")
    return q


def _schema_context(client: GraphClient) -> str:
    """Labels with counts and a few real ids, so the model uses ids that exist in this graph."""
    q = client.query("ask_schema_sample",
                     "MATCH (n) WHERE n.id IS NOT NULL AND NOT n:GridMeta AND NOT n:Action AND NOT n:Outcome "
                     "WITH labels(n)[0] AS label, collect(n.id) AS ids RETURN label, size(ids) AS n, ids[0..12] AS sample ORDER BY label")
    return "\n".join(f"  {r['label']}: {r['n']} nodes, e.g. {', '.join(map(str, r['sample']))}" for r in q.rows)


def _retry_after(exc, default=5.0, cap=20.0) -> float:
    """Seconds to wait from a rate-limit response; a date or junk header falls back to the default."""
    try:
        value = float(getattr(exc.response, "headers", {}).get("retry-after") or default)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.0), cap)


def _complete(client, messages, json_mode=True) -> str:
    import groq
    kwargs = dict(model=MODEL, messages=messages, max_completion_tokens=2048, temperature=0.1)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if MODEL.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"
    for attempt in range(3):
        try:
            r = client.chat.completions.create(**kwargs)
            return r.choices[0].message.content or ""
        except groq.RateLimitError as exc:
            if attempt == 2:
                raise AgentFailed("Groq rate limit: try again in a minute") from exc
            time.sleep(_retry_after(exc))
        except groq.AuthenticationError as exc:
            raise AgentUnavailable("Groq credentials missing or invalid; set GROQ_API_KEY") from exc
        except groq.APIStatusError as exc:
            raise AgentFailed(f"Groq API error {exc.status_code}: {exc.message}") from exc
        except groq.APIConnectionError as exc:
            raise AgentFailed("Could not reach the Groq API") from exc
    raise AgentFailed("no response")


def _parse(text: str) -> Dict[str, str]:
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AgentFailed("model did not return JSON")
        try:
            data = json.loads(m.group(0))
        except ValueError as exc:
            raise AgentFailed("model returned malformed JSON") from exc
    if not isinstance(data, dict):
        raise AgentFailed("model did not return a JSON object")
    return {"cypher": str(data.get("cypher") or ""), "explanation": str(data.get("explanation") or "")}


def ask(question: str, graph_client: Optional[GraphClient] = None, llm=None, summarize: bool = True,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Answer ``question`` with one read-only Cypher query on ``graph_client``'s graph."""
    if not isinstance(question, str) or not question.strip():
        raise InvalidRequestError("question is required")
    question = question.strip()[:MAX_QUESTION]
    gc = graph_client or graph.get_client()
    llm = llm or make_client()
    started = time.perf_counter()
    context = _schema_context(gc)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": f"{INSTRUCTIONS}\n\n{SCHEMA}\n\nIds present in this graph:\n{context}"},
        {"role": "user", "content": question},
    ]
    attempts = []
    for attempt in range(2):
        proposal = _parse(_complete(llm, messages))
        attempts.append(proposal)
        if not proposal["cypher"]:
            return {"question": question, "answerable": False, "explanation": proposal["explanation"], "attempts": attempts,
                    "model": MODEL, "elapsed_s": round(time.perf_counter() - started, 2)}
        try:
            cypher = check_cypher(proposal["cypher"])
            outcome = gc.query("ask_the_grid", cypher, {})  # read-only: GRAPH.RO_QUERY rejects writes
            break
        except (InvalidRequestError, GraphError) as exc:
            if attempt == 1:
                raise InvalidRequestError(f"could not produce a valid read-only query: {exc.message}",
                                          attempts=attempts) from exc
            messages += [{"role": "assistant", "content": json.dumps(proposal)},
                         {"role": "user", "content": f"That query failed: {exc.message}. Return a corrected JSON object."}]
    rows = outcome.rows[:MAX_ROWS]
    evidence = outcome.evidence()
    ids = sorted(_collect_ids(rows))
    answer = None
    if summarize:
        preview = json.dumps(rows[:30], default=str, separators=(",", ":"))[:6000]
        answer = _complete(llm, [
            {"role": "system", "content": "Answer the question in at most two sentences using only these query results. "
                                          "If the results are empty, say what that means. No markdown."},
            {"role": "user", "content": f"Question: {question}\nCypher: {cypher}\nRows ({len(outcome.rows)} total): {preview}"}],
            json_mode=False).strip()
    return {
        "question": question, "answerable": True, "answer": answer, "cypher": cypher, "explanation": attempts[-1]["explanation"],
        "columns": list(rows[0].keys()) if rows else [], "rows": rows, "row_count": len(outcome.rows), "truncated": len(outcome.rows) > MAX_ROWS,
        "highlight_ids": ids[:200], "server_ms": round(outcome.server_ms, 4), "evidence_hash": evidence["evidence_hash"],
        "attempts": len(attempts), "model": MODEL, "elapsed_s": round(time.perf_counter() - started, 2),
    }
