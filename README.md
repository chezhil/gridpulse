# GridPulse

An educational, single-worker grid interception demo: the animated landing page, demo login, live dashboard, one authoritative simulator, actual FalkorDB Cypher, guarded SCADA actions, authenticated telemetry, persistent SQLite audits, and an MCP stdio bridge.

## Start the complete app

Requires a running Docker Desktop. From the repository root:

```sh
docker compose up --build
```

Open **http://localhost:8000**, choose **Launch Console**, then **Chief Grid Operator**. Run lightning, inspect the Cypher trace and audit evidence, reset, and repeat for wildfire and heatwave. Engineer and auditor are read-only. API documentation: http://localhost:8000/docs.

Both the frontend and API use port 8000. The verified FalkorDB image is pinned by digest. Compose publishes FalkorDB on `127.0.0.1:6379`. If something else already uses 6379, set `FALKORDB_HOST_PORT` in an untracked `.env`, or pass it inline:

```sh
FALKORDB_HOST_PORT=6380 docker compose up --build
```

If this sandbox denies Buildx's default cache directory, use the equivalent verified command:

```sh
mkdir -p work/buildx
BUILDX_CONFIG="$PWD/work/buildx" docker compose up --build
```

The backend always connects to the Compose service `falkordb:6379`; the host port only affects host-side development/tests. Database files persist in the Compose volume; audits persist in `./data/audit_log.db`. Startup restores nominal graph/simulator state and preserves audit history. Run **one backend worker**. Login tokens live in memory: after a backend restart, sign out and log in again. The dashboard reports an expired session instead of retrying it forever.

## Repository-local Python environment

Verified with Python 3.11. Development dependencies belong to this checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
# When using the Compose database on host port 6380:
FALKORDB_PORT=6380 python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Stop the Compose backend first (`docker compose stop backend`) before starting a host backend on the same port. Outside Compose, `.env` is not loaded automatically: export configuration or prefix commands as above. See `.env.example`. Never commit tokens or `.env`.

## Model and runtime behavior

- `backend/graph/topology.py` is the nominal source of IDs and values. `TX_882` connects T3→T4; `TX_884` connects T5→T6; `TX_888` connects T9→SUB_METRO. Consumers connect through `DISTRIBUTES_TO`.
- One simulator owns state. Its updates reach FalkorDB before graph decisions. The deterministic interceptor calls the shared Python guard functions; the external MCP bridge forwards to HTTP adapters in that same running backend.
- The documented `radial-dc/1` projection models parallel circuits and load relief. Nominal demand is 840 MW. Heatwave multiplies every consumer by exactly 1.2: **1008 MW / 120% before mitigation**. Industrial demand is 62 MW nominal / 74.4 MW during heatwave. After shedding and 45 MW BESS support, energized demand is 778 MW for lightning/wildfire or 933.6 MW for heatwave; net substation load is 733 MW or 888.6 MW respectively.
- Cypher evidence contains actual query text, parameters, returned rows, hashes and measured timings. Dashboard latency is the sum of server times for the pre-action trace queries, not physical actuation latency or a fixed 1.4 ms benchmark.
- Every actuation requires current graph guards and durable audit intent before simulator mutation. Only isolated Tier-3 breakers are eligible; a supplied audit hash cannot authorize a hospital trip. BESS requires SOC above 40%, a finite positive amount within discharge/connection limits, and `LOWER_BACKUP`. Dispatch is an **absolute MW setpoint**, so repeating 45 MW does not add another 45 MW.
- Successful actions refresh projected circuit flows. A failure after actuation records the applied result, intent ID and uncertain confirmation, marks the runtime failed, and requires reset. Audit failure before intent prevents actuation.
- A duplicate trigger while a run is active returns the current run. A different concurrent scenario, or another trigger after completion, returns 409 until reset. Reset cancels pending work and preserves audits.
- Mutating control routes require an operator login. All four SCADA tool adapters require a login; read-only roles can query tools but cannot actuate. WebSocket telemetry requires a current login token. No static bypass token is seeded.
- A disconnected WebSocket retries and refreshes authoritative topology/state. Failed initial topology loads also retry. The topology shows the actual faulted span, backup reroute, BESS connection and shed consumer feeder. On narrow screens the diagram scrolls within its panel.
- With an unavailable database, the site/login remain available, `/health` reports `DEGRADED`, and live graph actions fail explicitly. **Offline simulation** is a labelled browser-only model: no database timings and no queued backend actions. Sign out and log in again to return to live mode.

## MCP transport

The supplied project specification explicitly describes Model Context Protocol SCADA tools. No separate hackathon judging rubric was supplied, so an organizer requirement cannot be established from the repository. HTTP routes named `/api/mcp/tools/*` alone are not an MCP transport.

