"""Handle Logitech Unifying keyboards with automatic reconnection."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import random
from typing import Awaitable, Callable, Dict, Optional

from evdev import InputDevice, ecodes

EdgeCallback = Callable[[str, str], Awaitable[None]] | Callable[[str, str], None]

def _jittered(t: float) -> float:
    """±25% jitter, capped to 10s."""
    return min(10.0, t) * (0.75 + random.random() * 0.5)

class UnifyingReader:
    """
    Reads a Logitech Unifying (or generic event-kbd) device and emits logical key edges.

    • scancode_map maps either:
        - "KEY_LEFT" style names → "rem_*"
        - numeric scan codes as strings (e.g. "786924") → "rem_*"
    • Emits only 'down' and 'up' edges (ignores auto-repeat).
    • Survives hot-unplug/replug: reopens device with jittered backoff.
    """

    def __init__(
        self,
        device_path: Optional[str],
        scancode_map: Dict[str, str],
        on_edge: EdgeCallback,
        *,
        grab: bool = True,
    ) -> None:
        self._device_path = (device_path or "").strip() or None
        self._map = scancode_map
        self._on_edge = on_edge
        self._grab = grab

        self._task: Optional[asyncio.Task] = None
        self._edge_worker: Optional[asyncio.Task] = None
        self._edge_queue: Optional[asyncio.Queue[tuple[str, str] | None]] = None
        self._stop = asyncio.Event()

    # ── Public API ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Begin watching the configured input device."""
        if self._task is None:
            self._stop.clear()
            self._edge_queue = asyncio.Queue()
            self._edge_worker = asyncio.create_task(
                self._drain_edges(), name="unifying_edge_worker"
            )
            self._task = asyncio.create_task(self._run(), name="unifying_reader")

    async def stop(self) -> None:
        """Stop watching the input device and wait for shutdown."""
        if self._task:
            self._stop.set()
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._edge_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._edge_queue.put_nowait(None)
        if self._edge_worker:
            self._edge_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._edge_worker
            self._edge_worker = None
        self._edge_queue = None

    @property
    def device_path(self) -> str:
        # Report the last known/selected path (may be None before first open)
        if self._device_path:
            return self._device_path
        # best-effort autodetect for display
        path = _autodetect_or_none()
        return path or "<auto>"

    # ── Internals ───────────────────────────────────────────────────────────
    async def _run(self) -> None:
        backoff = 1.0  # seconds, exponential up to 10s
        while not self._stop.is_set():
            # Resolve a device path each round
            path = self._device_path
            if not (path and os.path.exists(path)):
                path = _autodetect_or_none()
                if path:
                    # lock in once found
                    self._device_path = path
    
            if not path:
                # device not present; wait and retry (jittered)
                await asyncio.sleep(_jittered(backoff))
                backoff = min(backoff * 2, 10.0)
                continue
    
            # Try to open the device
            try:
                dev = InputDevice(path)
            except Exception:
                await asyncio.sleep(_jittered(backoff))
                backoff = min(backoff * 2, 10.0)
                continue
    
            # Optional exclusive grab; never fatal
            grabbed = False
            if self._grab:
                try:
                    dev.grab()
                    grabbed = True
                except Exception:
                    # continue without grab
                    pass
    
            print(f'[usb] open {path} grabbed={str(grabbed).lower()}', flush=True)
    
            # We have an open device; reset backoff
            backoff = 1.0
            
            last_msc_scan: Optional[int] = None
            pressed = set()
            
            _EV_MSC = ecodes.EV_MSC
            _MSC_SCAN = ecodes.MSC_SCAN
            _EV_KEY = ecodes.EV_KEY
            
            _map = self._map
            _resolve = self._resolve_logical_key
            _emit = self._emit
            _debug_unknown = (os.getenv("DEBUG_INPUT_UNK") == "1")
            
            try:
                async for ev in dev.async_read_loop():
                    t = ev.type
            
                    if t == _EV_MSC and ev.code == _MSC_SCAN:
                        last_msc_scan = int(ev.value)
                        continue
            
                    if t != _EV_KEY:
                        continue
            
                    logical = _resolve(ev.code, last_msc_scan)
                    last_msc_scan = None  # single-use
            
                    if not logical:
                        if _debug_unknown:
                            # only compute name when actually logging
                            try:
                                kname = ecodes.KEY[ev.code]
                            except Exception:
                                kname = f"KEY_{ev.code}"
                            print(f"[usb] unmapped key: msc=None name={kname}")
                        continue
            
                    val = ev.value
                    if val == 2:  # auto-repeat from kernel
                        continue
            
                    key_id = (logical, ev.code)
                    if val == 1:  # down
                        if key_id in pressed:
                            continue
                        pressed.add(key_id)
                        await _emit(logical, "down")
                    else:  # up
                        pressed.discard(key_id)
                        await _emit(logical, "up")
            
            except (OSError, IOError) as e:
                err = getattr(e, "errno", None)
                if err in (19, 5):  # ENODEV/EIO
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)
                else:
                    await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                # Shutting down
                break

            except Exception as exc:
                print(f"[usb] reader error: {exc!r}", flush=True)
                await asyncio.sleep(1.0)

            finally:
                with contextlib.suppress(Exception):
                    if grabbed:
                        dev.ungrab()
                with contextlib.suppress(Exception):
                    dev.close()
    
        # exit: ensure stop flag remains set
        self._stop.set()

    def _resolve_logical_key(self, key_code: int, msc_scan: Optional[int]) -> Optional[str]:
        # Prefer explicit MSC numeric mapping
        if msc_scan is not None:
            mapped = self._map.get(str(msc_scan))
            if mapped:
                return mapped

        # Else KEY_* name mapping
        name = _key_name_from_code(key_code)
        if name:
            mapped = self._map.get(name)
            if mapped:
                return mapped

        return None

    async def _emit(self, rem_key: str, edge: str) -> None:
        if os.getenv("DEBUG_INPUT") == "1":
            print(f"[usb] {rem_key} {edge}")
        queue = self._edge_queue
        if queue is None:
            return
        queue.put_nowait((rem_key, edge))

    async def _drain_edges(self) -> None:
        queue = self._edge_queue
        if queue is None:
            return
        try:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                rem_key, edge = item
                try:
                    res = self._on_edge(rem_key, edge)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    print(f"[usb] dispatch error: {exc!r}", flush=True)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            while not queue.empty():
                with contextlib.suppress(Exception):
                    queue.get_nowait()
                    queue.task_done()


def _autodetect_or_none() -> Optional[str]:
    """Best-effort find a keyboard-like event device via by-id; return None if absent."""
    cand = sorted(glob.glob("/dev/input/by-id/*Unifying*event-kbd")) or sorted(
        glob.glob("/dev/input/by-id/*event-kbd")
    )
    return cand[0] if cand else None


def _key_name_from_code(code: int) -> Optional[str]:
    try:
        name = ecodes.KEY[code]  # e.g. 'KEY_LEFT'
        return name if isinstance(name, str) else None
    except Exception:
        return None
