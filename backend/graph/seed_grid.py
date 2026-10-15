"""Idempotent seeder for the canonical GridPulse 500 kV topology.

``seed_grid(reset=True)`` is also the graph half of ``POST /api/sim/reset``:
every canonical node and relationship is MERGEd by its key and its dynamic
properties are set back to nominal. With ``prune=True`` it also removes
duplicates and anything with a GridPulse label or relationship type that is
not part of the canonical topology, so repeated seeding never accumulates
state.

CLI (run from ``backend/``)::

    python -m graph.seed_grid            # reset to nominal
    python -m graph.seed_grid --if-empty # seed only when the graph is empty
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import topology as topo
from .client import GraphClient, GraphDataError, GraphQueryError, GraphSettings, get_client

log = logging.getLogger("gridpulse.graph.seed")

_LABEL_PREDICATE = " OR ".join(f"n:{label}" for label in topo.NODE_LABELS)
_EDGE_KEY_EXPR = (
    "type(r) + '|' + a.id + '|' + b.id + '|' + coalesce(r.id, '') + '|' + coalesce(r.circuit, '')"
)
_EDGE_TYPES_LIST = "[" + ", ".join(f"'{t}'" for t in topo.EDGE_TYPES) + "]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ensure_indexes(client: GraphClient) -> None:
    for label in topo.NODE_LABELS + ("GridMeta",):
        try:
            client.query(f"index_{label}", f"CREATE INDEX FOR (n:{label}) ON (n.id)", read_only=False, retries=0)
        except GraphQueryError as exc:
            if "already indexed" not in str(exc):
                raise
    for rel in ("SPAN", "TERMINATES_AT"):
        try:
            client.query(f"index_{rel}", f"CREATE INDEX FOR ()-[r:{rel}]-() ON (r.id)", read_only=False, retries=0)
        except GraphQueryError as exc:
            if "already indexed" not in str(exc):
                raise


def _prune_nodes(client: GraphClient) -> List[Dict[str, Any]]:
    out = []
    out.append(
        client.query(
            "dedupe_nodes",
            f"MATCH (n) WHERE ({_LABEL_PREDICATE}) "
            "WITH n.id AS id, labels(n)[0] AS label, collect(n) AS ns WHERE size(ns) > 1 "
            "UNWIND tail(ns) AS dup DETACH DELETE dup RETURN count(dup) AS removed",
            read_only=False,
        )
    )
    out.append(
        client.query(
            "prune_nodes",
            f"MATCH (n) WHERE ({_LABEL_PREDICATE}) AND NOT n.id IN $ids "
            "DETACH DELETE n RETURN count(n) AS removed",
            {"ids": topo.all_node_ids()},
            read_only=False,
        )
    )
    return [o.evidence() for o in out]


def _prune_edges(client: GraphClient) -> List[Dict[str, Any]]:
    out = []
    out.append(
        client.query(
            "dedupe_edges",
            f"MATCH (a)-[r]->(b) WHERE type(r) IN {_EDGE_TYPES_LIST} "
            f"WITH {_EDGE_KEY_EXPR} AS key, collect(r) AS rs WHERE size(rs) > 1 "
            "UNWIND tail(rs) AS dup DELETE dup RETURN count(dup) AS removed",
            read_only=False,
        )
    )
    out.append(
        client.query(
            "prune_edges",
            f"MATCH (a)-[r]->(b) WHERE type(r) IN {_EDGE_TYPES_LIST} AND NOT ({_EDGE_KEY_EXPR}) IN $keys "
            "DELETE r RETURN count(r) AS removed",
            {"keys": topo.all_edge_keys()},
            read_only=False,
        )
    )
    return [o.evidence() for o in out]


def _set_clause(var: str, reset: bool) -> str:
    return f"SET {var} += row.props" if reset else f"ON CREATE SET {var} += row.props"


def _merge_nodes(client: GraphClient, reset: bool) -> List[Dict[str, Any]]:
    out = []
    for label, nodes in topo.nodes_by_label().items():
        rows = [{"id": n["id"], "props": {k: v for k, v in n.items() if k != "id"}} for n in nodes]
        out.append(
            client.query(
                f"merge_{label}",
                f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) {_set_clause('n', reset)} "
                "RETURN count(n) AS merged",
                {"rows": rows},
                read_only=False,
            ).evidence()
        )
    return out


def _merge_edges(client: GraphClient, reset: bool) -> List[Dict[str, Any]]:
    out = []
    spans = topo.span_rows()
    for rel_type, dst_label in (("SPAN", "Tower"), ("TERMINATES_AT", "Substation")):
        rows = [r for r in spans if r["rel_type"] == rel_type]
        out.append(
            client.query(
                f"merge_{rel_type}",
                f"UNWIND $rows AS row "
                f"MATCH (a:Tower {{id: row.from}}), (b:{dst_label} {{id: row.to}}) "
                f"MERGE (a)-[r:{rel_type} {{id: row.id, circuit: row.circuit}}]->(b) {_set_clause('r', reset)} "
                "RETURN count(r) AS merged",
                {"rows": rows},
                read_only=False,
            ).evidence()
        )
    for rel_type, src_label, dst_label, rows in (
        ("FEEDS", "HydroPlant", "Tower", topo.feeds_rows()),
        ("CONNECTED", "BESS", "Substation", topo.connected_rows()),
        ("DISTRIBUTES_TO", "Substation", "Consumer", topo.distributes_rows()),
    ):
        out.append(
            client.query(
                f"merge_{rel_type}",
                f"UNWIND $rows AS row "
                f"MATCH (a:{src_label} {{id: row.from}}), (b:{dst_label} {{id: row.to}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) {_set_clause('r', reset)} "
                "RETURN count(r) AS merged",
                {"rows": rows},
                read_only=False,
            ).evidence()
        )
    return out


def _bump_meta(client: GraphClient, reset: bool) -> int:
    now = _now()
    on_match = "SET m.state_version = m.state_version + 1, m.last_reset_at = $now" if reset else ""
    outcome = client.query(
        "merge_meta",
        "MERGE (m:GridMeta {id: $id}) "
        "ON CREATE SET m.state_version = 0, m.seeded_at = $now, m.last_reset_at = $now "
        f"{on_match} SET m.schema_version = $schema "
        "RETURN m.state_version AS state_version",
        {"id": topo.META_ID, "now": now, "schema": topo.SCHEMA_VERSION},
        read_only=False,
        retries=0,
    )
    return int(outcome.rows[0]["state_version"])


def count_topology(client: GraphClient) -> Dict[str, int]:
    nodes = client.query(
        "count_nodes", f"MATCH (n) WHERE ({_LABEL_PREDICATE}) RETURN count(n) AS nodes"
    ).rows[0]["nodes"]
    edges = client.query(
        "count_edges",
        f"MATCH (a)-[r]->(b) WHERE type(r) IN {_EDGE_TYPES_LIST} RETURN count(r) AS edges",
    ).rows[0]["edges"]
    return {"nodes": int(nodes), "edges": int(edges)}


def is_seeded(client: Optional[GraphClient] = None) -> bool:
    client = client or get_client()
    counts = count_topology(client)
    return counts["nodes"] > 0


def seed_grid(client: Optional[GraphClient] = None, *, reset: bool = True, prune: bool = True) -> Dict[str, Any]:
    """Create or restore the canonical topology. Safe to call repeatedly.

    ``reset=True`` overwrites every dynamic property with its nominal value and
    bumps ``state_version``. ``reset=False`` only fills in what is missing.
    """
    client = client or get_client()
    evidence: List[Dict[str, Any]] = []
    _ensure_indexes(client)
    if prune:
        evidence += _prune_nodes(client)
    evidence += _merge_nodes(client, reset)
    evidence += _merge_edges(client, reset)
    if prune:
        evidence += _prune_edges(client)
    state_version = _bump_meta(client, reset)
    counts = count_topology(client)
    expected = {"nodes": topo.EXPECTED_NODE_COUNT, "edges": topo.EXPECTED_EDGE_COUNT}
    if counts != expected:
        raise GraphDataError("Topology counts after seeding do not match the canonical topology", found=counts, expected=expected)
    return {
        "graph": client.graph_name,
        "schema_version": topo.SCHEMA_VERSION,
        "reset": reset,
        "state_version": state_version,
        "counts": counts,
        "server_ms": round(sum(e["server_ms"] for e in evidence), 4),
        "queries": [{k: e[k] for k in ("name", "row_count", "server_ms")} for e in evidence],
    }


def reset_grid(client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Restore nominal topology and state. Called by the simulator's reset."""
    return seed_grid(client, reset=True, prune=True)


def ensure_seeded(client: Optional[GraphClient] = None) -> Dict[str, Any]:
    """Startup hook: seed if empty, otherwise leave live state untouched."""
    client = client or get_client()
    if is_seeded(client):
        return seed_grid(client, reset=False, prune=False)
    return seed_grid(client, reset=True, prune=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the GridPulse FalkorDB topology")
    parser.add_argument("--if-empty", action="store_true", help="only seed when the graph has no topology")
    parser.add_argument("--graph", help="graph name (default: $FALKORDB_GRAPH or 'gridpulse')")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = GraphSettings.from_env()
    if args.graph:
        settings = GraphSettings(**{**settings.__dict__, "graph_name": args.graph})
    client = GraphClient(settings)
    try:
        summary = ensure_seeded(client) if args.if_empty else reset_grid(client)
    except Exception as exc:  # report and exit non-zero for scripts/compose
        print(json.dumps({"ok": False, "error": getattr(exc, "to_dict", lambda: str(exc))()}, indent=2))
        return 1
    print(json.dumps({"ok": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
