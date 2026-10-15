# GridPulse frontend

This frontend contains the landing page, login, and four focused console pages. Development branch: `feat/naman-ui-dashboard`.

## Run locally

On Windows, run `start-preview.cmd` and open `http://127.0.0.1:8766/login.html`; keep the server window open. Alternatively, serve this directory with any static server, for example `python -m http.server 8765`, then open `http://localhost:8765/login.html`. Serve pages from the same origin for shared browser storage. The UI and local simulator work without external CSS; Google Fonts are optional and fall back to installed fonts.

Choose a demo role when the backend is unavailable. The operator can run/reset scenarios and auto-cycle. The engineer can inspect graph traces and neighbors. The auditor has read-only access. Live credentials require the authentication service; rejected logins never silently become demo sessions.

Open the hamburger menu to switch between Grid overview (`dashboard.html`), Scenario lab (`scenarios.html`), Graph diagnostics (`diagnostics.html`), and Action audit (`audit.html`). Each address opens directly. Menu navigation keeps the active session and current in-page scenario state; reloading starts a fresh local scenario while retaining the browser's demo audit history.

All demo actions are simulated. Local audits are stored in the browser and can be exported. They are not an immutable compliance record. Backend latency is displayed only when supplied by live telemetry. An unavailable or disconnected live session pauses controls instead of issuing offline actions. Sign out and select a demo role to enter the local model.

## Backend integration contract to confirm

The adapters target `http://localhost:8000` and `ws://localhost:8000/ws/telemetry`. Endpoint paths match the supplied documents. Request/response shapes below are frontend assumptions because the live backend was unavailable for integration testing.

- `POST /api/auth/login`: JSON `{username, password, role, demo}`. Demo requests omit credentials. Expected response `{access_token, role, expires_in, user:{name}}` (also accepts `token` and a `data` envelope). Role values: `operator`, `engineer`, `auditor`. Backend must enforce permissions independently of the UI.
- REST requests attach `Authorization: Bearer <token>`. WebSocket uses `?token=<encoded token>`; coordinate this handshake with the backend, use WSS outside local development, and avoid logging the token.
- `GET /api/graph/topology`: `{nodes:[{id,name,type,...}],edges:[{id,from,to}]}`. Node `properties` and `labels` are also accepted, as are edge `source`/`target` IDs. Unknown response shapes show an error instead of substituting a fake live graph.
- `POST /api/graph/trace-cascade`: `{origin_node_id,max_hops:6}`. Response supports `cypher` or `query` plus evidence properties.
- `POST /api/sim/trigger-scenario/{1|2|3}` and `POST /api/sim/reset`: empty JSON body. Displayed state comes from telemetry, not the HTTP acknowledgment.
- `GET /api/audit/logs`: an array or `{logs:[...]}` with timestamps and message/action/evidence fields.
- WebSocket snapshots retain the supplied `timestamp`, `scenario`, `metrics`, `spans`, `active_cypher`, and `agent_log` fields. Supported scenario status values include NOMINAL, FAULT_DETECTED, ANALYZING, MITIGATING, MITIGATED, FAILED.
- Backend CORS should allow the chosen static-server origin, for example `http://localhost:8765`.

## Demo model assumptions

The local illustrative topology maps TX_880–TX_887 across the eight tower pairs, and TX_888 from T9 to SUB_METRO. This resolves the supplied nine-span/eight-pair ambiguity only for the demo; live edges always come from the backend. TX_882 remains T3→T4. The extra `CITY_FEEDER_DEMO` aggregates city/residential consumers.

The heatwave begins at 1008 MW (120% of 840 MW). The demo sheds 135 MW of industrial demand, leaving 873 MW and adding 45 MW of battery support. This is a simplified state model, not a validated physical grid solver. Example Cypher is explicitly marked as illustrative and is never represented as executed database evidence.

## Implemented

- Dedicated operator gateway with three demo roles and real credential submission.
- Dedicated responsive command center, interactive topology, two-hop neighbor highlighting, asset readouts, query inspector, scenario controls, auto-cycle, reset/cancellation, audit history/export, and connection status.
- Shared persistent day/night theme (`gridpulse_theme`), sessions (`gridpulse_session`), and operator profile (`gridpulse_operator`).
- Landing CTAs and visual-only lightning/wildfire/heatwave cycling. The existing waterfall, turbine, river confluence, and rounded cards are preserved.

The supplied waterfall preservation instructions reference stream arrays absent from the reconstructed source. The actual current drawing functions were retained rather than replaced with an imagined newer implementation.
