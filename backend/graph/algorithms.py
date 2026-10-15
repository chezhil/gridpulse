"""FalkorDB graph algorithms applied to the grid topology.

* :func:`criticality_ranking` uses ``algo.betweenness`` to find the buses that
  the most source-to-load paths depend on, joined with N-1 results.
* :func:`reroute_paths` uses ``algo.SPpaths`` (k weighted shortest paths over
  ``impedance_pu``) and drops any path that crosses a tripped circuit.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .client import GraphClient, InvalidRequestError, UnknownEntityError, get_client
from .queries import _timing, validate_id

BETWEENNESS_CYPHER = """\
CALL algo.betweenness({nodeLabels: ['HydroPlant', 'Tower', 'Substation'],
                       relationshipTypes: ['FEEDS', 'SPAN', 'TERMINATES_AT']})
YIELD node, score
RETURN node.id AS id, labels(node)[0] AS label, score
ORDER BY score DESC, id"""

N1_BY_NODE_CYPHER = """\
MATCH (a)-[r:SPAN|TERMINATES_AT]->(b)
WHERE r.n1_outcome IS NOT NULL AND r.n1_outcome <> 'WITHIN_LIMITS'
RETURN a.id AS src, b.id AS dst, r.id AS span_id, r.circuit AS circuit, r.n1_outcome AS outcome,
       r.n1_state_version AS state_version"""

REROUTE_CYPHER = """\
MATCH (s {{id: $source_id}}), (t {{id: $target_id}})
CALL algo.SPpaths({{sourceNode: s, targetNode: t, relTypes: ['FEEDS', 'SPAN', 'TERMINATES_AT'],
                   weightProp: 'impedance_pu', maxLen: {max_len}, pathCount: $candidates}})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.id] AS nodes,
       [r IN relationships(path) | {{type: type(r), span_id: r.id, circuit: r.circuit, status: r.status}}] AS hops,
       pathWeight AS impedance_pu
ORDER BY impedance_pu"""


def criticality_ranking(limit: int = 10, client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Buses ranked by betweenness centrality, with any N-1-critical circuits touching them."""
    client = client or get_client()
    started = time.perf_counter()
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise InvalidRequestError("limit must be an integer between 1 and 100", value=limit)
    bq = client.query("betweenness", BETWEENNESS_CYPHER)
    nq = client.query("n1_tags", N1_BY_NODE_CYPHER)
    critical_by_node: Dict[str, List[str]] = {}
    for r in nq.rows:
        for node in (r["src"], r["dst"]):
            critical_by_node.setdefault(node, []).append(f"{r['span_id']}:{r['circuit']}")
    ranked = [
        {
            "id": r["id"],
            "label": r["label"],
            "betweenness": round(float(r["score"]), 4),
            "n1_critical_circuits": sorted(set(critical_by_node.get(r["id"], []))),
        }
        for r in bq.rows[:limit]
    ]
    outcomes = [bq, nq]
    return {
        "algorithm": "algo.betweenness",
        "ranking": ranked,
        "n1_tags_present": bool(nq.rows),
        "evidence": [o.evidence() for o in outcomes],
        "timing": _timing(outcomes, started),
    }


def reroute_paths(
    source_id: str = "HYDRO_ALPINE_01",
    target_id: str = "SUB_METRO",
    k: int = 3,
    max_len: int = 12,
    client: Optional[GraphClient] = None,
) -> Dict[str, Any]:
    """Up to ``k`` lowest-impedance energised paths from ``source_id`` to ``target_id``."""
    client = client or get_client()
    started = time.perf_counter()
    validate_id(source_id, "source_id")
    validate_id(target_id, "target_id")
    for name, value, hi in (("k", k, 10), ("max_len", max_len, 20)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= hi:
            raise InvalidRequestError(f"{name} must be an integer between 1 and {hi}", value=value)
    # Ask for extra candidates because tripped paths are filtered out afterwards.
    candidates = min(k * 8, 64)
    q = client.query(
        "reroute_paths",
        REROUTE_CYPHER.format(max_len=max_len),
        {"source_id": source_id, "target_id": target_id, "candidates": candidates},
    )
    if not q.rows:
        exists = client.query(
            "reroute_endpoints",
            "MATCH (n) WHERE n.id IN $ids RETURN collect(n.id) AS ids",
            {"ids": [source_id, target_id]},
        )
        found = set(exists.rows[0]["ids"]) if exists.rows else set()
        missing = sorted({source_id, target_id} - found)
        if missing:
            raise UnknownEntityError("Unknown reroute endpoint", missing=missing)
    energised = [r for r in q.rows if all(h["status"] in (None, "ENERGIZED") for h in r["hops"])]
    paths = [
        {
            "nodes": r["nodes"],
            "circuits": [f"{h['span_id']}:{h['circuit']}" for h in r["hops"] if h["span_id"]],
            "impedance_pu": round(float(r["impedance_pu"]), 6),
        }
        for r in energised[:k]
    ]
    return {
        "algorithm": "algo.SPpaths",
        "source_id": source_id,
        "target_id": target_id,
        "outcome": "PATHS_FOUND" if paths else "NO_ENERGISED_PATH",
        "candidates_examined": len(q.rows),
        "candidates_exhausted": len(q.rows) < candidates,
        "paths": paths,
        "evidence": [q.evidence()],
        "timing": _timing([q], started),
    }


WCC_CYPHER = """\
CALL algo.WCC({nodeLabels: ['HydroPlant', 'Tower', 'Substation'], relationshipTypes: ['FEEDS', 'SPAN', 'TERMINATES_AT']})
YIELD node, componentId
RETURN componentId, collect(node.id) AS members, sum(CASE WHEN node:HydroPlant THEN 1 ELSE 0 END) AS plants
ORDER BY size(members) DESC"""

ZONES_CYPHER = """\
CALL algo.labelPropagation({nodeLabels: ['HydroPlant', 'Tower', 'Substation'],
                            relationshipTypes: ['FEEDS', 'SPAN', 'TERMINATES_AT'], maxIterations: 30})
YIELD node, communityId
RETURN communityId, collect(node.id) AS members
ORDER BY size(members) DESC"""


def connected_components(client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Topological islands (algo.WCC). Ignores breaker state: it finds parts that are never connected."""
    client = client or get_client()
    started = time.perf_counter()
    q = client.query("wcc", WCC_CYPHER)
    comps = [{"component": r["componentId"], "size": len(r["members"]), "members": sorted(r["members"]),
              "has_generator": r["plants"] > 0} for r in q.rows]
    return {"algorithm": "algo.WCC", "components": comps, "connected": len(comps) <= 1,
            "unsupplied_parts": [c for c in comps if not c["has_generator"]],
            "evidence": [q.evidence()], "timing": _timing([q], started)}


def zones(client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Grid zones by community detection (algo.labelPropagation). Most meaningful on meshed grids."""
    client = client or get_client()
    started = time.perf_counter()
    q = client.query("zones", ZONES_CYPHER)
    found = [{"zone": r["communityId"], "size": len(r["members"]), "members": sorted(r["members"])} for r in q.rows]
    return {"algorithm": "algo.labelPropagation", "zones": found, "evidence": [q.evidence()], "timing": _timing([q], started)}
