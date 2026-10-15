"""
GridPulse SCADA MCP WebSocket Telemetry Manager
Manages real-time telemetry streaming for `/ws/telemetry`.
Broadcasts state snapshots, scenario transitions, and heartbeat metrics.
"""

import asyncio
import json
import logging
from typing import Set, List, Dict, Any, Optional
from datetime import datetime, timezone
from fastapi import WebSocket, WebSocketDisconnect

from backend.mcp.schemas import (
    TelemetryPayload,
    ScenarioInfo,
    MetricsInfo,
    SpanStatus,
    SPAN_MAPPING,
    SUBSTATION_ID,
    BESS_ID,
    TIER1_HOSPITAL_ID,
    TIER3_BREAKER_ID,
)

logger = logging.getLogger("gridpulse.mcp.ws")


class WebSocketTelemetryManager:
    """
    Thread-safe, resilient WebSocket connection and telemetry broadcast manager.
    Guarantees that a disconnected or misbehaving client never crashes the simulation.
    """

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._current_payload: TelemetryPayload = self._create_nominal_payload()

    def _create_nominal_payload(self) -> TelemetryPayload:
        """Constructs canonical nominal 500kV grid telemetry state."""
        spans = []
        for span_id, (from_t, to_t) in SPAN_MAPPING.items():
            spans.append(
                SpanStatus(
                    id=span_id,
                    from_tower=from_t,
                    to_tower=to_t,
                    upper_status="NOMINAL",
                    lower_status="NOMINAL",
                )
            )

        return TelemetryPayload(
            timestamp=datetime.now(timezone.utc).isoformat(),
            scenario=ScenarioInfo(id=0, name="NOMINAL_GRID_FLOW", status="NOMINAL"),
            metrics=MetricsInfo(
                graphblas_latency_ms=1.4,
                city_load_mw=840.0,
                city_load_pct=100.0,
                bess_injection_mw=0.0,
                tier1_hospital_status="PROTECTED",
            ),
            spans=spans,
            active_cypher="// Nominal 500kV Grid State\nMATCH (h:HydroPlant)-[:FEEDS]->(t:Tower)-[:TERMINATES_AT]->(s:Substation)\nRETURN count(path);",
            agent_log="System Standby: SCADA MCP Agent monitoring 500kV corridor. All spans nominal.",
        )

    def get_current_payload(self) -> TelemetryPayload:
        return self._current_payload

    def update_payload(self, **kwargs) -> TelemetryPayload:
        """Updates internal snapshot and updates timestamp."""
        data = self._current_payload.model_dump(by_alias=True)
        data.update(kwargs)
        data["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._current_payload = TelemetryPayload.model_validate(data)
        return self._current_payload

    async def connect(self, websocket: WebSocket):
        """Accepts new client connection and immediately pushes current grid snapshot."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)

        try:
            # Send initial state snapshot immediately so UI is populated
            payload_dict = self._current_payload.model_dump(by_alias=True)
            await websocket.send_text(json.dumps(payload_dict))
        except Exception as e:
            logger.warning(f"Failed to send initial snapshot to client: {e}")
            await self.disconnect(websocket)

    async def disconnect(self, websocket: WebSocket):
        """Safely removes client from connection pool."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, payload: Optional[TelemetryPayload] = None):
        """
        Broadcasts telemetry to all connected WebSocket clients.
        Evicts disconnected sockets without breaking simulation loop.
        """
        if payload is not None:
            self._current_payload = payload
        else:
            payload = self._current_payload

        # Ensure timestamp is current
        payload_dict = payload.model_dump(by_alias=True)
        payload_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(payload_dict)

        async with self._lock:
            connections = list(self.active_connections)

        dead_connections = []
        for connection in connections:
            try:
                await connection.send_text(serialized)
            except (WebSocketDisconnect, RuntimeError, ConnectionResetError, Exception) as e:
                logger.debug(f"Client disconnected during broadcast: {e}")
                dead_connections.append(connection)

        if dead_connections:
            async with self._lock:
                for dead in dead_connections:
                    if dead in self.active_connections:
                        self.active_connections.remove(dead)

    def reset_to_nominal(self) -> TelemetryPayload:
        """Restores state to nominal baseline."""
        self._current_payload = self._create_nominal_payload()
        return self._current_payload


# Singleton instance
_ws_manager = WebSocketTelemetryManager()


def get_ws_manager() -> WebSocketTelemetryManager:
    return _ws_manager
