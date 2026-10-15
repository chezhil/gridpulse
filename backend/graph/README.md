# backend/graph — FalkorDB topology & query engine

Owner: Chez (`feat/chez-falkordb-engine`, Conventional Commit scope `graph`).

This package seeds the canonical 500 kV GridPulse topology into FalkorDB and answers the questions the interceptor needs: what is downstream of a fault, which circuits are projected to overload, which BESS units and Tier-3 loads can provide relief, and whether Tier-1 assets are protected. Every answer includes the executed Cypher, its parameters and returned rows, FalkorDB's measured execution time, and a `sha256` evidence hash.

## Run it

```bash
docker run -d --name gridpulse-falkordb -p 6379:6379 falkordb/falkordb:latest
pip install "falkordb>=1.8" "fastapi>=0.110" "pydantic>=2.5" pytest httpx
cd backend
python -m graph.seed_grid             # reset to nominal (idempotent)
python -m graph.seed_grid --if-empty  # seed only if no topology exists
cd ..
GRIDPULSE_REQUIRE_FALKORDB=1 python -m pytest backend/graph/tests -q
```

Without FalkorDB, the 30 integration tests are skipped and the 12 model/failure-mode tests still run. Set `GRIDPULSE_REQUIRE_FALKORDB=1` to make a missing database fail the run instead.

Environment variables: `FALKORDB_HOST` (`localhost`), `FALKORDB_PORT` (`6379`), `FALKORDB_GRAPH` (`gridpulse`), `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`, `FALKORDB_CONNECT_ATTEMPTS` (`5`), `FALKORDB_RETRY_BACKOFF_S` (`0.5`), `FALKORDB_QUERY_TIMEOUT_MS` (`2000`).

## Graph representation (decisions needing team sign-off)

The shared spec had three inconsistencies. These are the choices made, all in [`topology.py`](topology.py):

| Ambiguity | Decision |
| --- | --- |
| 9 towers have 8 adjacent pairs, but there are 9 span IDs | `TX_880`..`TX_887` join adjacent towers (`TX_880` = T1→T2, **`TX_882` = T3→T4**, `TX_887` = T8→T9). `TX_888` is the terminal segment **T9→SUB_METRO**. |
| The spec defines both a `:Span` node and a `[:SPAN]` relationship; its cascade query follows `SPAN` into a Substation, but the schema says `TERMINATES_AT` | Spans are **relationships only**. Each span ID is one corridor segment with two parallel circuits, stored as two relationships keyed by `(id, circuit)`: `UPPER_PRIMARY` and `LOWER_BACKUP`. `TX_880`–`TX_887` are `[:SPAN]`; `TX_888` uses `[:TERMINATES_AT]` with the same properties. Traversals use `[:FEEDS\|SPAN\|TERMINATES_AT]`. |
| The example `Span-[:FEEDS]->Consumer` path does not exist in the schema | Loads are reached via `(:Substation)-[:DISTRIBUTES_TO {feeder_id}]->(:Consumer)`. |

```
(HYDRO_ALPINE_01)-[:FEEDS]->(T1)=[TX_880]=>(T2)= ... =>(T9)=[TX_888 :TERMINATES_AT]=>(SUB_METRO)
                                    (BESS_METRO_01)-[:CONNECTED]->(SUB_METRO)-[:DISTRIBUTES_TO]->(4 consumers)
```

Circuit properties: `id, circuit, impedance_pu, limit_mw, flow_mw, sag_m, status (ENERGIZED|TRIPPED), kv`.

**Proposed IDs** (not in the shared spec; please confirm or rename *before* other modules hard-code them):

