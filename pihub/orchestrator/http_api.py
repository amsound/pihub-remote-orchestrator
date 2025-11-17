
from __future__ import annotations
import asyncio, json, logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Response, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .eventbus import EventBus
from .fsm import FSM
from .state import OrchestratorState

log = logging.getLogger(__name__)

class ActivityBody(BaseModel):
    action: str
    station: Optional[str] = None
    reason: Optional[str] = None

class VolumeBody(BaseModel):
    change: str
    level: Optional[int] = None

class MediaBody(BaseModel):
    command: str

class RadioBody(BaseModel):
    command: str
    name: Optional[str] = None
    index: Optional[int] = None

class SourceBody(BaseModel):
    device: str
    source: str

class PatchVolumesBody(BaseModel):
    watch: Optional[int] = None
    listen: Optional[int] = None

class PatchDefaultsBody(BaseModel):
    listen_station: Optional[str] = None

def make_app(bus: EventBus, fsm: FSM, radio) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/state")
    async def get_state():
        return fsm.state.to_dict()

    @app.post("/api/activity")
    async def post_activity(body: ActivityBody):
        a = body.action.lower()
        if a == "watch":
            await fsm.cmd_watch(reason=body.reason)
        elif a == "listen":
            await fsm.cmd_listen(station=body.station, reason=body.reason)
        elif a in {"power_off","off"}:
            await fsm.cmd_power_off(reason=body.reason)
        else:
            return JSONResponse({"ok": False, "error": "invalid action"}, status_code=400)
        return {"ok": True, "activity": fsm.state.activity}

    @app.post("/api/volume")
    async def post_volume(body: VolumeBody):
        if body.change == "up":
            await fsm.kef.change_volume(+2)
        elif body.change == "down":
            await fsm.kef.change_volume(-2)
        elif body.change == "set" and body.level is not None:
            await fsm.kef.set_volume(int(body.level))
        else:
            return JSONResponse({"ok": False, "error": "invalid"}, status_code=400)
        return {"ok": True}

    @app.post("/api/media")
    async def post_media(body: MediaBody):
        target = await fsm.route_media(body.command)
        return {"ok": True, "target": target}

    @app.post("/api/radio")
    async def post_radio(body: RadioBody):
        idx = await radio.get_index()
        played = False
        if body.command == "next":
            idx = await radio.next()
        elif body.command == "prev":
            idx = await radio.prev()
        elif body.command == "tune":
            if body.name:
                idx = await radio.find_by_name(body.name)
            elif body.index is not None:
                idx = await radio.set_index(body.index)
        else:
            return JSONResponse({"ok": False, "error":"invalid command"}, status_code=400)

        await fsm.kv.set("radio_station_index", idx)
        fsm.state.radio_index = idx
        await bus.publish({"type":"radio","data":{"index": idx}})

        # play if MA available and in LISTEN
        catalog = await radio.get_catalog()
        if 0 <= idx < len(catalog) and fsm.state.activity == "LISTEN" and fsm.state.ma.state != "off":
            await fsm.ma.play_station(catalog[idx])
            played = True
        return {"ok": True, "index": idx, "played": played}

    @app.get("/api/radio/stations")
    async def get_stations():
        return await radio.get_catalog()

    @app.post("/api/radio/resync")
    async def post_resync():
        stations = await fsm.ma.fetch_radio_catalog()
        await radio.set_catalog(stations)
        await fsm.kv.set("stations_refreshed_at", __import__("datetime").datetime.utcnow().isoformat())
        return {"ok": True, "count": len(stations)}

    @app.post("/api/source")
    async def post_source(body: SourceBody):
        if body.device != "kef" or body.source not in {"Opt","Wifi"}:
            return JSONResponse({"ok": False, "error":"invalid"}, status_code=400)
        await fsm.kef.set_source(body.source)  # this may cause passive LISTEN
        return {"ok": True}

    @app.patch("/api/config/volumes")
    async def patch_volumes(body: PatchVolumesBody):
        if body.watch is not None:
            fsm.defaults.watch_volume = int(body.watch)
            await fsm.kv.set("kef_default_watch", fsm.defaults.watch_volume)
        if body.listen is not None:
            fsm.defaults.listen_volume = int(body.listen)
            await fsm.kv.set("kef_default_listen", fsm.defaults.listen_volume)
        return {"ok": True}

    @app.patch("/api/config/defaults")
    async def patch_defaults(body: PatchDefaultsBody):
        if body.listen_station is not None:
            fsm.defaults.listen_station = body.listen_station
            await fsm.kv.set("listen_default_station", body.listen_station)
        return {"ok": True}

    @app.get("/events")
    async def sse(req: Request):
        async def gen():
            # send initial
            yield f"event: state\ndata: {json.dumps(fsm.state.to_dict())}\n\n"
            async for ev in bus.subscribe():
                if await req.is_disconnected():
                    break
                yield f"event: {ev.get('type','state')}\ndata: {json.dumps(ev.get('data'))}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
