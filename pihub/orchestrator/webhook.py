
from __future__ import annotations
import httpx
import asyncio
from typing import Optional

class WebhookClient:
    def __init__(self, url: Optional[str]) -> None:
        self._url = url

    async def post_activity(self, room: str, activity: str, ts: str) -> None:
        if not self._url:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as cli:
                await cli.post(self._url, json={"room": room, "activity": activity, "ts": ts})
        except Exception:
            # best-effort; log at caller
            pass
