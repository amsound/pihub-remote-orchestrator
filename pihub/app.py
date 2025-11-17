
"""PiHub Orchestrator entry: start BLE/HID + Orchestrator HTTP API and FSM."""
from __future__ import annotations
import asyncio, logging, os, json
import contextlib
import signal

try:
    import uvloop as _uvloop  # type: ignore
    _uvloop.install()
except Exception:
    pass

from .orchestrator.app import main as orchestrator_main
from .input_unifying import UnifyingReader
from .bt_le.controller import BTLEController
from .macros import MACROS

# Simple key routing: activity/media/volume mapped via KEYMAP_PATH (same format as before but using 'emit' text)
# We'll call the orchestrator HTTP API locally to keep coupling low.

import httpx

logger = logging.getLogger(__name__)

class LocalAPI:
    def __init__(self, base: str = "http://127.0.0.1:8000") -> None:
        self._base = base
        self._cli = httpx.AsyncClient(timeout=1.0)

    async def activity(self, action: str, station: str | None = None, reason: str | None = None) -> None:
        await self._cli.post(self._base + "/api/activity", json={"action": action, "station": station, "reason": reason})

    async def volume(self, change: str, level: int | None = None) -> None:
        await self._cli.post(self._base + "/api/volume", json={"change": change, "level": level})

    async def media(self, command: str) -> None:
        await self._cli.post(self._base + "/api/media", json={"command": command})

    async def radio(self, command: str) -> None:
        await self._cli.post(self._base + "/api/radio", json={"command": command})

async def run_inputs() -> None:
    api = LocalAPI()
    reader = UnifyingReader()
    async def on_edge(rem_key: str, edge: str) -> None:
        # Minimal hardcoded mapping for demo; replace with JSON keymap if desired.
        if edge != "down":
            return
        if rem_key in {"KEY_F1"}:
            await api.activity("watch", reason="remote")
        elif rem_key in {"KEY_F2"}:
            await api.activity("listen", reason="remote")
        elif rem_key in {"KEY_F3"}:
            await api.activity("power_off", reason="remote")
        elif rem_key in {"KEY_VOLUMEUP"}:
            await api.volume("up")
        elif rem_key in {"KEY_VOLUMEDOWN"}:
            await api.volume("down")
        elif rem_key in {"KEY_PLAYPAUSE"}:
            await api.media("toggle")
        elif rem_key in {"KEY_NEXTSONG"}:
            await api.media("next")
        elif rem_key in {"KEY_PREVIOUSSONG"}:
            await api.media("previous")
        elif rem_key in {"KEY_RIGHT"}:
            await api.radio("next")
        elif rem_key in {"KEY_LEFT"}:
            await api.radio("prev")

    await reader.start(on_edge)
    stop = asyncio.Event()
    await stop.wait()

async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO"))
    # Run orchestrator + input in-process
    t1 = asyncio.create_task(orchestrator_main())
    t2 = asyncio.create_task(run_inputs())

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()

    for t in (t1,t2):
        t.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.gather(t1,t2)

if __name__ == "__main__":
    asyncio.run(main())
