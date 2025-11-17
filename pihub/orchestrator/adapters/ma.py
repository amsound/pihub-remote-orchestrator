from __future__ import annotations
import asyncio, logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Literal, Callable, Awaitable

log = logging.getLogger(__name__)

MAState = Literal['off','idle','playing','paused']

@dataclass
class PlayerSnapshot:
    state: MAState = 'off'
    player_id: Optional[str] = None
    name: Optional[str] = None

class MusicAssistant:
    """Music Assistant WS client using 'music-assistant-client' if available."""
    def __init__(self, server_url: str = 'http://127.0.0.1:8095') -> None:
        self._url = server_url.rstrip('/')
        self._player_name_or_id: Optional[str] = None
        self._player_id: Optional[str] = None
        self._snap = PlayerSnapshot(state='off', player_id=None)
        self.on_change: Optional[Callable[[PlayerSnapshot], Awaitable[None]]] = None
        self._client = None  # music-assistant-client instance
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            from music_assistant_client.client import MusicAssistantClient  # type: ignore
            self._client = MusicAssistantClient(self._url)
            await self._client.connect()
            asyncio.create_task(self._listener())
            log.info('Connected to Music Assistant at %s', self._url)
        except Exception as exc:
            log.error('MA connect failed: %s', exc)
            self._client = None

    async def _listener(self) -> None:
        if not self._client:
            return
        try:
            async for evt in self._client.events():
                et = evt.get('event')
                if et == 'player_changed':
                    pid = evt.get('data', {}).get('player_id')
                    if not self._player_id or pid != self._player_id:
                        continue
                    state = self._normalize_state(evt.get('data', {}).get('state'))
                    await self._update_state(state=state)
        except Exception as exc:
            log.warning('MA listener stopped: %s', exc)

    def _normalize_state(self, raw: Any) -> MAState:
        s = str(raw or '').lower()
        if s in {'playing'}: return 'playing'
        if s in {'paused'}: return 'paused'
        if s in {'idle', 'stopped'}: return 'idle'
        return 'off'

    async def _update_state(self, *, state: Optional[MAState] = None, player_id: Optional[str] = None, name: Optional[str] = None) -> None:
        changed = False
        if state and state != self._snap.state:
            self._snap.state = state; changed = True
        if player_id and player_id != self._snap.player_id:
            self._snap.player_id = player_id; changed = True
        if name and name != self._snap.name:
            self._snap.name = name; changed = True
        if changed and self.on_change:
            await self.on_change(self._snap)

    async def ensure_player(self, name_or_id: Optional[str]) -> Optional[str]:
        async with self._lock:
            self._player_name_or_id = name_or_id
            if not self._client:
                return None
            try:
                players = await self._client.players()
                want = (name_or_id or '').strip().lower()
                for p in players:
                    if str(p.get('player_id','')).lower() == want:
                        self._player_id = p['player_id']; break
                if not self._player_id and want:
                    for p in players:
                        if str(p.get('display_name','')).lower() == want:
                            self._player_id = p['player_id']; break
                if not self._player_id and want:
                    for p in players:
                        if want in str(p.get('display_name','')).lower():
                            self._player_id = p['player_id']; break
                if not self._player_id and players:
                    self._player_id = players[0]['player_id']
                await self._update_state(player_id=self._player_id, name=want or None)
                return self._player_id
            except Exception as exc:
                log.error('MA ensure_player failed: %s', exc)
                return None

    async def get_snapshot(self) -> PlayerSnapshot:
        return self._snap

    async def media(self, command: str) -> None:
        if not self._client or not self._player_id:
            return
        cmd = command.lower()
        try:
            if cmd in {'play','toggle'}:
                await self._client.player_play(self._player_id)
                await self._update_state(state='playing')
            elif cmd == 'pause':
                await self._client.player_pause(self._player_id)
                await self._update_state(state='paused')
            elif cmd == 'stop':
                await self._client.player_stop(self._player_id)
                await self._update_state(state='idle')
            elif cmd == 'next':
                await self._client.player_next(self._player_id)
            elif cmd == 'previous':
                await self._client.player_previous(self._player_id)
            elif cmd in {'ff','rw'}:
                pass
        except Exception as exc:
            log.warning('MA media %s failed: %s', cmd, exc)

    async def stop(self) -> None:
        if not self._client or not self._player_id:
            await self._update_state(state='idle')
            return
        try:
            await self._client.player_stop(self._player_id)
        except Exception:
            pass
        await self._update_state(state='idle')

    async def fetch_radio_catalog(self) -> List[str]:
        if not self._client:
            return []
        stations: List[str] = []
        try:
            items = await self._client.library_items(media_type='radio')
            for it in items or []:
                name = it.get('name') or it.get('title')
                if name:
                    stations.append(str(name))
        except Exception as exc:
            log.warning('MA radio catalog via API failed: %s', exc)
            try:
                items = await self._client.search('radio')
                for it in items or []:
                    name = it.get('name') or it.get('title')
                    if name:
                        stations.append(str(name))
            except Exception:
                pass
        seen = set(); out: List[str] = []
        for n in stations:
            low = n.lower()
            if low in seen: continue
            seen.add(low); out.append(n)
        return out

    async def play_station(self, station_name: str) -> None:
        if not self._client or not self._player_id:
            return
        name = station_name.strip().lower()
        try:
            items = await self._client.library_items(media_type='radio')
            target = None
            for it in items or []:
                nm = (it.get('name') or it.get('title') or '').lower()
                if nm == name:
                    target = it; break
            if not target:
                for it in items or []:
                    nm = (it.get('name') or it.get('title') or '').lower()
                    if name in nm:
                        target = it; break
            if not target:
                log.warning('Station %r not found', station_name); return
            item_id = target.get('item_id') or target.get('uri') or target.get('id')
            await self._client.play_media(self._player_id, item_id=item_id)
            await self._update_state(state='playing')
        except Exception as exc:
            log.warning('MA play_station failed: %s', exc)