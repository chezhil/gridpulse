# GridPulse

A single-app educational demo of graph-grounded blackout interception: the original animated landing page, demo login, live operator dashboard, FalkorDB topology, deterministic SCADA tools, one simulator state, authenticated telemetry, and persistent SQLite audit records.

## Start the complete app

Requires Docker Desktop running. From this repository:

```sh
docker compose up --build
```

Open **http://localhost:8000**. Choose **Open Console**, then a demo role. The **operator** can trigger scenarios and reset; **engineer** and **auditor** are read-only. Demo access is not production authentication or NERC certification. API documentation is at http://localhost:8000/docs.

Run lightning, inspect the executed Cypher and audit evidence, and reset before running wildfire or heatwave. A duplicate request during an active run returns that run instead of starting a second one. A different concurrent scenario returns 409.

The frontend and backend share port 8000; no separate frontend server is needed. FalkorDB persists in a Docker volume and audit history in `./data/audit_log.db`. Startup restores nominal simulator/graph state; audit history survives restarts and grid reset. Run **one backend worker**.

## Without Docker for the Python process

Start a FalkorDB server on localhost:6379, then:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements-dev.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

See `.env.example` for configuration. Set variables in your shell; the app does not automatically load `.env` outside Docker Compose. Never commit credentials.

If FalkorDB is unavailable, the site and demo login still load, `/health` reports `DEGRADED`, and live graph actions return explicit dependency errors. On the dashboard, **Offline simulation** switches to a clearly labelled browser-only model. Offline traces are illustrative and actions are never queued for later execution. Sign out and log in again to return to the live backend.

## Integration decisions

- All four feature branches are merged on `integration/gridpulse-app`.
- `backend/graph/topology.py` defines canonical IDs and nominal values across modules. `TX_882` is T3→T4; `TX_888` is T9→SUB_METRO. Consumers are reached through `DISTRIBUTES_TO`.
- The graph branch's documented `radial-dc/1` model is used for actual loading projections. The backup circuit rating is 800 MW; nominal demand is 840 MW, with 62 MW of isolated Tier-3 industrial demand. Heatwave multiplies all demand by 1.2 to produce exactly 1008 MW.
- One simulator owns state; the interceptor synchronizes it into FalkorDB before querying or acting. There is no independent MCP grid model or second scenario loop.
- Graph evidence includes executed query, parameters, returned rows, hash, and measured query time. No fixed latency is presented as a measurement.
- SCADA tools query current graph guards and persist intent before actuation; a caller-supplied hash alone never authorizes a trip. Only isolated Tier-3 breakers are eligible. BESS requires SOC above 40%, a positive bounded amount, and LOWER_BACKUP routing.
- All mutating HTTP routes enforce the operator role. WebSocket connections require the login token and stream the same state as `/api/sim/state`.
- SCADA tool adapters are available through `/api/mcp/tools/*` in OpenAPI. These HTTP adapters do not implement an MCP JSON-RPC transport.
- SQLite records intent, results, failed/rejected actions, trace evidence, and post-action Tier-1 verification. Storage is append-only at the application level, not tamper-proof.
- The landing page's original canvas and theme implementation is unchanged.

This is a simplified educational model. It does not provide validated AC power flow, thermal collapse timing, or physical SCADA actuation.

## Verification

```sh
pytest -q
# Require a real FalkorDB connection rather than skipping database checks:
GRIDPULSE_REQUIRE_FALKORDB=1 pytest backend/graph/tests -q
```

`backend/graph/tests` includes real database seeding/query checks plus pure model tests. App and SCADA regression tests explicitly substitute graph fixtures and test HTTP/auth/WebSocket contracts, the actual radial model, all three scenario pipelines, duplicate triggers, cancellation, missing graph evidence, battery guards, and audit persistence. Fixture timings are test inputs, never database measurements.

Integration-session verification: 30 tests passed, 30 live database tests skipped because this restricted session cannot access Docker or install the missing FalkorDB SDK. JavaScript syntax checks passed. Binding a local HTTP port is also blocked in this session, so browser-to-live-database verification must be completed after starting Docker Compose on the host.