| ID | What |
| --- | --- |
| `RES_VALLEY_SUBURBS` | Tier-2 residential, 210 MW, breaker `BRK_RES_04`, feeder `FDR_RES_04` |
| `COM_METRO_CORE` | Tier-2 commercial (23 high-rises), 538 MW, `BRK_COM_07`, `FDR_COM_07` |
| `IND_METRO_12` | Tier-3 industrial, 62 MW, on the canonical **`BRK_IND_12`**, `FDR_IND_12` |
| `BRK_HOSP_01`, `FDR_MED_01` | Breaker/feeder for `HOSP_ST_JUDE` (Tier 1, 30 MW) |
| `INV_METRO_01` | Inverter on the `BESS_METRO_01`→`SUB_METRO` connection |

Nominal values: total demand 840 MW, matching the hydro output. `SUB_METRO` capacity is 1000 MW. `BESS_METRO_01` is at 78% SOC with 60 MW maximum discharge. **The `LOWER_BACKUP` circuit is rated 800 MW** while `UPPER_PRIMARY` is rated 900 MW (the spec only gave 900). With both rated 900 MW, losing the primary would put 840 MW on a 900 MW circuit (93%), which is not an overload, so the lightning scenario would not be one either. Tower `world_x` values are spread evenly over x=520–5190. Replace them with the real coordinates from `index.html` if those differ.

## Overload model (`radial-dc/1`)

Reachability and overload are separate results. Being downstream of a fault is not an overload claim. Overloads come only from [`thermal.py`](thermal.py), a pure function of the graph snapshot:

- Each segment carries the net demand of every energised substation downstream of it. Net demand is energised consumer `mw_demand` minus BESS `active_injection_mw`.
- Parallel energised circuits share that flow in proportion to `1/impedance_pu`. A tripped circuit carries 0.
- A segment with no energised circuit islands everything downstream of it.
- An element is at risk when its loading exceeds `threshold_pct` (default 95) of `limit_mw` / `capacity_mw`. Relief is the reduction in downstream net demand needed to get back under the threshold.
- The model is lossless and steady-state, and treats the source as a slack bus. Thermal time constants and relay timing (the "90 s" narrative) are **not** modelled.

What the model produces for the three scenarios, all covered by tests:

| State | Result |
| --- | --- |
| Nominal | Primary 504 MW (56%), backup 336 MW (42%), SUB_METRO 84%: within limits |
| Lightning: `TX_882` `UPPER_PRIMARY` tripped | `TX_882:LOWER_BACKUP` carries 840 MW = **105%**, relief needed **80 MW** |
| + 45 MW BESS | 795 MW = 99.4%, still overloaded (relief 35 MW) |
| + 45 MW BESS + shed `BRK_IND_12` (62 MW) | 733 MW = 91.6%: within limits |
| Heatwave: every consumer ×1.2 | SUB_METRO 1008/1000 MW = 100.8%, relief 58 MW. Lines stay within limits. |
| Wildfire: trip one `TX_884` (T5→T6) circuit | Same result as lightning, on `TX_884`. Tripping both circuits islands SUB_METRO. |

## HTTP API (mount: `app.include_router(graph.router)`)

### `GET /api/graph/topology`

Returns `{graph, schema_version, state_version, counts, nodes[], edges[], spans[], evidence[], timing}`.
- `nodes[]` = `{id, label, properties}`
- `edges[]` = `{id, type, source, target, properties}`. Span edges have ids like `TX_882:UPPER_PRIMARY`.
- `spans[]` = `{id, from, to, circuits: {UPPER_PRIMARY: {status, flow_mw, limit_mw, impedance_pu, sag_m}, LOWER_BACKUP: {...}}}` — the shape the frontend needs for drawing.

### `POST /api/graph/trace-cascade`

```json
{"origin_id": "TX_882", "max_hops": 6, "threshold_pct": 95,
 "assume_tripped": [{"span_id": "TX_882", "circuit": "UPPER_PRIMARY"}]}
```

