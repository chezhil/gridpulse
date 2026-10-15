"""Unified single-worker GridPulse app, including the preserved frontend."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import graph
from backend.auth.router import auth_router
from backend.audit.logger import audit_router
from backend.simulator.grid_sim import grid_simulator as sim
from backend.simulator.router import sim_router
from backend.mcp.router import router as mcp_router
from backend.mcp.ws_manager import get_ws_manager

log = logging.getLogger('gridpulse')
FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'


@asynccontextmanager
async def lifespan(app):
    try:
        # Each process starts with one nominal simulator; restore the graph to match.
        await asyncio.to_thread(graph.init_graph, reset=True)
        sim._agent_log = 'FalkorDB ready. Educational simulator at nominal state.'
    except graph.GraphError as exc:
        log.warning('Graph unavailable at startup: %s', exc)
        sim._agent_log = f'Graph unavailable: {exc}. Live actions fail closed; offline UI simulation remains available.'
    task = asyncio.create_task(get_ws_manager().publish_loop())
    try:
        yield
    finally:
        await sim._cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='GridPulse', version='1.0.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:8000', 'http://127.0.0.1:8000',
    'http://localhost:5500', 'http://127.0.0.1:5500', 'http://localhost:8080', 'http://127.0.0.1:8080'],
    allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type', 'X-Operator-Token'])
app.include_router(auth_router, prefix='/api/auth', tags=['auth'])
app.include_router(audit_router, prefix='/api/audit', tags=['audit'])
app.include_router(sim_router, prefix='/api/sim', tags=['simulator'])
app.include_router(graph.router)
app.include_router(mcp_router)


@app.exception_handler(graph.GraphError)
async def graph_failure(request, exc):
    from backend.graph.router import _STATUS
    return JSONResponse(status_code=_STATUS.get(type(exc), 503), content={'detail': exc.to_dict()})


@app.get('/health')
def health():
    return {'status': 'HEALTHY' if graph.get_client().ping() else 'DEGRADED',
            'run_id': sim._run_id, 'grid_status': sim._scenario_status,
            'active_ws_clients': len(get_ws_manager().active_connections)}


@app.get('/')
def landing():
    return FileResponse(FRONTEND / 'index.html')


# Mount last so APIs and docs take precedence over static content.
app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')
