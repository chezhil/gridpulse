"""Measured graph-vs-SQL benchmark for multi-hop downstream traces.

The same synthetic meshed grid (:mod:`graph.synthetic`) is loaded into a
FalkorDB graph and a SQLite database. For each origin bus, three engines
answer "which buses are within 6 hops downstream?":

* ``falkordb_cypher``  ``MATCH (o)-[:LINE*1..6]->(n) RETURN DISTINCT n.id``
* ``falkordb_bfs``     ``CALL algo.BFS(o, 6, 'LINE')`` (GraphBLAS BFS)
* ``sqlite_recursive`` a recursive CTE over an indexed edge table

Every engine's result set is compared before any timing is reported, and
nothing is hardcoded. FalkorDB times are the server-reported execution time
(plus measured round trip); SQLite runs in-process, so its time includes no
network hop. Treat the comparison as indicative for this machine only.

For grids of at most ``PF_MAX_BUSES`` buses the benchmark also runs a full
N-1 scan with LODFs from :mod:`graph.powerflow`.

CLI (from the repository root)::

    python -m backend.graph.benchmark --sizes 1000 10000 100000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np

from .client import GraphClient, GraphNotSeededError, GraphSettings, InvalidRequestError, get_client
from .powerflow import Branch, Network
from .synthetic import SyntheticGrid, benchmark_origins, generate

MAX_HOPS = 6
ALLOWED_SIZES = (1000, 10000, 100000)
PF_MAX_BUSES = 2500
BATCH = 5000

HOPS_RANGE = (2, 16)
CYPHER_VARLEN = "MATCH (o:Bus {{id: $id}})-[:LINE*1..{hops}]->(n) RETURN collect(DISTINCT n.id) AS ids"
CYPHER_BFS = "MATCH (o:Bus {{id: $id}}) CALL algo.BFS(o, {hops}, 'LINE') YIELD nodes RETURN [n IN nodes | n.id] AS ids"
SQL_RECURSIVE = """\
WITH RECURSIVE reach(id, depth) AS (
  SELECT dst, 1 FROM line WHERE src = ?
  UNION
  SELECT l.dst, r.depth + 1 FROM reach r JOIN line l ON l.src = r.id WHERE r.depth < {hops}
)
SELECT DISTINCT id FROM reach"""


def _bench_dir() -> Path:
    default = Path(__file__).resolve().parents[2] / "data" / "bench"
    path = Path(os.getenv("GRIDPULSE_BENCH_DIR", str(default)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _graph_client(base: GraphClient, grid: SyntheticGrid) -> GraphClient:
    settings = GraphSettings(**{**base.settings.__dict__, "graph_name": grid.graph_name})
    return GraphClient(settings)


def _stamp(grid: SyntheticGrid) -> str:
    digest = hashlib.sha256(json.dumps([grid.buses, grid.lines], sort_keys=True).encode()).hexdigest()[:16]
    return f"{grid.size}:{len(grid.lines)}:{grid.seed}:{digest}"


def load_falkordb(client: GraphClient, grid: SyntheticGrid) -> Dict[str, Any]:
    """Create the synthetic graph unless an identical one already exists."""
    stamp = _stamp(grid)
    try:
        current = client.query("bench_stamp", "MATCH (m:BenchMeta) RETURN m.stamp AS stamp").rows
        existing = client.query("bench_count", "MATCH (b:Bus) RETURN count(b) AS n").rows[0]["n"]
    except GraphNotSeededError:
        current, existing = [], 0
    if current and current[0]["stamp"] == stamp:
        return {"reused": True, "load_s": 0.0}
    started = time.perf_counter()
    if existing:
        client.query("bench_drop", "MATCH (n) WHERE n:Bus OR n:BenchMeta DETACH DELETE n", read_only=False, retries=0)
    try:
        client.query("bench_index", "CREATE INDEX FOR (b:Bus) ON (b.id)", read_only=False, retries=0)
    except Exception as exc:  # already indexed
        if "already indexed" not in str(exc):
            raise
    for i in range(0, len(grid.buses), BATCH):
        client.query(
            "bench_buses",
            "UNWIND $rows AS row CREATE (:Bus {id: row.id, row: row.row, col: row.col, load_mw: row.load_mw})",
            {"rows": grid.buses[i : i + BATCH]},
            read_only=False,
            retries=0,
        )
    for i in range(0, len(grid.lines), BATCH):
        client.query(
            "bench_lines",
            "UNWIND $rows AS row MATCH (a:Bus {id: row.src}), (b:Bus {id: row.dst}) "
            "CREATE (a)-[:LINE {id: row.id, impedance_pu: row.impedance_pu, limit_mw: row.limit_mw}]->(b)",
            {"rows": grid.lines[i : i + BATCH]},
            read_only=False,
            retries=0,
        )
    client.query("bench_meta", "MERGE (m:BenchMeta {id: 'BENCH'}) SET m.stamp = $stamp", {"stamp": stamp}, read_only=False)
    return {"reused": False, "load_s": round(time.perf_counter() - started, 3)}


def load_sqlite(grid: SyntheticGrid) -> sqlite3.Connection:
    path = _bench_dir() / f"{grid.graph_name}.sqlite"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    stamp = _stamp(grid)
    row = cur.execute("SELECT v FROM meta WHERE k = 'stamp'").fetchone()
    if row and row[0] == stamp:
        return conn
    cur.executescript(
        "DROP TABLE IF EXISTS bus; DROP TABLE IF EXISTS line;"
        "CREATE TABLE bus (id TEXT PRIMARY KEY, row INT, col INT, load_mw REAL);"
        "CREATE TABLE line (id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, impedance_pu REAL, limit_mw REAL);"
    )
    cur.executemany("INSERT INTO bus VALUES (:id, :row, :col, :load_mw)", grid.buses)
    cur.executemany("INSERT INTO line VALUES (:id, :src, :dst, :impedance_pu, :limit_mw)", grid.lines)
    cur.execute("CREATE INDEX idx_line_src ON line(src)")
    cur.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)", (stamp,))
    conn.commit()
    return conn


def _stats(values: List[float]) -> Dict[str, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered))) - 1))]
    return {"median_ms": round(statistics.median(ordered), 4), "p95_ms": round(p95, 4), "min_ms": round(ordered[0], 4)}


def _n1_lodf(grid: SyntheticGrid, threshold: float = 0.95) -> Dict[str, Any]:
    branches = [Branch(l["id"], l["src"], l["dst"], l["impedance_pu"]) for l in grid.lines]
    limits = {l["id"]: l["limit_mw"] for l in grid.lines}
    buses = [b["id"] for b in grid.buses]
    started = time.perf_counter()
    net = Network(buses, branches, grid.source)
    base = net.flows({b["id"]: -b["load_mw"] for b in grid.buses})
    factor_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    keys, post, bridge_keys = net.n1_flows(base)
    limit_vec = np.array([limits[k] for k in keys])
    over = np.abs(post) > threshold * limit_vec[:, None]
    np.fill_diagonal(over, False)
    over[:, np.isnan(post).all(axis=0)] = False
    insecure = int(over.any(axis=0).sum())
    bridges = len(bridge_keys)
    base_over = sum(abs(base[k]) > threshold * limits[k] for k in keys)
    scan_ms = (time.perf_counter() - started) * 1000.0
    return {
        "buses": len(buses),
        "branches": len(net.branches),
        "factorization_ms": round(factor_ms, 2),
        "n1_scan_ms": round(scan_ms, 2),
        "base_overloads": int(base_over),
        "insecure_outages": insecure,
        "islanding_outages": bridges,
    }


def run_benchmark(
    size: int, runs: int = 20, origins: int = 10, client: Optional[GraphClient] = None, max_hops: int = MAX_HOPS
) -> Dict[str, Any]:
    if size not in ALLOWED_SIZES:
        raise InvalidRequestError(f"size must be one of {list(ALLOWED_SIZES)}", value=size)
    if isinstance(runs, bool) or not isinstance(runs, int) or not 1 <= runs <= 200:
        raise InvalidRequestError("runs must be an integer between 1 and 200", value=runs)
    if isinstance(origins, bool) or not isinstance(origins, int) or not 1 <= origins <= 50:
        raise InvalidRequestError("origins must be an integer between 1 and 50", value=origins)
    if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not HOPS_RANGE[0] <= max_hops <= HOPS_RANGE[1]:
        raise InvalidRequestError(f"max_hops must be an integer between {HOPS_RANGE[0]} and {HOPS_RANGE[1]}", value=max_hops)
    cypher_varlen = CYPHER_VARLEN.format(hops=max_hops)
    cypher_bfs = CYPHER_BFS.format(hops=max_hops)
    sql_recursive = SQL_RECURSIVE.format(hops=max_hops)
    grid = generate(size)
    gclient = _graph_client(client or get_client(), grid)
    load = load_falkordb(gclient, grid)
    conn = load_sqlite(grid)
    starts = benchmark_origins(grid, origins)

    # Correctness first: all engines must agree on every origin.
    reach_sizes = []
    for o in starts:
        a = set(gclient.query("bench_cypher", cypher_varlen, {"id": o}).rows[0]["ids"])
        b = set(gclient.query("bench_bfs", cypher_bfs, {"id": o}).rows[0]["ids"])
        c: Set[str] = {r[0] for r in conn.execute(sql_recursive, (o,))}
        if not (a == b == c):
            raise RuntimeError(f"engines disagree for origin {o}: cypher={len(a)} bfs={len(b)} sql={len(c)}")
        reach_sizes.append(len(a))

    timings: Dict[str, Dict[str, List[float]]] = {
        "falkordb_cypher": {"server": [], "roundtrip": []},
        "falkordb_bfs": {"server": [], "roundtrip": []},
        "sqlite_recursive": {"server": [], "roundtrip": []},
    }
    for i in range(runs):
        o = starts[i % len(starts)]
        for engine, cypher in (("falkordb_cypher", cypher_varlen), ("falkordb_bfs", cypher_bfs)):
            out = gclient.query(f"bench_{engine}", cypher, {"id": o})
            timings[engine]["server"].append(out.server_ms)
            timings[engine]["roundtrip"].append(out.roundtrip_ms)
        started = time.perf_counter()
        conn.execute(sql_recursive, (o,)).fetchall()
        elapsed = (time.perf_counter() - started) * 1000.0
        timings["sqlite_recursive"]["server"].append(elapsed)
        timings["sqlite_recursive"]["roundtrip"].append(elapsed)
    conn.close()

    engines = {
        name: {"engine_time": _stats(t["server"]), "client_roundtrip": _stats(t["roundtrip"])}
        for name, t in timings.items()
    }
    result: Dict[str, Any] = {
        "requested_size": size,
        "grid": {"buses": len(grid.buses), "lines": len(grid.lines), "rows": grid.rows, "cols": grid.cols, "seed": grid.seed},
        "graph_name": grid.graph_name,
        "load": load,
        "max_hops": max_hops,
        "runs": runs,
        "origins": starts,
        "results_verified_equal": True,
        "median_reach_size": statistics.median(reach_sizes),
        "engines": engines,
        "queries": {"falkordb_cypher": cypher_varlen, "falkordb_bfs": cypher_bfs, "sqlite_recursive": sql_recursive},
        "notes": [
            "FalkorDB engine_time is server-reported execution time; client_roundtrip adds the network hop.",
            "SQLite runs in-process; its engine_time and client_roundtrip are the same wall-clock measurement.",
            "Results are machine-specific; every engine's result set was checked for equality first.",
        ],
    }
    if len(grid.buses) <= PF_MAX_BUSES:
        result["n1_lodf"] = _n1_lodf(grid)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="GridPulse graph-vs-SQL trace benchmark")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--hops", type=int, nargs="+", default=[MAX_HOPS])
    args = parser.parse_args(argv)
    for size in args.sizes:
        for hops in args.hops:
            print(json.dumps(run_benchmark(size, runs=args.runs, max_hops=hops), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
