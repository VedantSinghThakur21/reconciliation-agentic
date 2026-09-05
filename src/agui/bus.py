"""Optional in-process progress bus for AG-UI (fail-open, best-effort)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

_lock = threading.Lock()
_buffers: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=200))


def publish(kickoff_id: str, event: dict[str, Any]) -> None:
    kid = (kickoff_id or "").strip()
    if not kid:
        return
    with _lock:
        _buffers[kid].append({**event, "_published_at": time.time()})


def drain(kickoff_id: str, *, after: float = 0.0) -> list[dict[str, Any]]:
    kid = (kickoff_id or "").strip()
    with _lock:
        items = list(_buffers.get(kid, ()))
    return [e for e in items if float(e.get("_published_at") or 0) > after]
