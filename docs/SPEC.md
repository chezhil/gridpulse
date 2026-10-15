# GridPulse — Project Context & Technical Specification
> **Target Audience**: AI Coding Agents, Autonomous Dev Assistants, and Engineering Contributors  
> **Project Status**: Interactive Scrollytelling Landing Page Complete $\rightarrow$ Transitioning to Auth & Live Operator Dashboard  

---

## 1. Executive Summary & Core Vision

**GridPulse** is an **Autonomous Cascading Blackout Interceptor** powered by **FalkorDB** (GraphBLAS sparse matrix engine) and **Model Context Protocol (MCP)** SCADA agents.

### The Problem
When high-voltage transmission corridors ($500\text{ kV}$) suffer a physical fault (lightning strike, wildfire smoke ionization, or extreme heatwave load surges), excess power redistributes across parallel spans in milliseconds. Traditional relational databases require expensive multi-table SQL joins that are too slow to trace 4-to-6-hop cascading thermal overloads before physical relays trip, resulting in catastrophic regional blackouts.

### The GridPulse Solution
1. **1:1 GraphBLAS Adjacency Mapping**: Electrical power grids naturally map to sparse admittance matrices ($Y_{\text{bus}}$). FalkorDB executes multi-hop traversal and linear algebra matrix operations in **$\sim 1.4\text{ ms}$** with zero SQL joins.
2. **Multi-Hop Blast Radius Prediction**: Traverses 4–6 hops ahead of a fault in real time to identify downstream substations and transformers approaching thermal collapse ($90\text{ seconds}$ before physical relay blowout).
3. **Autonomous SCADA MCP Dispatch**: Instead of blind regional load shedding, an MCP-enabled control agent queries the graph for nearby Battery Energy Storage Systems (BESS) within 2 hops, injects local inverter power ($+45\text{ MW}$), and selectively trips only Tier-3 heavy industrial breakers (`BRK_IND_12`).
4. **Deterministic Critical Asset Shielding**: Guarantees $100\%$ uptime for Tier-1 critical infrastructure (e.g., *St. Jude Regional Trauma Center*) and residential suburbs using deterministic Cypher audit trails—zero LLM hallucination in physical breaker actuation.

---

## 2. Visual & Design System Specifications

All UI surfaces across the landing page, authentication flow, and operator dashboard must adhere to the established visual language:

### Typography
- **Display / Headings**: `'Space Grotesk', sans-serif` (Bold `700` / SemiBold `600`, tight tracking `-0.04em` for kinetic statistics).
- **Body / UI Controls**: `'Inter', sans-serif` (`400`, `500`, `600`, `700`).
- **Telemetry / Cypher / Logs**: `'JetBrains Mono', monospace` (`400`, `500`, `600`).

### Color & Atmospheric Theme System
- **Deep Night Mode (`.dark` default)**:
  - Canvas / Background: `#040814` to `#0f172a`
  - Primary Energized Current: `#38bdf8` (Sky 400) / `#00f2fe` (Cyan Glow)
  - BESS Reroute / Healthy Nominal: `#10b981` (Emerald 500) / `#34d399` (Emerald 400)
  - Overload Warning / Thermal Risk: `#f59e0b` (Amber 500) / `#fcd34d` (Amber 300)
  - Fault / Trip / Critical Alert: `#ef4444` (Red 500) / `#f43f5e` (Rose 500)
  - Critical Shielding Accent: `#a855f7` (Purple 500)
  - Night Typography: Frameless, clean floating text (`#f8fafc`) with multi-layered dark drop-shadows (`.dynamic-sky-text`).
- **Golden Morning Day Mode (`html:not(.dark)`)**:
  - Sky Palette: Warm golden horizon (`#fde6c3`, `#f8d4a4`) transitioning to crisp alpine sky (`#70b2e4`, `#1c64b2`).
  - Adaptive Glass Containers (`.day-boxed-content`): Used **exclusively in Day Mode** to guarantee razor-sharp legibility against the bright sky.
    - Background: `rgba(255, 255, 255, 0.52)`
    - Backdrop Filter: `blur(14px)`
    - Border: `1px solid rgba(255, 255, 255, 0.75)`
    - Border Radius: `1.75rem` (uniformly rounded on all 4 corners)
    - Padding: `2.25rem 2.5rem`
  - Adaptive Top Header (`.transparent-header`): Completely transparent in Night Mode; switches to a subtle translucent glass bar (`rgba(255, 255, 255, 0.48)`, `blur(10px)`) in Day Mode.

