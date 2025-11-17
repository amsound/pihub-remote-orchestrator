
from __future__ import annotations
import asyncio
import contextlib
from typing import Any, AsyncIterator, Dict, List, Optional

class EventBus:
    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: Dict[str, Any]) -> None:
        # Non-blocking fanout
        async with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # drop oldest to keep fresh
                    try:
                        _ = q.get_nowait()
                        q.put_nowait(event)
                    except Exception:
                        pass

    async def subscribe(self, maxsize: int = 100) -> AsyncIterator[Dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            async with self._lock:
                with contextlib.suppress(ValueError):
                    self._subscribers.remove(q)