Request fields:
- `origin_id` (alias `origin_node_id`): a span ID or a node ID. For a span, the traversal starts at its downstream end (hop 0 = `T4` for `TX_882`). `SUB_METRO` is hop 6 and its consumers/BESS are hop 7. Default `max_hops` is 6, maximum 12.
- `assume_tripped` is optional. It is a what-if that is layered onto the stored state for the projection and is **never written**. Leave it empty to project the actual synced state.

Real response from this branch, trimmed:

```json
{
  "origin": {"id": "TX_882", "kind": "SPAN", "from": "T3", "to": "T4", "start_node_id": "T4",
             "circuits": [{"circuit": "UPPER_PRIMARY", "status": "ENERGIZED", "rel_type": "SPAN"}, "..."]},
  "state_version": 1,
  "outcome": "IMPACT_FOUND",
  "summary": {"downstream_substations": ["SUB_METRO"],
              "downstream_consumer_ids": ["HOSP_ST_JUDE", "RES_VALLEY_SUBURBS", "COM_METRO_CORE", "IND_METRO_12"],
              "furthest_hop": 6, "projection_outcome": "OVERLOAD_PREDICTED",
              "required_relief_mw": 80.0, "tier1_at_risk": ["HOSP_ST_JUDE"]},
  "reachability": {"max_hops": 6, "beyond_max_hops": [],
                   "nodes": [{"id": "SUB_METRO", "label": "Substation", "hops": 6,
                              "path": ["T4", "T5", "T6", "T7", "T8", "T9", "SUB_METRO"], "status": "ONLINE"}, "..."]},
  "impact": {"substations": [{"id": "SUB_METRO", "projected_status": "OVERLOAD_RISK",
                              "overloaded_supply_circuits": ["TX_882:LOWER_BACKUP"], "...": "..."}],
             "consumers": [{"id": "IND_METRO_12", "priority_tier": 3, "breaker_id": "BRK_IND_12", "risk": "OVERLOAD_RISK", "...": "..."}],
             "bess": [{"id": "BESS_METRO_01", "soc_pct": 78.0, "max_discharge_mw": 60.0, "...": "..."}]},
  "projection": {"model": "radial-dc/1", "outcome": "OVERLOAD_PREDICTED", "required_relief_mw": 80.0,
                 "overloads": [{"element": "TX_882:LOWER_BACKUP", "kind": "CIRCUIT", "loading_pct": 105.0, "relief_mw": 80.0}],
                 "circuits": ["...18 rows..."], "substations": ["..."], "islanded_substations": [], "assumptions": ["..."]},
  "evidence": [{"name": "trace_reachability", "cypher": "MATCH (o) WHERE o.id = $start_id ...", "params": {"start_id": "T4"},
                "row_count": 6, "rows": ["..."], "server_ms": 0.6739, "roundtrip_ms": 1.7478,
                "evidence_hash": "sha256:e483ca49..."}, "..."],
  "timing": {"graph_server_ms": 2.2873, "graph_roundtrip_ms": 8.6352, "engine_total_ms": 9.2243, "query_count": 4}
}
```

`outcome` values:
- `outcome`: `IMPACT_FOUND` | `NO_DOWNSTREAM_ASSETS`
- `projection.outcome`: `WITHIN_LIMITS` | `OVERLOAD_PREDICTED` | `ISLANDING_PREDICTED`

### `GET /api/graph/health` (additive, for readiness)

Returns `{graph, falkordb: bool, seeded: bool, state_version}`. It never raises.

### Errors

Errors use FastAPI's `{"detail": {"code", "message", "details"}}` envelope:

| Code | Status | Meaning |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | Bad id, hop bound, circuit or threshold. Pydantic's own 422 can also appear. |
| `UNKNOWN_ENTITY` | 404 | The origin or substation is not in the graph. |
| `GRAPH_NOT_SEEDED` | 503 | FalkorDB is reachable but the topology is missing. |
| `GRAPH_UNAVAILABLE` | 503 | FalkorDB is still unreachable after bounded retries. |
| `GRAPH_QUERY_FAILED` | 502 | FalkorDB rejected or failed a query. |
| `GRAPH_DATA_INCOMPLETE` | 500 | Required properties are missing from the graph state. |

