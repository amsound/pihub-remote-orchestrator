
from __future__ import annotations
import asyncio, socket
from typing import Callable, Awaitable, Optional, Literal
import httpx

Power = Literal["on","off"]

class SamsungTVMonitor:
    def __init__(self, host: str, *, http_port: int = 8001, timeout: float = 1.0) -> None:
        self._host = host
        self._http_port = http_port
        self._timeout = timeout
        self.on_power: Optional[Callable[[Power], Awaitable[None]]] = None

    async def _http_probe(self) -> Optional[Power]:
        url = f"http://{self._host}:{self._http_port}/api/v2/"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as cli:
                r = await cli.get(url)
                if r.status_code == 200:
                    return "on"
        except Exception:
            return None
        return None

    async def _tcp_ping(self, port: int) -> bool:
        try:
            fut = asyncio.open_connection(self._host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=self._timeout)
            writer.close()
            return True
        except Exception:
            return False

    async def poll_loop(self, interval: float = 2.0) -> None:
        last: Optional[Power] = None
        while True:
            power: Power = "off"
            # Heuristics: HTTP API, else try common ports 8001/8002
            probe = await self._http_probe()
            if probe is None:
                alive = await self._tcp_ping(8001) or await self._tcp_ping(8002)
                power = "on" if alive else "off"
            else:
                power = probe
            if power != last:
                if self.on_power:
                    await self.on_power(power)
                last = power
            await asyncio.sleep(interval)
