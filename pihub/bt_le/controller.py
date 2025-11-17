"""Bluetooth Low Energy controller orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

# Import module so we can read the runtime singleton AFTER start_hid()
from . import hid_device as _hd
from .hid_client import HIDClient


class HIDTransportBLE:
    """Low-level transport wrapper around the BlueZ HID service."""

    # Send only report-mode keyboard by default (tvOS/iOS subs to report input).
    SEND_BOTH_KB = False

    def __init__(self, *, adapter: str, device_name: str, debug: bool = False) -> None:
        self._adapter = adapter
        self._device_name = device_name
        self._debug = debug

        self._shutdown: Optional[Callable[[], asyncio.Future]] = None
        self._hid_service = None  # set in start()
        self._runtime: Optional[_hd.HidRuntime] = None

    async def start(self) -> None:
        """Bring the HID service online."""

        if self._runtime is not None:
            return

        cfg = SimpleNamespace(adapter_name=self._adapter, device_name=self._device_name, appearance=0x03C1)
        # bring up BlueZ + GATT app (HID/BAS/DIS). start_hid() must set _hid_service_singleton.
        runtime, shutdown = await _hd.start_hid(cfg)
        self._shutdown = shutdown
        self._runtime = runtime

        # pull service AFTER start_hid()
        self._hid_service = getattr(_hd, "_hid_service_singleton", None)
        if self._hid_service is None:
            raise RuntimeError(
                "HID service not available after start_hid(); "
                "ensure start_hid() sets _hid_service_singleton = hid"
            )

        if self._debug:
            print(f'[hid] advertising registered as "{self._device_name}" on "{self._adapter}"', flush=True)

    async def stop(self) -> None:
        """Tear down the HID service."""

        if self._shutdown is None:
            return

        res = self._shutdown()
        if asyncio.iscoroutine(res):
            await res
        self._shutdown = None
        self._hid_service = None
        self._runtime = None

    @property
    def runtime(self) -> Optional[_hd.HidRuntime]:
        """Expose the most recent runtime returned by :func:`start_hid`."""

        return self._runtime

    async def wait_for_critical_failure(self, stop_event: Optional[asyncio.Event] = None) -> str:
        """Block until the transport should be restarted.

        This listens for either a caller requested stop, a DBus disconnect or the
        adapter losing power.  Returns a short string describing the trigger.
        """

        runtime = self._runtime
        if runtime is None:
            return "not-started"

        watchers: list[asyncio.Task[str]] = []

        if stop_event is not None:
            watchers.append(asyncio.create_task(self._wait_stop(stop_event)))

        if getattr(runtime, "bus", None) is not None:
            watchers.append(asyncio.create_task(self._watch_bus(runtime)))

        if getattr(runtime, "adapter", None) is not None:
            watchers.append(asyncio.create_task(self._watch_adapter_power(runtime)))

        if not watchers:
            return "no-runtime"

        done, pending = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        result = "unknown"
        for task in done:
            with contextlib.suppress(Exception):
                result = task.result()

        return result

    async def _wait_stop(self, stop_event: asyncio.Event) -> str:
        await stop_event.wait()
        return "requested"

    async def _watch_bus(self, runtime: _hd.HidRuntime) -> str:
        try:
            await runtime.bus.wait_for_disconnect()
            return "bus-disconnect"
        except asyncio.CancelledError:
            raise
        except Exception:
            return "bus-error"

    async def _watch_adapter_power(self, runtime: _hd.HidRuntime, poll_interval: float = 1.0) -> str:
        try:
            adapter = runtime.adapter
            while True:
                powered = await adapter.get_powered()
                if not powered:
                    return "adapter-power"
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "adapter-error"

    # --- notifications used by HIDClient -------------------------------------

    def notify_keyboard(self, report: bytes) -> None:
        """
        Send an 8-byte keyboard report.
        Primary: Report-mode Keyboard Input (0x2A4D). RID is provided via Report Ref descriptor.
        Optional: Boot Keyboard Input (0x2A22) if SEND_BOTH_KB=True.
        """
        svc = self._hid_service
        if not svc:
            return

        rep = getattr(svc, "input_keyboard", None)
        if rep is not None and hasattr(rep, "changed"):
            try:
                rep.changed(report)
                if self._debug:
                    print("[bt] keyboard report changed", flush=True)
            except Exception as e:
                if self._debug:
                    print(f"[bt] keyboard report changed error: {e}", flush=True)

        if self.SEND_BOTH_KB:
            boot = getattr(svc, "boot_keyboard_input", None)
            if boot is not None and hasattr(boot, "changed"):
                try:
                    boot.changed(report)
                    if self._debug:
                        print("[bt] keyboard boot changed", flush=True)
                except Exception as e:
                    if self._debug:
                        print(f"[bt] keyboard boot changed error: {e}", flush=True)

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        """
        Send 2-byte Consumer Control usage via Report-mode Consumer Input (0x2A4D, RID=2).
        """
        svc = self._hid_service
        if not svc:
            return

        payload = (usage_id if pressed else 0).to_bytes(2, "little")
        cons = getattr(svc, "input_consumer", None)
        if cons is not None and hasattr(cons, "changed"):
            try:
                cons.changed(payload)
                if self._debug:
                    edge = "down" if pressed else "up"
                    print(f"[bt] consumer changed 0x{usage_id:04X} {edge}", flush=True)
            except Exception as e:
                if self._debug:
                    print(f"[bt] consumer changed error: {e}", flush=True)


class BTLEController:
    """High-level BLE orchestrator with automatic recovery."""

    def __init__(self, *, adapter: str, device_name: str, debug: bool = False) -> None:
        self._tx = HIDTransportBLE(adapter=adapter, device_name=device_name, debug=debug)
        self._client = HIDClient(hid=self._tx, debug=debug)
        self._available = False
        self._debug = debug

        self._stop_event = asyncio.Event()
        self._ready = asyncio.Event()
        self._runner: Optional[asyncio.Task[None]] = None

    @property
    def available(self) -> bool:
        """Return True when the transport is currently connected and ready."""

        return self._available

    async def start(self) -> None:
        """Start supervising the BLE transport."""

        if self._runner is not None:
            return

        self._stop_event.clear()
        self._ready.clear()
        self._runner = asyncio.create_task(self._run(), name="btle-supervisor")

        # Give the supervisor a chance to perform the first connection attempt.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if self._debug:
                print("[bt] BLE controller still waiting for adapter", flush=True)

    async def stop(self) -> None:
        """Stop the supervisor and tear down the transport."""

        self._stop_event.set()

        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

        await self._tx.stop()
        self._available = False
        self._ready.clear()

    async def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """Block until the controller reports availability.

        Returns False if the timeout expires.  A ``None`` timeout waits forever.
        """

        if timeout is None:
            await self._ready.wait()
            return True

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        backoff = 1.0
        try:
            while not self._stop_event.is_set():
                try:
                    await self._tx.start()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._available = False
                    print(f"[bt] transport start failed: {exc}", flush=True)
                    await self._sleep_with_stop(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    continue

                self._available = True
                self._ready.set()
                backoff = 1.0

                reason = await self._tx.wait_for_critical_failure(self._stop_event)

                self._available = False
                self._ready.clear()
                await self._tx.stop()

                if reason == "requested" or self._stop_event.is_set():
                    break

                delay = backoff
                backoff = min(backoff * 2.0, 30.0)
                print(f"[bt] restarting after {reason}; retry in {delay:.1f}s", flush=True)
                await self._sleep_with_stop(delay)
        finally:
            self._available = False
            self._ready.clear()

    async def _sleep_with_stop(self, timeout: float) -> None:
        """Sleep for ``timeout`` seconds but abort quickly if stopping."""

        if timeout <= 0:
            return

        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    # Edge-level passthroughs used by Dispatcher for true key down/up
    def key_down(self, *, usage: str, code: str) -> None:
        """Forward a key-down edge if the transport is ready."""
        if not self._available:
            return
        self._client.key_down(usage=usage, code=code)

    def key_up(self, *, usage: str, code: str) -> None:
        """Forward a key-up edge if the transport is ready."""
        if not self._available:
            return
        self._client.key_up(usage=usage, code=code)

    # Tap (used by WS/macros)
    async def send_key(self, *, usage: str, code: str, hold_ms: int = 40) -> None:
        """Tap a key when the transport is available."""
        if not self._available:
            return
        await self._client.send_key(usage=usage, code=code, hold_ms=hold_ms)

    async def run_macro(
        self,
        steps: List[Dict[str, Any]],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        """Run a macro through the BLE client if available."""
        if not self._available:
            return
        await self._client.run_macro(steps, default_hold_ms=default_hold_ms, inter_delay_ms=inter_delay_ms)
