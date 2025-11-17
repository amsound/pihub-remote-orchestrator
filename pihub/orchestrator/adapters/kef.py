
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Literal, Optional, Callable, Awaitable

# Minimal async KEF adapter with pluggable backend.
# Default backend is a 'Sim' that keeps state in-memory.
# Replace Sim with a real implementation when wiring hardware.

Source = Literal["Opt","Wifi"]

@dataclass
class KEFSnapshot:
    source: Source
    volume: int
    mute: bool

class KEFBackend:
    async def get_snapshot(self) -> KEFSnapshot: ...
    async def set_source(self, source: Source) -> None: ...
    async def set_volume(self, volume: int) -> None: ...
    async def change_volume(self, delta: int) -> None: ...
    async def set_mute(self, mute: bool) -> None: ...
    async def media(self, command: str) -> None: ...  # only meaningful on Wifi

class SimKEF(KEFBackend):
    def __init__(self) -> None:
        self._source: Source = "Opt"
        self._volume: int = 20
        self._mute: bool = False
        self._lock = asyncio.Lock()

    async def get_snapshot(self) -> KEFSnapshot:
        async with self._lock:
            return KEFSnapshot(self._source, self._volume, self._mute)

    async def set_source(self, source: Source) -> None:
        async with self._lock:
            self._source = source

    async def set_volume(self, volume: int) -> None:
        async with self._lock:
            self._volume = max(0, min(100, int(volume)))

    async def change_volume(self, delta: int) -> None:
        await self.set_volume((await self.get_snapshot()).volume + delta)

    async def set_mute(self, mute: bool) -> None:
        async with self._lock:
            self._mute = bool(mute)

    async def media(self, command: str) -> None:
        # No-op in sim; real impl would send transport controls when Wifi
        return

class KEFAdapter:
    def __init__(self, host: str | None = None, backend: Optional[KEFBackend] = None) -> None:
        if backend:
            self._backend = backend
        else:
            if not host:
                raise RuntimeError("KEF host must be provided")
            self._backend = AiokefBackend(host)
        self.on_change: Optional[Callable[[KEFSnapshot], Awaitable[None]]] = None


    async def poll_loop(self, interval: float = 0.5) -> None:
        last: Optional[KEFSnapshot] = None
        while True:
            snap = await self._backend.get_snapshot()
            if last is None or snap != last:
                if self.on_change:
                    await self.on_change(snap)
                last = snap
            await asyncio.sleep(interval)

    # Commands are idempotent and wake unit by setting source
    async def set_source(self, source: Source) -> None:
        await self._backend.set_source(source)
        if self.on_change:
            await self.on_change(await self._backend.get_snapshot())

    async def set_volume(self, volume: int) -> None:
        await self._backend.set_volume(volume)
        if self.on_change:
            await self.on_change(await self._backend.get_snapshot())

    async def change_volume(self, delta: int) -> None:
        await self._backend.change_volume(delta)
        if self.on_change:
            await self.on_change(await self._backend.get_snapshot())

    async def set_mute(self, mute: bool) -> None:
        await self._backend.set_mute(mute)
        if self.on_change:
            await self.on_change(await self._backend.get_snapshot())

    async def media(self, command: str) -> None:
        await self._backend.media(command)
