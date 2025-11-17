
from __future__ import annotations
import asyncio, logging
from dataclasses import dataclass
from typing import Literal, Optional, Tuple
from datetime import datetime, timezone

from .state import OrchestratorState, TVState, KEFState, MAState
from .adapters.kef import KEFAdapter, KEFSnapshot
from .adapters.ma import MusicAssistant, PlayerSnapshot
from .adapters.samsung import SamsungTVMonitor, Power
from .macros import AppleTVMacros
from .persistence import SQLiteKV
from .eventbus import EventBus
from .webhook import WebhookClient

Activity = Literal["OFF","WATCH","LISTEN"]

log = logging.getLogger(__name__)

@dataclass
class Defaults:
    watch_volume: int = 20
    listen_volume: int = 30
    listen_station: Optional[str] = None

class FSM:
    def __init__(
        self,
        kv: SQLiteKV,
        bus: EventBus,
        kef: KEFAdapter,
        ma: MusicAssistant,
        tv: SamsungTVMonitor,
        macros: AppleTVMacros,
        webhook: WebhookClient,
        room: str,
        defaults: Defaults,
    ) -> None:
        self.kv = kv
        self.bus = bus
        self.kef = kef
        self.ma = ma
        self.tv = tv
        self.macros = macros
        self.webhook = webhook
        self.room = room
        self.defaults = defaults
        self.state = OrchestratorState()

        self._tv_cached_power: Power = "off"

        kef.on_change = self._on_kef_change
        ma.on_change  = self._on_ma_change
        tv.on_power   = self._on_tv_power

    async def restore(self) -> None:
        keys = [
            "last_activity","radio_station_index","tv_last_power",
            "kef_last_source","kef_last_volume","kef_last_mute",
            "ma_player_id","ma_last_state",
            "kef_default_watch","kef_default_listen","listen_default_station"
        ]
        data = await self.kv.mget(keys)
        self.state.activity = (data.get("last_activity") or "OFF").upper()
        self.state.radio_index = int(data.get("radio_station_index") or -1)
        self.state.tv.power = (data.get("tv_last_power") or "off")
        self._tv_cached_power = self.state.tv.power  # gate macros
        self.state.kef.source = data.get("kef_last_source") or "Opt"
        self.state.kef.volume = int(data.get("kef_last_volume") or self.defaults.watch_volume)
        self.state.kef.mute   = bool(data.get("kef_last_mute") or False)
        self.state.ma.player_id = data.get("ma_player_id")
        self.state.ma.state = data.get("ma_last_state") or "off"
        # defaults (PATCH-able)
        self.defaults.watch_volume = int(data.get("kef_default_watch") or self.defaults.watch_volume)
        self.defaults.listen_volume = int(data.get("kef_default_listen") or self.defaults.listen_volume)
        self.defaults.listen_station = data.get("listen_default_station") or self.defaults.listen_station
        await self._emit_state()

    async def _persist_snapshots(self) -> None:
        await self.kv.set("tv_last_power", self.state.tv.power)
        await self.kv.set("kef_last_source", self.state.kef.source)
        await self.kv.set("kef_last_volume", self.state.kef.volume)
        await self.kv.set("kef_last_mute", self.state.kef.mute)
        await self.kv.set("ma_player_id", self.state.ma.player_id)
        await self.kv.set("ma_last_state", self.state.ma.state)

    async def _emit_state(self) -> None:
        await self.bus.publish({"type":"state", "data": self.state.to_dict()})

    async def _post_webhook(self, activity: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        await self.webhook.post_activity(self.room, activity.lower().replace("off","power_off"), ts)

    # ---- Adapter events ----
    async def _on_tv_power(self, power: Power) -> None:
        self.state.tv.power = power
        self._tv_cached_power = power
        await self.kv.set("tv_last_power", power)
        # Passive WATCH when TV flips on
        if power == "on" and self.state.activity != "WATCH":
            await self.enter_watch(passive=True)
        await self._emit_state()

    async def _on_kef_change(self, snap: KEFSnapshot) -> None:
        prev_source = self.state.kef.source
        self.state.kef.source = snap.source
        self.state.kef.volume = snap.volume
        self.state.kef.mute   = snap.mute
        await self._persist_snapshots()
        # Passive LISTEN if source becomes Wifi
        if prev_source != "Wifi" and snap.source == "Wifi" and self.state.activity != "LISTEN":
            await self.enter_listen(passive=True)
        await self._emit_state()

    async def _on_ma_change(self, snap: PlayerSnapshot) -> None:
        self.state.ma.state = snap.state
        self.state.ma.player_id = snap.player_id
        await self._persist_snapshots()
        await self._emit_state()

    # ---- Commands (explicit) ----
    async def cmd_watch(self, reason: str | None = None) -> None:
        await self.enter_watch(passive=False)

    async def cmd_listen(self, station: Optional[str] = None, reason: str | None = None) -> None:
        await self.enter_listen(passive=False, station=station)

    async def cmd_power_off(self, reason: str | None = None) -> None:
        await self.enter_off()

    # ---- Entry actions ----
    async def enter_watch(self, passive: bool) -> None:
        await self.bus.publish({"type":"activity","data":{"activity":"WATCH","passive": passive}})
        if not passive and self._tv_cached_power == "off":
            # gate to avoid double toggle
            asyncio.create_task(self.macros.power_on())
        await self.kef.set_source("Opt")
        await self.ma.stop()
        self.state.radio_index = -1
        await self.kv.set("radio_station_index", -1)
        await self.kef.set_volume(self.defaults.watch_volume)
        self.state.activity = "WATCH"
        await self.kv.set("last_activity", "WATCH")
        await self._post_webhook("watch")
        await self._emit_state()

    async def enter_listen(self, passive: bool, station: Optional[str] = None) -> None:
        await self.bus.publish({"type":"activity","data":{"activity":"LISTEN","passive": passive}})
        await self.kef.set_source("Wifi")
        if not passive:
            # Only command MA on explicit
            if station or self.defaults.listen_station:
                name = station or self.defaults.listen_station
                try:
                    await self.ma.play_station(name or "")
                    self.state.ma.state = "playing"
                    await self.kef.set_volume(self.defaults.listen_volume)
                except Exception as exc:
                    log.warning("Failed to play MA station: %s", exc)
        else:
            # Passive: if TV is on, power it off via macro (gated)
            if self._tv_cached_power == "on":
                asyncio.create_task(self.macros.power_off())
        self.state.activity = "LISTEN"
        await self.kv.set("last_activity", "LISTEN")
        await self._post_webhook("listen")
        await self._emit_state()

    async def enter_off(self) -> None:
        await self.bus.publish({"type":"activity","data":{"activity":"OFF","passive": False}})
        if self._tv_cached_power == "on":
            asyncio.create_task(self.macros.power_off())
        await self.ma.stop()
        # Simulate KEF power off by source->Opt and mute
        await self.kef.set_mute(True)
        self.state.activity = "OFF"
        await self.kv.set("last_activity", "OFF")
        await self._post_webhook("power_off")
        await self._emit_state()

    # ---- Routing ----
    async def route_media(self, command: str) -> str:
        # Volume always KEF (handled elsewhere)
        # Media: in LISTEN and MA != off -> MA, else KEF
        target = "kef"
        if self.state.activity == "LISTEN" and self.state.ma.state in {"idle","playing","paused"}:
            await self.ma.media(command)
            target = "ma"
        else:
            await self.kef.media(command)
            target = "kef"
        return target
