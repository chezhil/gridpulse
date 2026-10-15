"""Bounded broadcasting of the authoritative simulator state; no second model."""
import asyncio
from backend.simulator.grid_sim import grid_simulator


class WebSocketTelemetryManager:
    def __init__(self):
        self.active_connections = set()

    def get_current_payload(self):
        return grid_simulator.get_snapshot()

    async def connect(self, websocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        try:
            await asyncio.wait_for(websocket.send_json(self.get_current_payload()), timeout=2)
        except Exception:
            await self.disconnect(websocket)

    async def disconnect(self, websocket):
        self.active_connections.discard(websocket)

    async def broadcast(self):
        payload = self.get_current_payload()
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=2)
            except Exception:
                await self.disconnect(ws)
        await asyncio.gather(*(send(ws) for ws in tuple(self.active_connections)))

    async def publish_loop(self):
        # 4Hz publishes transient scenario phases without claiming a 60Hz physics solver.
        while True:
            await self.broadcast()
            await asyncio.sleep(0.25)


_manager = WebSocketTelemetryManager()


def get_ws_manager():
    return _manager
