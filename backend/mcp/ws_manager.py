"""Bounded broadcasting of the authoritative simulator state; no second model."""
import asyncio
import json
import logging
from backend.simulator.grid_sim import grid_simulator

log = logging.getLogger('gridpulse.telemetry')


class WebSocketTelemetryManager:
    def __init__(self):
        self.active_connections = set()

    def get_current_payload(self):
        return grid_simulator.get_snapshot()

    async def connect(self, websocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        try:
            await asyncio.wait_for(websocket.send_text(json.dumps(self.get_current_payload())), timeout=2)
        except Exception:
            await self.drop(websocket)

    async def disconnect(self, websocket):
        self.active_connections.discard(websocket)

    async def drop(self, websocket):
        """Forget a slow or broken socket and close it, so the browser notices and reconnects."""
        await self.disconnect(websocket)
        try:
            await asyncio.wait_for(websocket.close(code=1011), timeout=1)
        except Exception:
            pass

    async def broadcast(self):
        # Serialise once for every client instead of once per send.
        text = json.dumps(self.get_current_payload())
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=2)
            except Exception:
                await self.drop(ws)
        await asyncio.gather(*(send(ws) for ws in tuple(self.active_connections)))

    async def publish_loop(self):
        # 4Hz publishes transient scenario phases without claiming a 60Hz physics solver.
        while True:
            try:
                await self.broadcast()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad snapshot must not stop telemetry for every client until restart.
                log.exception('telemetry broadcast failed')
            await asyncio.sleep(0.25)


_manager = WebSocketTelemetryManager()


def get_ws_manager():
    return _manager
