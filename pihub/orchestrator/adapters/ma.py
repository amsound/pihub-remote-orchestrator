from __future__ import annotations
import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Literal, Callable, Awaitable

import aiohttp

log = logging.getLogger(__name__)

MAState = Literal["off", "idle", "playing", "paused"]

@dataclass
class PlayerSnapshot:
    state: MAState = "off"
    player_id: Optional[str] = None
    name: Optional[str] = None


class MusicAssistant:
    """
    Music Assistant adapter using music-assistant-client via polling.
    - connect(): WS/API connect + start polling loop
    - ensure_player(name_or_id): resolve and persist player_id (ID wins over name)
    - get_snapshot()
    - play_station(name), media(command), stop()
    - fetch_radio_catalog()
    - close()
    """
    def __init__(self, server_url: str = "http://127.0.0.1:8095") -> None:
        self._url = server_url.rstrip("/")
        self._client = None  # type: ignore
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._player_hint: Optional[str] = None  # name or id preferred by user
        self._player_id: Optional[str] = None

        self._snap = PlayerSnapshot(state="off", player_id=None)
        self.on_change: Optional[Callable[[PlayerSnapshot], Awaitable[None]]] = None
        self._lock = asyncio.Lock()

    # ---------- lifecycle ----------

    async def connect(self) -> None:
        try:
            from music_assistant_client.client import MusicAssistantClient  # type: ignore
        except Exception as exc:  # pragma: no cover
            log.error("music-assistant-client not installed: %s", exc)
            return

        try:
            self._session = aiohttp.ClientSession()
            self._client = MusicAssistantClient(self._url, aiohttp_session=self._session)
            await self._client.connect()
            if self._poll_task is None:
                self._poll_task = asyncio.create_task(self._poll_loop(), name="ma_poll")
            log.info("Connected to Music Assistant at %s", self._url)
        except Exception as exc:
            log.error("MA connect failed: %s", exc)
            self._client = None

    async def close(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        try:
            if self._client:
                await self._client.disconnect()
        finally:
            if self._session:
                await self._session.close()
        self._client = None
        self._session = None

    # ---------- player resolution ----------

    async def ensure_player(self, name_or_id: Optional[str]) -> Optional[str]:
        """Persist preferred player (ID wins). Call anytime; safe before/after connect()."""
        async with self._lock:
            if name_or_id:
                self._player_hint = name_or_id.strip()
            if not self._client:
                # will resolve after connect/poll
                return self._player_id

            try:
                players = await self._client.players()  # list[dict]
            except Exception as exc:
                log.debug("MA players() failed: %s", exc)
                return self._player_id

            hint = (self._player_hint or "").strip()
            pid: Optional[str] = None

            if hint:
                # 1) exact ID
                for p in players:
                    if str(p.get("player_id", "")).strip() == hint:
                        pid = p["player_id"]
                        break
                # 2) exact name
                if not pid:
                    for p in players:
                        if str(p.get("display_name", "")).strip().lower() == hint.lower():
                            pid = p["player_id"]
                            break
                # 3) contains name
                if not pid:
                    for p in players:
                        if hint.lower() in str(p.get("display_name", "")).lower():
                            pid = p["player_id"]
                            break

            if not pid and players:
                pid = players[0]["player_id"]

            if pid and pid != self._player_id:
                self._player_id = pid
                await self._update_state(player_id=pid)

            return self._player_id

    # ---------- polling ----------

    async def _poll_loop(self) -> None:
        """Poll MA every second; normalize player state."""
        while True:
            try:
                if not self._client:
                    await asyncio.sleep(1.0)
                    continue

                # Always (re)resolve preferred player if needed
                await self.ensure_player(self._player_hint)

                state: MAState = "off"
                name: Optional[str] = None

                players = await self._client.players()
                if players and self._player_id:
                    for p in players:
                        if p.get("player_id") == self._player_id:
                            raw = (p.get("state") or p.get("power_state") or "").lower()
                            if raw in {"playing", "paused", "idle"}:
                                state = raw  # type: ignore[assignment]
                            else:
                                state = "idle"
                            name = str(p.get("display_name") or "") or None
                            break

                await self._update_state(state=state, name=name)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                # keep quiet but resilient
                log.debug("MA poll error: %s", exc)
            await asyncio.sleep(1.0)

    # ---------- snapshot & notify ----------

    async def _update_state(
        self, *, state: Optional[MAState] = None, player_id: Optional[str] = None, name: Optional[str] = None
    ) -> None:
        changed = False
        if state and state != self._snap.state:
            self._snap.state = state
            changed = True
        if player_id and player_id != self._snap.player_id:
            self._snap.player_id = player_id
            changed = True
        if name is not None and name != self._snap.name:
            self._snap.name = name
            changed = True
        if changed and self.on_change:
            await self.on_change(self._snap)

    async def get_snapshot(self) -> PlayerSnapshot:
        return self._snap

    # ---------- transport ----------

    async def media(self, command: str) -> None:
        if not self._client or not self._player_id:
            return
        cmd = command.lower()
        try:
            if cmd in {"play", "toggle"}:
                await self._client.player_play(self._player_id)
                await self._update_state(state="playing")
            elif cmd == "pause":
                await self._client.player_pause(self._player_id)
                await self._update_state(state="paused")
            elif cmd == "stop":
                await self._client.player_stop(self._player_id)
                await self._update_state(state="idle")
            elif cmd == "next":
                await self._client.player_next(self._player_id)
            elif cmd == "previous":
                await self._client.player_previous(self._player_id)
        except Exception as exc:
            log.warning("MA media %s failed: %s", cmd, exc)

    async def stop(self) -> None:
        if not self._client or not self._player_id:
            await self._update_state(state="idle")
            return
        try:
            await self._client.player_stop(self._player_id)
        except Exception:
            pass
        await self._update_state(state="idle")

    # ---------- radio ----------

    async def fetch_radio_catalog(self) -> List[str]:
        if not self._client:
            return []
        stations: List[str] = []
        try:
            items = await self._client.library_items(media_type="radio")
            for it in items or []:
                nm = it.get("name") or it.get("title")
                if nm:
                    stations.append(str(nm))
        except Exception as exc:
            log.debug("MA radio catalog failed: %s", exc)
            try:
                items = await self._client.search("radio")
                for it in items or []:
                    nm = it.get("name") or it.get("title")
                    if nm:
                        stations.append(str(nm))
            except Exception:
                pass
        # de-dup, stable
        seen = set()
        out: List[str] = []
        for n in stations:
            low = n.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(n)
        return out

    async def play_station(self, station_name: str) -> None:
        if not self._client or not self._player_id:
            return
        want = station_name.strip().lower()
        try:
            items = await self._client.library_items(media_type="radio")
            target = None
            for it in items or []:
                nm = (it.get("name") or it.get("title") or "").lower()
                if nm == want:
                    target = it
                    break
            if not target:
                for it in items or []:
                    nm = (it.get("name") or it.get("title") or "").lower()
                    if want in nm:
                        target = it
                        break
            if not target:
                log.warning("Station not found: %r", station_name)
                return
            item_id = target.get("item_id") or target.get("uri") or target.get("id")
            if not item_id:
                log.warning("Station has no playable id: %r", target)
                return
            await self._client.play_media(self._player_id, item_id=item_id)
            await self._update_state(state="playing")
        except Exception as exc:
            log.warning("MA play_station failed: %s", exc)
