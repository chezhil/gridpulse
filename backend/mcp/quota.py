"""Usage limits for LLM-backed features on a public demo.

Every Groq call path (advisor, AI-operator scenarios, Ask the Grid) checks a
sliding-window budget first, so a public URL with a shared demo login cannot
exhaust the API key's quota. In-memory, single worker (like the rest of the app).

Environment:
  GRIDPULSE_AI_ENABLED          "0" turns all LLM features off (default "1")
  GRIDPULSE_AI_PER_HOUR         site-wide calls per hour (default 60)
  GRIDPULSE_AI_PER_USER_10MIN   calls per operator per 10 minutes (default 10)
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional


class QuotaExceeded(RuntimeError):
    """The LLM budget for this window is used up (or AI is disabled)."""


_lock = threading.Lock()
_site: Deque[float] = deque()
_users: Dict[str, Deque[float]] = {}


def _limit(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def enabled() -> bool:
    return os.environ.get("GRIDPULSE_AI_ENABLED", "1").strip() not in ("0", "false", "no", "off")


def consume(user: Optional[str] = None) -> None:
    """Record one LLM-backed request or raise QuotaExceeded with a user-facing message."""
    if not enabled():
        raise QuotaExceeded("AI features are turned off on this deployment")
    now = time.time()
    per_hour, per_user = _limit("GRIDPULSE_AI_PER_HOUR", 60), _limit("GRIDPULSE_AI_PER_USER_10MIN", 10)
    with _lock:
        while _site and now - _site[0] > 3600:
            _site.popleft()
        if len(_site) >= per_hour:
            wait = int(3600 - (now - _site[0])) + 1
            raise QuotaExceeded(f"Demo AI budget reached ({per_hour}/hour); try again in {wait // 60 + 1} min")
        if user:
            q = _users.setdefault(user, deque())
            while q and now - q[0] > 600:
                q.popleft()
            if len(q) >= per_user:
                wait = int(600 - (now - q[0])) + 1
                raise QuotaExceeded(f"You've used {per_user} AI requests in 10 minutes; try again in {wait // 60 + 1} min")
            q.append(now)
        _site.append(now)


def reset() -> None:
    """Clear all windows (tests)."""
    with _lock:
        _site.clear()
        _users.clear()
