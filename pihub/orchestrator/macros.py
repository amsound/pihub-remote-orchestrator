
from __future__ import annotations
import asyncio
from typing import Optional
from ..bt_le.controller import BTLEController
from ..macros import MACROS

class AppleTVMacros:
    def __init__(self, controller: BTLEController) -> None:
        self._bt = controller

    async def fire(self, name: str) -> None:
        seq = MACROS.get(name)
        if not seq:
            return
        await self._bt.play_macro(seq, hold_ms_default=40)

    async def power_on(self) -> None:
        await self.fire("power_on")

    async def power_off(self) -> None:
        await self.fire("power_off")