---

## 3. Completed Milestone: Interactive Scrollytelling Landing Page (`index.html`)

The landing page is a self-contained HTML5 Canvas + Tailwind CSS experience spanning a virtual **`7200px × 1200px` panoramic world** (`WORLD_WIDTH = 7200`, `WORLD_HEIGHT = 1200`) synchronized to a `600vh` virtual scroll runway.

### World Geography & Coordinate Zones
1. **Zone 01 — Alpine Wilderness & Headwaters (`x = 0` to `1450`)**:
   - Towering snow-capped headwaters massif and glacial meltwater basin at `x = 275`.
   - Non-uniform, organic waterfall cascade (`drawAlpineHeadwatersWaterfall`) with 5 undulating water chutes, 19 asynchronous foam streams, and billowing plunge-pool mist.
   - Seamless tributary-to-river confluence (`drawWaterfallRiverConfluence`) where the riverbank opens (`x = 232` to `486`) and 22 asynchronous current threads fan out into the main river.
2. **Zone 02 — $500\text{ kV}$ Transmission Corridor (`x = 520` to `5190`)**:
   - **9 Double-Circuit Waisted Steel Lattice Towers (`T1` through `T9`)**: Complete with 3D side-face struts, K-bracing, porcelain insulator discs, corona discharge glow, and aviation beacons.
   - **Catenary Conductor Spans**: Upper primary circuit and lower BESS backup circuit with animated quadratic Bézier power pulses and a live scrolling current orb that hugs the wire.
   - **Autonomous River Patrol Vessel (`drawMiddleRiverBoat`)**: Cruising continuously on an independent loop (`x = 1650` to `4250`) with V-wake foam, radar mast, and warm water reflections.
3. **Zone 03 — Valley Suburbs & Foothills (`x = 2880` to `5345`)**:
   - 5 depth tiers of procedurally placed swaying spruce trees (`forestTrees`) and mossy granite boulders.
   - 17 transitional human dwellings (`valleyHouses` — alpine cabins $\rightarrow$ village cottages $\rightarrow$ 2–3 story suburban homes) whose windows and chimneys light up as the $500\text{ kV}$ current passes overhead.
4. **Zone 04 — Metropolitan Smart City (`x = 5350` to `7200`)**:
   - **$500\text{ kV}$ Receiving Substation (`SUB_METRO` at `x = 5545`)** and utility-scale **BESS Inverter Containers (`x = 5610`)**.
   - **23 High-Rise Buildings**: 11 background high-rises (`bgCityBuildings`) + 12 foreground curtain-wall skyscrapers (`cityBuildings`) that energize floor-by-floor as the underground feeder current (`y = 994`) surges across the waterfront.
   - **St. Jude Regional Trauma Center (`x = 6304`)**: Tier-1 hospital with emerald roof crown, illuminated medical cross, and protected power status.

### Scroll-Driven Narrative Stages
- **Hero Stage (`0%` Scroll)**: Brand title + `1.4 ms` FalkorDB + GraphBLAS latency callout.
- **Stage 01 (`0.05` – `0.35`)**: *GraphBLAS Topology Engine — Sub-Millisecond Grid Matrix* ($840\text{ MW}$ Y-Bus Sparse Admittance Graph).
- **Stage 02 (`0.30` – `0.62`)**: *Multi-Hop Blast Radius — Tracing Cascades 4–6 Hops Ahead* ($90\text{ sec}$ Early Thermal Collapse Interception).
- **Stage 03 (`0.57` – `0.86`)**: *SCADA MCP & BESS Dispatch — Autonomous Battery & Breaker Control* ($+45\text{ MW}$ Local BESS Inverter Injection).
- **Stage 04 (`0.81` – `1.00`)**: *Critical Asset Shielding — Zero-Guessing Shield for Tier-1 Hospitals* ($100\%$ St. Jude Trauma Center Online).

