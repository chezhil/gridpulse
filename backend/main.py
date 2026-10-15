"""
GridPulse Unified FastAPI Entrypoint & Runtime Orchestrator.
Mounts Auth, Simulator, Audit, Graph (Chez), and MCP/WebSocket (Nishitha) modules.
Configures CORS, dependency lifecycles, and resilient WebSocket broadcasting.
"""

import asyncio
import logging
import json
from contextlib import asynccontextmanager
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Core Owned Modules
from backend.auth.router import auth_router
from backend.simulator.router import sim_router
from backend.simulator.grid_sim import grid_simulator
from backend.audit.logger import audit_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gridpulse.main")

# Active WebSocket connections
active_connections: Set[WebSocket] = set()


async def broadcast_telemetry(payload: dict):
    """Broadcasts a telemetry payload to all active WebSocket clients."""
    if not active_connections:
        return
    message = json.dumps(payload)
    disconnected = set()
    for ws in list(active_connections):
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    for dead_ws in disconnected:
        active_connections.discard(dead_ws)


async def telemetry_broadcaster_loop():
    """Background periodic loop (1Hz) ensuring continuous live telemetry stream."""
    while True:
        try:
            snapshot = grid_simulator.get_snapshot()
            await broadcast_telemetry(snapshot)
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in telemetry broadcast loop: {e}")
            await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup & shutdown lifecycle manager."""
    logger.info("Initializing GridPulse SCADA Runtime Environment...")
    broadcaster_task = asyncio.create_task(telemetry_broadcaster_loop())
    yield
    logger.info("Shutting down GridPulse Runtime Environment...")
    broadcaster_task.cancel()
    try:
        await broadcaster_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="GridPulse — Autonomous Cascading Blackout Interceptor",
    description="FalkorDB GraphBLAS & SCADA MCP Telemetry Engine",
    version="1.0.0",
    lifespan=lifespan
)

# CORS Configuration for Local Operator Dashboard & Landing Page
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Core Routers
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(sim_router, prefix="/api/sim", tags=["simulator"])
app.include_router(audit_router, prefix="/api/audit", tags=["audit"])

# Mount Graph Router (Integration with Chez's module or graceful placeholder)
try:
    from backend.graph.router import graph_router
    app.include_router(graph_router, prefix="/api/graph", tags=["graph"])
    logger.info("Successfully mounted FalkorDB Graph router from backend.graph.router")
except ImportError:
    # Graceful integration fallback for hackathon development before branch merge
    from fastapi import APIRouter
    graph_router = APIRouter()

    @graph_router.get("/topology")
    def get_topology():
        return {
            "source": "SIMULATOR_ADAPTER",
            "towers": grid_simulator._towers,
            "spans": grid_simulator._spans,
            "substation": grid_simulator._substation,
            "bess": grid_simulator._bess,
            "consumers": grid_simulator._consumers,
            "status": "Awaiting Chez FalkorDB router merge or running in embedded mode"
        }

    @graph_router.post("/trace-cascade")
    def trace_cascade():
        return {
            "origin": "TX_882",
            "blast_radius_hops": 4,
            "graphblas_latency_ms": 1.4,
            "overloaded_substations": ["SUB_METRO"],
            "cypher_executed": "MATCH path = (t1:Tower {id:'T3'})-[:SPAN*1..6]->(sub:Substation) RETURN path;"
        }

    app.include_router(graph_router, prefix="/api/graph", tags=["graph"])
    logger.info("Mounted fallback graph router (backend.graph.router not yet imported)")


# Mount WebSocket Telemetry Route (/ws/telemetry)
@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    """
    ws://localhost:8000/ws/telemetry
    Primary telemetry feed for Naman's SCADA Dashboard and Landing Page.
    """
    await websocket.accept()
    active_connections.add(websocket)
    try:
        # Immediately push current snapshot upon connection
        await websocket.send_text(json.dumps(grid_simulator.get_snapshot()))
        # Listen for any client keep-alive pings or messages
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.discard(websocket)
    except Exception as e:
        logger.warning(f"WebSocket client error: {e}")
        active_connections.discard(websocket)


@app.get("/")
def root():
    return {
        "service": "GridPulse SCADA Runtime",
        "status": "ONLINE",
        "docs_url": "/docs",
        "health": "/health"
    }


@app.get("/health")
def health():
    return {
        "status": "HEALTHY",
        "active_ws_clients": len(active_connections),
        "run_id": grid_simulator._run_id,
        "grid_status": grid_simulator._scenario_status
    }
