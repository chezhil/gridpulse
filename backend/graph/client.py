"""FalkorDB connection handling for the GridPulse graph engine.

Every query goes through :meth:`GraphClient.query`, which returns rows as
dicts together with the FalkorDB-reported execution time and the measured
client round trip. Callers never see raw ``redis`` exceptions: connection
problems surface as :class:`GraphUnavailableError` and Cypher failures as
:class:`GraphQueryError`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import redis
except ImportError:
    redis = None
try:
    from falkordb import FalkorDB
except ImportError:
    FalkorDB = None

log = logging.getLogger("gridpulse.graph")


class GraphError(Exception):
    """Base class for graph-engine failures. ``code`` is a stable machine-readable value."""

    code = "GRAPH_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class GraphUnavailableError(GraphError):
    code = "GRAPH_UNAVAILABLE"


class GraphQueryError(GraphError):
    code = "GRAPH_QUERY_FAILED"


class GraphNotSeededError(GraphError):
    code = "GRAPH_NOT_SEEDED"


class UnknownEntityError(GraphError):
    code = "UNKNOWN_ENTITY"


class GraphDataError(GraphError):
    """The graph is reachable but required properties are missing or malformed."""

    code = "GRAPH_DATA_INCOMPLETE"


class InvalidRequestError(GraphError):
    code = "INVALID_REQUEST"


@dataclass(frozen=True)
class GraphSettings:
    host: str = "localhost"
    port: int = 6379
    graph_name: str = "gridpulse"
    username: Optional[str] = None
    password: Optional[str] = None
    connect_attempts: int = 5
    retry_backoff_s: float = 0.5
    query_timeout_ms: int = 2000

    @classmethod
    def from_env(cls) -> "GraphSettings":
        return cls(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            graph_name=os.getenv("FALKORDB_GRAPH", "gridpulse"),
            username=os.getenv("FALKORDB_USERNAME") or None,
            password=os.getenv("FALKORDB_PASSWORD") or None,
            connect_attempts=int(os.getenv("FALKORDB_CONNECT_ATTEMPTS", "5")),
            retry_backoff_s=float(os.getenv("FALKORDB_RETRY_BACKOFF_S", "0.5")),
            query_timeout_ms=int(os.getenv("FALKORDB_QUERY_TIMEOUT_MS", "2000")),
        )


@dataclass
class QueryOutcome:
    """Result of one Cypher execution, with the evidence needed for auditing."""

    name: str
    cypher: str
    params: Dict[str, Any]
    rows: List[Dict[str, Any]]
    server_ms: float
    roundtrip_ms: float
    stats: Dict[str, float] = field(default_factory=dict)

    def evidence(self) -> Dict[str, Any]:
        from .evidence import evidence_hash

        return {
            "name": self.name,
            "cypher": self.cypher,
            "params": self.params,
            "row_count": len(self.rows),
            "rows": self.rows,
            "server_ms": round(self.server_ms, 4),
            "roundtrip_ms": round(self.roundtrip_ms, 4),
            "evidence_hash": evidence_hash(self.cypher, self.params, self.rows),
        }


_CONNECTION_ERRORS = ((redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError)
                      if redis is not None else (OSError,))
_STAT_FIELDS = (
    "nodes_created",
    "nodes_deleted",
    "relationships_created",
    "relationships_deleted",
    "properties_set",
    "properties_removed",
)


class GraphClient:
    """Thread-safe wrapper over one FalkorDB graph."""

    def __init__(self, settings: Optional[GraphSettings] = None) -> None:
        self.settings = settings or GraphSettings.from_env()
        self._db: Optional[FalkorDB] = None
        self._graph = None
        self._lock = threading.Lock()

    @property
    def graph_name(self) -> str:
        return self.settings.graph_name

    def connect(self, attempts: Optional[int] = None) -> None:
        """Connect and PING, retrying with linear backoff a bounded number of times."""
        if FalkorDB is None:
            raise GraphUnavailableError("FalkorDB SDK missing; install backend/requirements.txt",
                                        host=self.settings.host, port=self.settings.port)
        attempts = attempts or self.settings.connect_attempts
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                db = FalkorDB(
                    host=self.settings.host,
                    port=self.settings.port,
                    username=self.settings.username,
                    password=self.settings.password,
                    socket_connect_timeout=1.0,
                    socket_timeout=max(1.0, self.settings.query_timeout_ms / 1000 + 0.5),
                    retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
                )
                db.connection.ping()
                with self._lock:
                    self._db = db
                    self._graph = db.select_graph(self.settings.graph_name)
                log.info("Connected to FalkorDB %s:%s graph=%s", self.settings.host, self.settings.port, self.graph_name)
                return
            except _CONNECTION_ERRORS as exc:
                last_exc = exc
                log.warning("FalkorDB connect attempt %d/%d failed: %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(self.settings.retry_backoff_s * attempt)
        raise GraphUnavailableError(
            f"FalkorDB unreachable at {self.settings.host}:{self.settings.port} after {attempts} attempts",
            host=self.settings.host,
            port=self.settings.port,
            last_error=str(last_exc),
        )

    def ping(self) -> bool:
        try:
            self._ensure_graph(attempts=1)
            self._db.connection.ping()
            return True
        except (GraphUnavailableError, *_CONNECTION_ERRORS):
            return False

    def _ensure_graph(self, attempts: Optional[int] = None):
        if self._graph is None:
            self.connect(attempts)
        return self._graph

    def query(
        self,
        name: str,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        read_only: bool = True,
        retries: int = 1,
    ) -> QueryOutcome:
        """Run one Cypher statement.

        Read-only queries (and writes the caller marks idempotent by passing
        ``retries``) are retried once on a dropped connection; a Cypher error
        is never retried.
        """
        params = dict(params or {})
        last_exc: Optional[BaseException] = None
        for attempt in range(retries + 1):
            # Request-time reconnects fail fast; startup uses connect() with the full retry budget.
            graph = self._ensure_graph(attempts=1 if attempt else 2)
            started = time.perf_counter()
            try:
                if read_only:
                    result = graph.ro_query(cypher, params, timeout=self.settings.query_timeout_ms)
                else:
                    result = graph.query(cypher, params, timeout=self.settings.query_timeout_ms)
            except _CONNECTION_ERRORS as exc:
                last_exc = exc
                with self._lock:
                    self._graph = None
                    self._db = None
                continue
            except redis.exceptions.ResponseError as exc:
                if "empty key" in str(exc):
                    # Read-only queries on a graph key that was never created.
                    raise GraphNotSeededError(
                        f"Graph '{self.graph_name}' does not exist; run the seeder", query=name
                    ) from exc
                raise GraphQueryError(f"{name} failed: {exc}", query=name) from exc
            roundtrip_ms = (time.perf_counter() - started) * 1000.0
            columns = [col[1] for col in result.header]
            rows = [dict(zip(columns, (_plain(v) for v in row))) for row in result.result_set]
            stats = {k: getattr(result, k) for k in _STAT_FIELDS if getattr(result, k, 0)}
            return QueryOutcome(
                name=name,
                cypher=cypher,
                params=params,
                rows=rows,
                server_ms=float(result.run_time_ms),
                roundtrip_ms=roundtrip_ms,
                stats=stats,
            )
        raise GraphUnavailableError(f"{name} failed: FalkorDB connection lost", query=name, last_error=str(last_exc))


def _plain(value: Any) -> Any:
    """Convert FalkorDB result values into JSON-friendly Python values."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "properties") and hasattr(value, "labels"):
        return {"labels": list(value.labels), "properties": _plain(value.properties)}
    return value


_default_client: Optional[GraphClient] = None
_default_lock = threading.Lock()


def get_client() -> GraphClient:
    """Process-wide client configured from environment variables."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = GraphClient()
        return _default_client


def set_client(client: Optional[GraphClient]) -> None:
    """Override the process-wide client (tests, alternate graph names)."""
    global _default_client
    with _default_lock:
        _default_client = client