### Interactive Telemetry & 3 Core Grid Scenarios
The system models three core grid emergency scenarios along the corridor:
1. **Scenario 1 — Lightning Strike on Span TX-882 (`T3` $\rightarrow$ `T4`)**:
   - High-voltage lightning bolt strikes Span 03 (`T3`–`T4`), tripping the upper primary circuit (flashing red) and predicting a 4-hop thermal overload on `SUB_METRO`.
   - Within `1.1s` (simulating `1.8ms` graph execution), the MCP agent opens Tier-3 industrial breaker `BRK_IND_12` and reroutes $+45\text{ MW}$ from BESS onto the lower circuit (turning emerald green).
2. **Scenario 2 — Wildfire Smoke Ionization (Spans `T5`–`T6`)**:
   - Detects plume-induced line-to-ground flashover risk in the alpine forest corridor and proactively de-energizes the affected circuit while balancing load via BESS.
3. **Scenario 3 — 5 PM Extreme Heatwave Demand Surge (`120%` City Load)**:
   - Detects metropolitan HVAC thermal spikes across the 23 city high-rises and sheds non-essential Tier-3 industrial loads to protect St. Jude Trauma Center.

---

## 4. FalkorDB Graph Schema & Cypher Specification

Coding agents working on the backend, simulation engine, or dashboard query builders should use the following property graph schema in **FalkorDB**:

### Node Labels & Properties
- `(:HydroPlant {id: 'HYDRO_ALPINE_01', name: 'Headwaters Pumped Hydro', output_mw: 840.0, voltage_kv: 500})`
- `(:Tower {id: 'T1'..'T9', world_x: Int, elevation_m: Float, status: 'NOMINAL' | 'FAULT' | 'REROUTED', insulator_temp_c: Float})`
- `(:Span {id: 'TX_880'..'TX_888', circuit: 'UPPER_PRIMARY' | 'LOWER_BACKUP', flow_mw: Float, thermal_limit_mw: 900.0, sag_m: Float, status: 'ENERGIZED' | 'TRIPPED'})`
- `(:Substation {id: 'SUB_METRO', city: 'MetroGrid', capacity_mw: 1000.0, current_load_mw: Float, transformer_temp_c: Float, status: 'ONLINE' | 'OVERLOAD_RISK' | 'ISLANDED'})`
- `(:BESS {id: 'BESS_METRO_01', soc_pct: Float, max_discharge_mw: 60.0, active_injection_mw: Float, status: 'STANDBY' | 'DISCHARGING'})`
- `(:Consumer {id: String, name: String, category: 'HOSPITAL' | 'RESIDENTIAL' | 'COMMERCIAL' | 'INDUSTRIAL', priority_tier: 1 | 2 | 3, mw_demand: Float, breaker_id: String, energized: Boolean})`

### Relationship Types
- `(:HydroPlant)-[:FEEDS {mw: Float,kv: 500}]->(:Tower)`
- `(:Tower)-[:SPAN {id: String, circuit: String, impedance_pu: Float, flow_mw: Float, limit_mw: Float}]->(:Tower)`
- `(:Tower)-[:TERMINATES_AT {kv: 500}]->(:Substation)`
- `(:BESS)-[:CONNECTED {inverter_id: String, max_mw: Float}]->(:Substation)`
- `(:Substation)-[:DISTRIBUTES_TO {feeder_id: String, priority_tier: Int}]->(:Consumer)`

### Canonical Cypher Queries
```cypher
// 1. Multi-Hop Cascade Blast Radius Trace (4-6 Hops Ahead)
MATCH path = (t1:Tower {id:'T3'})-[:SPAN*1..6]->(sub:Substation)
WHERE sub.city = 'MetroGrid'
RETURN path, sub.capacity_mw, sub.current_load_mw;

// 2. Selective Tier-3 Industrial Load Shedding Discovery
MATCH (f:Span {id:'TX_882'})-[:FEEDS*1..4]->(load:Consumer)
WHERE load.priority_tier = 3 AND load.energized = true
RETURN load.id, load.name, load.breaker_id, load.mw_demand
ORDER BY load.mw_demand DESC;

// 3. 2-Hop BESS Inverter Discovery & Dispatch Verification
MATCH (sub:Substation {id:'SUB_METRO'})<-[:CONNECTED]-(b:BESS)
WHERE b.soc_pct > 40
RETURN b.id, b.soc_pct, b.max_discharge_mw;

// 4. Tier-1 Critical Asset Protection Audit (St. Jude Hospital)
MATCH (sub:Substation {id:'SUB_METRO'})-[:DISTRIBUTES_TO]->(h:Consumer {priority_tier: 1})
RETURN h.id, h.name, h.energized, sub.status;
```