## Python interfaces (for the MCP agent and simulator)

```python
import graph

graph.init_graph(reset: bool = False) -> dict              # startup: connect (5 attempts, linear backoff) + seed if empty
graph.reset_grid() -> dict                                 # nominal topology+state, prunes strays/dupes, bumps state_version
graph.apply_state_update(update: dict) -> {"state_version", "applied", "server_ms"}
graph.trace_cascade(origin_id, max_hops=6, assume_tripped=None, threshold_pct=95.0) -> dict   # as HTTP above
graph.find_mitigation_assets(substation_id, required_mw, min_soc_pct=40.0) -> dict
graph.check_breaker(breaker_id) -> dict
graph.verify_tier1_protection(substation_id=None) -> dict
graph.read_grid_snapshot() -> (snapshot, QueryOutcome); graph.project_loading(snapshot, assume_tripped, threshold_pct)
graph.verify_evidence(evidence_record) -> bool             # recompute sha256 over cypher+params+rows
```

All of them accept `client=` for a non-default `GraphClient`, and they raise the `GraphError` subclasses listed above.

- **`find_mitigation_assets("SUB_METRO", 80)`** returns:
  - `bess.eligible` — e.g. `BESS_METRO_01` with `available_mw` = min(max discharge, connection max) − active injection.
  - `bess.ineligible` — with reasons such as `SOC_AT_OR_BELOW_MINIMUM`, `NO_DISCHARGE_HEADROOM`, `STATUS_OFFLINE` or `MISSING_*`.
  - `sheddable_loads.eligible` — only Tier-3, energised loads with a breaker and no shared breaker or feeder with a Tier-1/2 load. Today that is `IND_METRO_12` / `BRK_IND_12`, 62 MW.
  - `sheddable_loads.excluded` — with reasons.
  - `totals`, `meets_requirement` and `evidence`.

  It discovers options only; it does not choose an action.
- **`check_breaker("BRK_IND_12")`** returns `{trip_allowed: true, shed_mw: 62.0, reasons: []}`. `BRK_HOSP_01` returns `trip_allowed: false, reasons: ["SERVES_TIER_1"]`. An unknown breaker returns `["UNKNOWN_BREAKER"]`, and an open breaker returns `["ALREADY_OPEN"]`. `scada_trip_breaker` should refuse unless `trip_allowed` is true.
- **`verify_tier1_protection()`** returns an `outcome` of `ALL_PROTECTED` | `TIER1_AT_RISK` | `TIER1_DEENERGIZED` | `NO_TIER1_ASSETS`. Each asset carries `protected` (energised and the supply path is energised) and `at_risk` (projection).

### How simulator state reaches the graph

1. The simulator is the only physics owner. After each state transition it calls `graph.apply_state_update({...})`, and the interceptor runs only after that call returns. Writable fields:
   - `spans[{span_id, circuit, status, flow_mw, sag_m}]`
   - `towers[{id, status, insulator_temp_c}]`
   - `substations[{id, status, current_load_mw, transformer_temp_c}]`
   - `bess[{id, status, soc_pct, active_injection_mw}]`
   - `consumers[{id, energized, mw_demand}]`
   - `hydro[{id, output_mw}]`
   - `source` (a tag stored on the graph)
2. The entire update is validated before any write: fields, types, enums, ranges, that the IDs exist with the right label, and that BESS injection stays within `max_discharge_mw`. A bad update raises and writes nothing.
3. Each accepted update bumps `state_version`. `trace_cascade` and `verify_tier1_protection` read their state in **one** query and return that `state_version`, so an audit record can cite the exact state a decision used.
4. Writes are one query per entity type and are **not** one transaction. Hold the simulator's single state lock across *sync → trace → actuate → sync*.
5. Reset: `POST /api/sim/reset` should cancel scenario tasks first, then call `graph.reset_grid()`. Reset restores every nominal value, removes stray or duplicate nodes and edges, and bumps `state_version`, so a stale task can detect that the run changed.

