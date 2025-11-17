"""Translate symbolic key names into HID payloads."""
from __future__ import annotations

import os
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

Usage = Literal["keyboard", "consumer"]

class HIDClient:
    """Encode symbolic keys to HID payloads and forward to the transport."""
    def __init__(self, *, hid, debug: bool = False) -> None:
        self._hid = hid
        self._debug = debug or (os.getenv("DEBUG_BT", "0").lower() in {"1","true","yes","on"})
        self._kb, self._cc = self._load_hid_tables()

    # ---------- edge-level API ----------
    def key_down(self, *, usage: Usage, code: str) -> None:
        """Send a logical key-down edge."""
        if usage == "keyboard":
            down = self._encode_keyboard_down(code)
            if self._debug:
                print(f'[bt] keyboard "{code}" down')
            self._hid.notify_keyboard(down)
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if self._debug:
                print(f'[bt] consumer "{code}" down (0x{usage_id:04X})')
            if usage_id:
                self._hid.notify_consumer(usage_id, True)

    def key_up(self, *, usage: Usage, code: str) -> None:
        """Send a logical key-up edge."""
        if usage == "keyboard":
            if self._debug:
                print(f'[bt] keyboard "{code}" up')
            self._hid.notify_keyboard(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        elif usage == "consumer":
            usage_id = self._encode_consumer_usage(code)
            if self._debug:
                print(f'[bt] consumer "{code}" up (0x{usage_id:04X})')
            if usage_id:
                self._hid.notify_consumer(usage_id, False)

    # ---------- tap API (macros/WS) ----------
    async def send_key(self, *, usage: Usage, code: str, hold_ms: int = 40) -> None:
        """Tap a key by sending down → delay → up."""
        # tap = down + delay + up
        self.key_down(usage=usage, code=code)
        await asyncio.sleep(max(0, hold_ms) / 1000.0)
        self.key_up(usage=usage, code=code)

    async def run_macro(
        self,
        steps: List[Dict[str, Any]],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        """Execute a timed macro sequence."""
        for step in steps:
            if "wait_ms" in step:  # idle delay
                await asyncio.sleep(max(0, int(step["wait_ms"])) / 1000.0)
                continue
            usage = step.get("usage")
            code = step.get("code")
            hold = int(step.get("hold_ms", default_hold_ms))
            if isinstance(usage, str) and isinstance(code, str):
                await self.send_key(usage=usage, code=code, hold_ms=hold)
                await asyncio.sleep(max(0, inter_delay_ms) / 1000.0)

    # ---------- internals ----------
    def _load_hid_tables(self) -> Tuple[Dict[str, int], Dict[str, int]]:
        base = Path(__file__).resolve().parent
        yml = base / "hid_keymap.yaml"
        jsn = base / "hid_keymap.json"

        data: Dict[str, Any] = {}
        if yml.is_file():
            try:
                import yaml
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        elif jsn.is_file():
            try:
                data = json.loads(jsn.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}

        kb = {k: int(v) for k, v in (data.get("keyboard") or {}).items()}
        cc = {k: int(v) for k, v in (data.get("consumer") or {}).items()}
        return kb, cc

    def _encode_keyboard_down(self, code: str) -> bytes:
        hid = self._kb.get(code)
        if hid is None:
            return b"\x00\x00\x00\x00\x00\x00\x00\x00"
        # Boot Keyboard 8-byte: mods(1), reserved(1), key1..key6
        return bytes([0x00, 0x00, hid, 0x00, 0x00, 0x00, 0x00, 0x00])

    def _encode_consumer_usage(self, code: str) -> int:
        return int(self._cc.get(code) or 0)