`backend/mcp/transport.py` now supplies genuine **MCP JSON-RPC over stdio**, using the official Python SDK pinned to 1.30.0. An MCP client can initialize, discover schemas and call all four documented tools. The bridge imports no simulator or graph state; it sends requests to the running app, preserving the same roles, graph guards, locking and audits. The dashboard's deterministic interceptor remains an in-process caller of those shared functions; it does not use an LLM or send its internal calls over MCP.

Configure an MCP client's stdio server command as the absolute path to this repository's `.venv/bin/python`, with arguments `-m backend.mcp.transport`, working directory set to this repository, and these environment variables:

```text
GRIDPULSE_API_URL=http://127.0.0.1:8000
GRIDPULSE_MCP_TOKEN=<access_token returned by POST /api/auth/login>
```

A token can be obtained with `POST /api/auth/login` and JSON `{"demo":true,"role":"operator"}`. Keep it out of committed client config. Relogin and replace the token after backend restart. There is **no HTTP Streamable MCP/SSE endpoint**, OAuth flow, or autonomous LLM agent.

## Verified results — 15 October 2026

```sh
# FALKORDB_PORT is the host port of the Compose database (6379 unless overridden):
FALKORDB_PORT=6380 GRIDPULSE_REQUIRE_FALKORDB=1 .venv/bin/python -m pytest -q
# Require real database tests; do not allow connection-based skips:
FALKORDB_PORT=6380 GRIDPULSE_REQUIRE_FALKORDB=1 .venv/bin/python -m pytest backend/graph/tests -q
```

**69 passed, zero skipped** overall. The required graph suite: **42 passed, zero skipped**. Both report one upstream Starlette/httpx TestClient deprecation warning. The graph suite includes pure model tests as well as real Cypher tests. `backend/mcp/test_live_runtime.py` adds seven real-database app/runtime tests with isolated graph names and audit files; it does not substitute graph answers. Other app/SCADA fixture tests remain explicitly labelled as fixtures.

Live database coverage includes repeated seeding, graph traversal, all three complete scenario pipelines, evidence hashes, Tier-1 protection, operator/read-only HTTP and WebSocket behavior, duplicate/conflicting triggers, reset cancellation, insufficient battery capacity, rejected hospital trips, repeatable battery setpoints and projected state synchronization. Partial confirmation and audit-write failures have additional fault-injection regression tests.

Two separately executed acceptance checks are committed:

```sh
# Resets this demo, restarts its backend, and stops/starts its database.
# Requires Docker access; preserves audit history and leaves the grid nominal.
.venv/bin/python tests/verify_running_stack.py

# Requires Node + Playwright Chromium; can be run against the current app.
npm install --prefix work/browser playwright@1.64.1
PLAYWRIGHT_BROWSERS_PATH="$PWD/work/browsers" work/browser/node_modules/.bin/playwright install chromium
NODE_PATH="$PWD/work/browser/node_modules" PLAYWRIGHT_BROWSERS_PATH="$PWD/work/browsers" node tests/browser.cjs
```

The running-stack check passed actual MCP stdio initialize/list/query/dispatch and permission rejections; audit persistence across a real backend process restart; an actual stopped FalkorDB returning 503 without fault injection or actuation; continued landing-page availability; and dependency recovery/reset.

Browser acceptance passed landing→demo login→dashboard→lightning→real graph evidence→mitigation→audit→reset, wildfire, exact 120%/1008 MW heatwave telemetry, both themes, 1440px desktop and 390px narrow layouts, canonical span mapping, live fault/reroute styling, reset during a run, WebSocket reconnect, offline action isolation, and both read-only roles. The check also covers retry after a failed topology load and an expired session. No JavaScript page errors occurred. Verification used headless Chromium in `mcr.microsoft.com/playwright:v1.64.1-noble` because the local macOS browser process was blocked by the sandbox; Safari and Firefox were not tested.

Python compilation, JavaScript syntax and `git diff --check` passed. The landing HTML, its animation script, and shared specification are unchanged from integration commit `093c2ee`.

## Remaining limitations

This is a deterministic educational radial model, not validated AC power flow, thermal protection timing, physical SCADA, production authentication or NERC certification. The preserved landing narrative includes illustrative benchmark/timing claims; these are not results from this verification. Fonts and the landing Tailwind enhancement use external CDNs; existing fallback styles remain in place. SQLite is append-only through application APIs, not tamper-proof, and SQLite/graph/simulator updates are not a distributed transaction. Process restart restores nominal state rather than resuming a run; unresolved crash-time intents require inspection. Demo tokens have no production expiry/revocation service. MCP is a stdio bridge and requires the app plus a valid session. No environment blocker remains for the verified Compose/Chromium workflow.