---

## 5. MCP (Model Context Protocol) SCADA Tool Definitions

The autonomous interceptor agent interacts with the grid exclusively through deterministic MCP tools backed by FalkorDB graph state:

1. `falkordb_trace_blast_radius(origin_node_id: string, max_hops: int = 6)`
   - Executes sparse matrix traversal to return all downstream substations and feeders where projected post-fault load exceeds $95\%$ of `thermal_limit_mw`.
2. `falkordb_find_mitigation_assets(substation_id: string, required_mw: float)`
   - Returns available `(:BESS)` units with `soc_pct > 40` and `(:Consumer {priority_tier: 3})` interruptible industrial loads within 2 hops.
3. `scada_trip_breaker(breaker_id: string, reason_code: string, cypher_audit_hash: string)`
   - Opens a non-essential Tier-3 breaker (e.g., `'BRK_IND_12'`) to shed load. Rejects execution if the target feeder serves any `priority_tier == 1` asset.
4. `scada_dispatch_bess(bess_id: string, inject_mw: float, target_circuit: 'LOWER_BACKUP')`
   - Commands local utility battery inverters to inject real/reactive power onto the backup circuit within milliseconds.

---

## 6. Upcoming Technical Milestones (Next Phase)

The project is now transitioning from the completed visual landing page into the full application stack:

### Milestone A: Operator Authentication & Role-Based Access (`/auth`)
- ** estética & Theme Continuity**: Glassmorphic authentication portal matching the GridPulse dark/day palette (`Space Grotesk` headers, `JetBrains Mono` security badges).
- **Features**:
  - Operator SSO / Credentials login with NERC CIP compliance session badges.
  - Quick "Demo Operator Bypass" mode for instant hackathon/judge evaluation.
  - Smooth transition from the landing page header/CTA into the authenticated operator console.

### Milestone B: Live GridPulse Operator Dashboard (`/dashboard`)
- **Interactive Topology Graph View**:
  - Live node-link and geospatial visualization of the Alpine Hydro Plant, 9 HV Towers (`T1`–`T9`), `SUB_METRO`, `BESS_METRO_01`, Valley Suburbs, and 23 City High-Rises (highlighting *St. Jude Trauma Center*).
- **Real-Time Scenario Simulator & Chaos Controls**:
  - Trigger or auto-cycle the 3 core fault scenarios (Lightning Strike on `TX-882`, Wildfire Ionization on `T5`–`T6`, and 5 PM Heatwave Demand Surge).
  - Step-by-step visual inspection of the GraphBLAS sparse matrix calculation ($1.4\text{ ms}$) vs. legacy SQL join latency ($>450\text{ ms}$).
- **Live Cypher Query Console & MCP Action Audit Log**:
  - Interactive Cypher execution panel showing real-time graph queries and returned paths.
  - Immutable, timestamped SCADA MCP action log proving why specific breakers were tripped and verifying $100\%$ uptime for Tier-1 critical infrastructure.

---

## 7. Guidelines for Coding Agents

- **Do Not Break Existing Visual Polish**: Keep all landing page animations, non-uniform waterfall/river confluence shaders, and `.day-boxed-content` / `.dark` theme behaviors intact.
- **Zero-Hallucination Architecture**: Any AI/agentic feature added to the dashboard must ground its decisions in FalkorDB Cypher query results and execute physical actions through structured MCP tool calls.
- **Modular & Self-Contained**: Ensure new views, components, or API endpoints use the exact node/edge IDs (`T1`–`T9`, `TX_882`, `SUB_METRO`, `BESS_METRO_01`, `BRK_IND_12`) defined in Section 4 so telemetry stays 100% consistent across the entire project.