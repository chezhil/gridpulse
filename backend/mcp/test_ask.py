"""Ask the grid: read-only guard and the question -> Cypher -> rows flow (scripted model, real FalkorDB)."""
import json
from types import SimpleNamespace

import pytest

from backend import graph
from backend.graph.client import InvalidRequestError
from backend.mcp import ask
from backend.mcp.test_live_runtime import live_runtime  # noqa: F401  (fixture)


@pytest.mark.parametrize("cypher", [
    "MATCH (n) DETACH DELETE n", "MATCH (n:Tower) SET n.status = 'X' RETURN n", "CREATE (:Tower {id:'EVIL'})",
    "MERGE (n:Tower {id:'T1'}) RETURN n", "MATCH (n) REMOVE n.id", "CALL db.idx.fulltext.drop('Tower')",
    "MATCH (n) RETURN n; MATCH (m) DELETE m", "LOAD CSV FROM 'file:///x' AS r RETURN r", "", "MATCH (n) FOREACH (x IN [1] | SET n.a = 1)",
])
def test_guard_rejects_writes_and_procedures(cypher):
    with pytest.raises(InvalidRequestError):
        ask.check_cypher(cypher)


def test_guard_allows_reads_algorithms_and_keywords_in_strings():
    assert ask.check_cypher("MATCH (c:Consumer) WHERE c.name = 'Delete me; SET x' RETURN c.id;") .endswith("RETURN c.id")
    ask.check_cypher("CALL algo.betweenness({nodeLabels:['Tower']}) YIELD node, score RETURN node.id, score")


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))])


def q(cypher, explanation="x"):
    return json.dumps({"cypher": cypher, "explanation": explanation})


def test_ask_runs_read_only_query_and_summarises(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    llm = FakeLLM([q("MATCH (t {id:'T5'})-[:SPAN|TERMINATES_AT*1..10]->(s:Substation) RETURN DISTINCT s.id AS substation"),
                   "SUB_METRO is downstream of T5."])
    r = ask.ask("Which substations are downstream of T5?", llm=llm)
    assert r["rows"] == [{"substation": "SUB_METRO"}] and r["answer"] == "SUB_METRO is downstream of T5."
    assert "SUB_METRO" in r["highlight_ids"] and r["evidence_hash"].startswith("sha256:")
    assert llm.calls[0]["response_format"] == {"type": "json_object"}
    assert "Tower: 9 nodes" in llm.calls[0]["messages"][0]["content"]  # real ids from this graph


def test_ask_retries_once_after_bad_query_then_gives_up(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    llm = FakeLLM([q("MATCH (n) DELETE n"), q("MATCH (c:Consumer) RETURN count(c) AS n"), "There are 4 consumers."])
    r = ask.ask("How many consumers?", llm=llm)
    assert r["attempts"] == 2 and r["rows"] == [{"n": 4}]
    assert "not allowed" in llm.calls[1]["messages"][-1]["content"]
    assert graph.get_topology()["counts"]["nodes"] == 16  # nothing was deleted
    with pytest.raises(InvalidRequestError):
        ask.ask("break it", llm=FakeLLM([q("CREATE (:X)"), q("MATCH (n) SET n.x = 1")]), summarize=False)


def test_unanswerable_question(live_runtime):  # noqa: F811
    graph.init_graph(reset=True)
    r = ask.ask("What is the weather tomorrow?", llm=FakeLLM([q("", "The graph has no weather data.")]))
    assert r["answerable"] is False and "weather" in r["explanation"]


def test_websocket_rejects_bad_session_with_policy_code(live_runtime):  # noqa: F811
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from backend.main import app
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect('/ws/telemetry?token=stale') as ws:
                ws.receive_json()
        assert exc.value.code == 1008