## Integration handoff

**Jyothi (`backend/main.py`, `requirements.txt`, compose):**

```python
from contextlib import asynccontextmanager
import graph  # run uvicorn from backend/ (or use `from backend import graph` with backend on sys.path)

@asynccontextmanager
async def lifespan(app):
    graph.init_graph(reset=False)   # raises GraphUnavailableError after bounded retries
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(graph.router)    # /api/graph/topology, /api/graph/trace-cascade, /api/graph/health
```

- Add these to `requirements.txt`: `falkordb>=1.8,<2`, `fastapi>=0.110`, `pydantic>=2.5`, plus `pytest` and `httpx` for tests. This branch was tested with falkordb 1.8.0, fastapi 0.143.1, pydantic 2.14.1, redis 8.1.0 and Python 3.11.
- Add a compose service for `falkordb/falkordb:latest` on 6379, and set `FALKORDB_HOST=falkordb` for the API container.
- `.env.example` lines: `FALKORDB_HOST=localhost`, `FALKORDB_PORT=6379`, `FALKORDB_GRAPH=gridpulse`.
- Call `graph.reset_grid()` from `/api/sim/reset`.

**Nishitha (`backend/mcp`):**

| MCP tool | Graph function |
| --- | --- |
| `falkordb_trace_blast_radius` | `graph.trace_cascade(origin_id, max_hops)` |
| `falkordb_find_mitigation_assets` | `graph.find_mitigation_assets(substation_id, required_mw)` |
| `scada_trip_breaker` guard | `graph.check_breaker(breaker_id)` |

- Use `summary.required_relief_mw` as `required_mw`.
- For the audit hash, use the `evidence[].evidence_hash` values, or recompute them with `graph.verify_evidence`.
- For telemetry `graphblas_latency_ms`, use `timing.graph_server_ms`. That value is FalkorDB's own reported execution time, summed over the trace's queries. It is not a constant.

**Naman (`frontend`):**
- Draw from `spans[]` in `/api/graph/topology`.
- The Cypher inspector can show `evidence[].cypher`, `params`, `row_count` and `server_ms` from the trace.

## Measured timings (this branch, local Docker on macOS, 50 warm runs)

These are for the lightning trace with `assume_tripped`. `graph_server_ms` is FalkorDB's reported execution time summed over the trace's 4 queries:

| | median | p95 |
| --- | --- | --- |
| `graph_server_ms` (4 queries) | 1.43 ms | 1.91 ms |
| `trace_reachability` alone | 0.63 ms | 0.95 ms |
| `graph_roundtrip_ms` (client-measured) | 4.97 ms | 6.34 ms |
| `engine_total_ms` (incl. model) | 5.27 ms | 6.73 ms |

A full reset/seed takes about 3.4 ms of FalkorDB server time. Expect different numbers on other machines.

## Open contract questions

1. Confirm the span mapping (`TX_888` = T9→SUB_METRO) and the proposed consumer, breaker, feeder and inverter IDs above.
2. Confirm the 800 MW `LOWER_BACKUP` rating, or choose a different way to make lightning an overload.
3. Status vocabulary: the graph uses `ENERGIZED`/`TRIPPED` per circuit, while telemetry uses `NOMINAL`/`FAULT_TRIPPED`/`REROUTED_BESS`. Nishitha should own that mapping.
4. Auth on `/api/graph/*`: both routes are read-only and currently open. Add Jyothi's dependency at mount time if needed: `app.include_router(graph.router, dependencies=[...])`.
5. `GRIDPULSE_CONTEXT.md` and `GITHUB_RULES.md` are referenced by the team prompts but are not in the repo yet. The spec currently lives at `docs/SPEC.md`.
