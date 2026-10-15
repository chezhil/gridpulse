# GridPulse frontend

The supplied final.html design replaces the previous UI. The scenic corridor is a public illustration; authenticated console operations use the backend simulator.

## Run

Use the repository Docker Compose setup (FalkorDB and FastAPI), then open http://localhost:8000. For a separately served frontend on port 8766, the API defaults to http://127.0.0.1:8000. A deployment may set window.GRIDPULSE_API_BASE before app.js loads.

Demo role buttons create real backend sessions. Operator can trigger/reset scenarios and execute agent plans; engineer and auditor cannot mutate the grid. Credential failures never fall back to demo access. Sessions are stored per browser tab. Backend authentication remains the repository's hackathon implementation.

## Workspaces

- Topology: GET /api/graph/topology and authenticated /ws/telemetry; scenario/reset mutations use bearer tokens.
- Cypher: POST /api/graph/trace-cascade; displays actual query evidence and structured results.
- Agent: POST /api/mcp/agent/advise; the backend needs its configured model credentials. Execution is optional and operator-only.
- Audit: logs, hash-chain verification, and JSON export of retrieved records.

Old page URLs redirect to index.html hash routes. No graph is invented when FalkorDB is unavailable; mutation controls are disabled and a degraded status is displayed. The backend may continue publishing its last simulator snapshot while degraded.

## Assets

Tailwind is compiled locally. From frontend run `npx tailwindcss@3.4.17 -i assets/tailwind.input.css -o assets/utilities.css -c tailwind.config.cjs --minify`, then `python refresh_asset_versions.py`. Google Fonts is optional; local fallback fonts remain available offline.

## Verification

Validated actual backend authentication rejection, demo session, telemetry connection, degraded database handling, audit chain verification, sign-out, and mobile overflow. Contract-based browser checks also passed for authenticated scenario requests, trace evidence, agent responses, and role restrictions; a full successful simulation requires a running FalkorDB instance.
