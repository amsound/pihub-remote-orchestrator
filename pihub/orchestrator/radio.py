
from __future__ import annotations
import asyncio
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

class RadioDial:
    def __init__(self) -> None:
        self._stations: List[str] = []
        self._index: int = -1
        self._lock = asyncio.Lock()

    async def set_catalog(self, stations: List[str]) -> None:
        async with self._lock:
            self._stations = stations
            if self._index >= len(stations):
                self._index = -1

    async def get_catalog(self) -> List[str]:
        async with self._lock:
            return list(self._stations)

    async def get_index(self) -> int:
        async with self._lock:
            return self._index

    async def set_index(self, idx: int) -> int:
        async with self._lock:
            if not self._stations:
                self._index = -1
                return -1
            self._index = idx % len(self._stations)
            return self._index

    async def next(self) -> int:
        return await self.set_index((await self.get_index() + 1) if await self.get_index() >= 0 else 0)

    async def prev(self) -> int:
        return await self.set_index((await self.get_index() - 1) if await self.get_index() >= 0 else 0)

    async def find_by_name(self, name: str) -> int:
        st = [s.lower() for s in await self.get_catalog()]
        low = name.lower()
        # exact
        for i, s in enumerate(st):
            if s == low:
                return await self.set_index(i)
        # contains
        for i, s in enumerate(st):
            if low in s:
                return await self.set_index(i)
        return -1

    async def next_3am_utc(self, tz_name: str) -> float:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        target = (now + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
        return (target - now).total_seconds()
