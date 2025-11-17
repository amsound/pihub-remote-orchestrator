"""Home Assistant WebSocket integration with resilient reconnects + subscribe_trigger."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import contextlib

OnActivity = Callable[[str], Awaitable[None]] | Callable[[str], None]
OnCmd      = Callable[[dict], Awaitable[None]] | Callable[[dict], None]


class HAWS:
    """
    Uses subscribe_trigger to receive only the target entity's changes.
    Why: reduce WS noise/CPU on constrained devices.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        activity_entity: str,
        event_name: str,
        on_activity: OnActivity,
        on_cmd: OnCmd,
    ) -> None:
        self._url = url
        self._token = token or ""
        self._activity_entity = activity_entity
        self._event_name = event_name
        self._on_activity = on_activity
        self._on_cmd = on_cmd

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._stopping = asyncio.Event()
        self._msg_id = 1
        self._last_activity: Optional[str] = None

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run until stop() is called. Reconnect with exponential backoff + jitter."""
        delay = 1.0
        while not self._stopping.is_set():
            try:
                await self._connect_once()
                delay = 1.0
                if not self._stopping.is_set():
                    await asyncio.sleep(random.uniform(0.2, 0.8))
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping.is_set():
                    print(f"[ws] error: {exc!r}", flush=True)
                jitter = random.uniform(0.75, 1.25)
                timeout = min(60.0, delay) * jitter
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
                    break
                except asyncio.TimeoutError:
                    delay = min(delay * 2.0, 60.0)
                    continue

    async def stop(self) -> None:
        """Signal the client to stop and close the socket."""
        self._stopping.set()
        await self._close_ws()
        await self._close_session()

    async def send_cmd(self, text: str, **extra: Any) -> bool:
        """
        Fire an event to HA (dest:'ha'). No acks, no buffering.
        """
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json({
                "id": self._next_id(),
                "type": "fire_event",
                "event_type": self._event_name,
                "event_data": {"dest": "ha", "text": text, **extra},
            })
            return True
        except Exception:
            return False

    # ── Internals ───────────────────────────────────────────────────────────

    async def _connect_once(self) -> None:
        """One lifecycle: connect → auth → subscribe → seed → recv loop → close."""
        await self._close_ws()
        session = await self._ensure_session()

        try:
            ws = await session.ws_connect(self._url, heartbeat=30, autoping=True)
        except Exception:
            await self._close_ws()
            raise

        self._ws = ws
        try:
            await self._auth(ws)

            print("[ws] connected")  # log *before* seed so order is consistent

            # Subscribe to ONLY the target entity via trigger.
            await self._subscribe_trigger_entity(ws, self._activity_entity)

            # Keep custom event bus subscription unchanged (e.g., "pihub.cmd").
            await self._subscribe(ws, self._event_name)

            # Seed activity from current states once.
            await self._seed_activity(ws)

            # Receive until closed.
            await self._recv_loop(ws)

        finally:
            print("[ws] disconnected")
            await self._close_ws()

    async def _auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == "auth_ok":
            return
        if mtype != "auth_required":
            raise RuntimeError(f"unexpected handshake: {mtype}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        msg = await ws.receive_json()
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"auth failed: {msg}")

    async def _seed_activity(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """
        Fetch current activity once; ALWAYS print + callback, then cache.
        Why: ensure we resync after reconnects without relying on missed events.
        """
        req_id = self._next_id()
        await ws.send_json({"id": req_id, "type": "get_states"})
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "result" and msg.get("id") == req_id and msg.get("success"):
                states = msg.get("result") or []
                for st in states:
                    if st.get("entity_id") == self._activity_entity:
                        val = str(st.get("state", "") or "").strip()
                        if val:
                            print(f"[activity] {val}")
                            self._last_activity = val
                            res = self._on_activity(val)
                            if asyncio.iscoroutine(res):
                                await res
                return
            # ignore interleaved messages until our result arrives

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse, event_type: str) -> None:
        await ws.send_json({"id": self._next_id(), "type": "subscribe_events", "event_type": event_type})

    async def _subscribe_trigger_entity(self, ws: aiohttp.ClientWebSocketResponse, entity_id: str) -> None:
        """
        Server-side filter: only deliver state changes for this entity.
        """
        await ws.send_json({
            "id": self._next_id(),
            "type": "subscribe_trigger",
            "trigger": {
                "platform": "state",
                "entity_id": entity_id,
            },
        })
        # Note: HA replies with a result, then sends trigger matches as events with
        # event.variables.trigger.{from_state,to_state}. (Docs show 'type: event' payload.)  # noqa: E501

    def _extract_trigger_states(self, ev: dict) -> tuple[Optional[dict], Optional[dict]]:
        """
        Return (from_state, to_state) from common subscribe_trigger shapes.
        Why: HA docs/examples differ between versions; be tolerant. :contentReference[oaicite:1]{index=1}
        """
        vars_ = ev.get("variables") or {}
        trig = vars_.get("trigger") or {}
        if not trig and "data" in ev:
            # Some builds place trigger info in data; keep backward-friendly.
            trig = (ev.get("data") or {}).get("trigger") or {}
        from_state = trig.get("from_state") or (ev.get("data") or {}).get("from_state")
        to_state = trig.get("to_state") or (ev.get("data") or {}).get("to_state")
        return from_state, to_state

    async def _recv_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._stopping.is_set():
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                if data.get("type") == "event":
                    ev = data.get("event") or {}
                    ev_type = ev.get("event_type")
                    edata = ev.get("data") or {}

                    # 1) Triggered state change for our one entity (subscribe_trigger).
                    #    No need to re-check entity_id, but do it defensively.
                    from_state, to_state = self._extract_trigger_states(ev)
                    if to_state:
                        ent = to_state.get("entity_id") or (from_state or {}).get("entity_id")
                        if not ent or ent == self._activity_entity:
                            new_state = to_state.get("state")
                            if isinstance(new_state, str) and new_state:
                                if new_state != self._last_activity:
                                    print(f"[activity] {new_state}")
                                    self._last_activity = new_state
                                res = self._on_activity(new_state)
                                if asyncio.iscoroutine(res):
                                    await res
                            continue  # already handled

                    # 2) Your custom command events (unchanged).
                    if ev_type == self._event_name:
                        if edata.get("dest") == "pi":
                            t = edata.get("text", "?")
                            if t == "macro":
                                print(f"[cmd] macro {edata.get('name', '?')}")
                            elif t == "ble_key":
                                print(
                                    f"[cmd] ble_key {edata.get('usage', '?')}/{edata.get('code', '?')} "
                                    f"hold={int(edata.get('hold_ms', 40))}ms"
                                )
                            else:
                                print(f"[cmd] {t}")
                            res = self._on_cmd(edata)
                            if asyncio.iscoroutine(res):
                                await res

            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break  # reconnect

    def _next_id(self) -> int:
        i = self._msg_id
        self._msg_id += 1
        return i

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _close_session(self) -> None:
        sess, self._session = self._session, None
        if sess:
            with contextlib.suppress(Exception):
                await sess.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        session = self._session
        if session is None or session.closed:
            self._session = session = aiohttp.ClientSession()
        return session
