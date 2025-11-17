
from __future__ import annotations
import asyncio, logging, os
from fastapi import FastAPI
import uvicorn

from .persistence import SQLiteKV
from .eventbus import EventBus
from .adapters.kef import KEFAdapter
from .adapters.ma import MusicAssistant
from .adapters.samsung import SamsungTVMonitor
from .macros import AppleTVMacros
from .fsm import FSM, Defaults
from .http_api import make_app
from .radio import RadioDial
from .webhook import WebhookClient
from ..bt_le.controller import BTLEController

logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO"))

async def main() -> None:
    data_dir = os.environ.get("DATA_DIR","/data")
    os.makedirs(data_dir, exist_ok=True)
    kv = SQLiteKV(os.path.join(data_dir, "state.db"))
    bus = EventBus()

    kef = KEFAdapter(host=os.environ.get("KEF_HOST"))
    ma  = MusicAssistant(server_url=os.environ.get("MA_URL","http://127.0.0.1:8095"))
    tv  = SamsungTVMonitor(os.environ.get("TV_HOST","192.168.1.50"))
    bt  = BTLEController()
    macros = AppleTVMacros(bt)

    webhook = WebhookClient(os.environ.get("HA_WEBHOOK_URL"))
    room = os.environ.get("ROOM_NAME","living_room")
    defaults = Defaults(
        watch_volume=int(os.environ.get("DEF_WATCH_VOL","20")),
        listen_volume=int(os.environ.get("DEF_LISTEN_VOL","30")),
        listen_station=os.environ.get("DEF_LISTEN_STATION")
    )

    fsm = FSM(kv, bus, kef, ma, tv, macros, webhook, room, defaults)
    radio = RadioDial()

    # Restore persisted snapshot (no device side-effects)
    await fsm.restore()

    # Background tasks
    async def task_tv():
        await tv.poll_loop()
    async def task_kef():
        await kef.poll_loop()
    async def task_ma():
        await ma.connect()
    async def task_radio_refresh():
        # Boot refresh + weekly 03:00 Europe/London
        stations = await ma.fetch_radio_catalog()
        await radio.set_catalog(stations)
        while True:
            delay = await radio.next_3am_utc("Europe/London")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            stations = await ma.fetch_radio_catalog()
            await radio.set_catalog(stations)

    tasks = [
        asyncio.create_task(task_tv()),
        asyncio.create_task(task_kef()),
        asyncio.create_task(task_ma()),
        asyncio.create_task(task_radio_refresh()),
    ]

    # FastAPI app
    app = make_app(bus, fsm, radio)

    # Uvicorn inside this process
    config = uvicorn.Config(app=app, host="0.0.0.0", port=int(os.environ.get("PORT","8000")), log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    async def run_server():
        await server.serve()

    tasks.append(asyncio.create_task(run_server()))

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
