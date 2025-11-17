#!/usr/bin/env python3
"""BlueZ HID service definitions and lifecycle helpers."""

import asyncio
import os
import contextlib
import inspect
import time

from bluez_peripheral.util import get_message_bus, Adapter, is_bluez_available
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.gatt.service import Service, ServiceCollection
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.descriptor import DescriptorFlags as DescFlags

from dbus_fast.constants import MessageType
from dbus_fast import Variant
from dataclasses import dataclass

_hid_service_singleton = None  # set inside start_hid()

# --------------------------
# Device identity / advert
# --------------------------

APPEARANCE   = 0x03C1  # Keyboard

# Report IDs
RID_KEYBOARD = 0x01
RID_CONSUMER = 0x02

# --------------------------
# HID Report Map (Keyboard + Consumer bitfield)
# --------------------------
REPORT_MAP = bytes([
    # Keyboard (Boot) – Report ID 1
    0x05,0x01, 0x09,0x06, 0xA1,0x01,
      0x85,0x01,              # REPORT_ID (1)
      0x05,0x07,              #   USAGE_PAGE (Keyboard)
      0x19,0xE0, 0x29,0xE7,   #   USAGE_MIN/MAX (modifiers)
      0x15,0x00, 0x25,0x01,   #   LOGICAL_MIN 0 / MAX 1
      0x75,0x01, 0x95,0x08,   #   REPORT_SIZE 1, COUNT 8  (mod bits)
      0x81,0x02,              #   INPUT (Data,Var,Abs)
      0x95,0x01, 0x75,0x08,   #   reserved byte
      0x81,0x01,              #   INPUT (Const,Array,Abs)
      0x95,0x06, 0x75,0x08,   #   6 keys
      0x15,0x00, 0x25,0x65,   #   key range 0..0x65
      0x19,0x00, 0x29,0x65,   #   USAGE_MIN/MAX (keys)
      0x81,0x00,              #   INPUT (Data,Array,Abs)
    0xC0,

    # Consumer Control – 16‑bit *array* usage (Report ID 2) — 2‑byte value
    0x05,0x0C, 0x09,0x01, 0xA1,0x01,
      0x85,0x02,              # REPORT_ID (2)
      0x15,0x00,              # LOGICAL_MIN 0
      0x26,0xFF,0x03,         # LOGICAL_MAX 0x03FF
      0x19,0x00,              # USAGE_MIN 0x0000
      0x2A,0xFF,0x03,         # USAGE_MAX 0x03FF
      0x75,0x10,              # REPORT_SIZE 16
      0x95,0x01,              # REPORT_COUNT 1 (one slot)
      0x81,0x00,              # INPUT (Data,Array,Abs)
    0xC0,
])
    
async def _adv_unregister(bus, advert) -> bool:
    """
    Unregister/stop advertising. Returns True if we likely did anything.
    Only logs on error.
    """
    try:
        if hasattr(advert, "unregister"):
            sig = inspect.signature(advert.unregister)
            if "bus" in sig.parameters:
                await advert.unregister(bus)
            else:
                await advert.unregister()
            return True
        if hasattr(advert, "stop"):
            await advert.stop()
            return True
        return False
    except Exception as e:
        print(f"[hid] adv unregister error: {e!r}")
        return False

async def _adv_register_and_start(bus, advert) -> str:
    """
    (Re)register (+ start if supported). Returns a short mode label:
    'registered+started', 'registered', or 'noop'. Only logs on error.
    """
    try:
        did_register = False
        if hasattr(advert, "register"):
            sig = inspect.signature(advert.register)
            if "bus" in sig.parameters:
                await advert.register(bus)
            else:
                await advert.register()
            did_register = True

        if hasattr(advert, "start"):
            await advert.start()
            return "registered+started" if did_register else "started"

        return "registered" if did_register else "noop"
    except Exception as e:
        print(f"[hid] adv register/start error: {e!r}")
        return "error"

# --------------------------
# BlueZ object manager helpers
# --------------------------
def _get_bool(v):  # unwrap dbus_next.Variant or use raw bool
    return bool(v.value) if isinstance(v, Variant) else bool(v)

async def trust_device(bus, device_path):
    """Set org.bluez.Device1.Trusted = True for the connected peer."""
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
    except Exception:
        pass
        
async def _get_managed_objects(bus):
    root_xml = await bus.introspect("org.bluez", "/")
    root = bus.get_proxy_object("org.bluez", "/", root_xml)
    om = root.get_interface("org.freedesktop.DBus.ObjectManager")
    return await om.call_get_managed_objects()

async def wait_for_any_connection(bus, poll_interval=0.25):
    """Return Device1 path once Connected=True (we then wait for ServicesResolved)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    # quick poll
    objs = await _get_managed_objects(bus)
    for path, props in objs.items():
        dev = props.get("org.bluez.Device1")
        if dev and _get_bool(dev.get("Connected", False)):
            return path

    def handler(msg):
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded":
            obj_path, ifaces = msg.body
            dev = ifaces.get("org.bluez.Device1")
            if dev and _get_bool(dev.get("Connected", False)) and not fut.done():
                fut.set_result(obj_path)
        elif msg.member == "PropertiesChanged" and msg.path:
            iface, changed, _ = msg.body
            if iface == "org.bluez.Device1" and "Connected" in changed and _get_bool(changed["Connected"]) and not fut.done():
                fut.set_result(msg.path)

    bus.add_message_handler(handler)
    try:
        while True:
            if fut.done():
                return await fut
            # polling fallback
            objs = await _get_managed_objects(bus)
            for path, props in objs.items():
                dev = props.get("org.bluez.Device1")
                if dev and _get_bool(dev.get("Connected", False)):
                    return path
            await asyncio.sleep(poll_interval)
    finally:
        bus.remove_message_handler(handler)

async def wait_until_services_resolved(bus, device_path, timeout_s=30, poll_interval=0.25):
    """Wait for Device1.ServicesResolved == True for this device."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        objs = await _get_managed_objects(bus)
        dev = objs.get(device_path, {}).get("org.bluez.Device1")
        if dev and _get_bool(dev.get("ServicesResolved", False)):
            return True
        await asyncio.sleep(poll_interval)
    return False

async def wait_for_disconnect(bus, device_path, poll_interval=0.5):
    """Block until this device disconnects."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def handler(msg):
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member != "PropertiesChanged" or msg.path != device_path:
            return
        iface, changed, _ = msg.body
        if iface == "org.bluez.Device1" and "Connected" in changed and not _get_bool(changed["Connected"]) and not fut.done():
            fut.set_result(None)

    bus.add_message_handler(handler)
    try:
        while True:
            if fut.done():
                await fut
                return
            objs = await _get_managed_objects(bus)
            dev = objs.get(device_path, {}).get("org.bluez.Device1")
            if not dev or not _get_bool(dev.get("Connected", False)):
                return
            await asyncio.sleep(poll_interval)
    finally:
        bus.remove_message_handler(handler)
        
async def _get_device_alias_or_name(bus, device_path) -> str:
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

        alias = await props.call_get("org.bluez.Device1", "Alias")
        name  = await props.call_get("org.bluez.Device1", "Name")

        def _unwrap(v): 
            from dbus_fast import Variant
            return v.value if isinstance(v, Variant) else v

        return _unwrap(alias) or _unwrap(name) or ""
    except Exception:
        return ""

async def watch_link(bus, advert, hid: "HIDService"):
    """
    Gate _link_ready when client actually enables notifications on any HID input.
    Also: unregister adverts on connect; re-register on disconnect.
    """
    import time, contextlib
    while True:
        dev_path = await wait_for_any_connection(bus)
        with contextlib.suppress(Exception):
            await trust_device(bus, dev_path)
        human = await _get_device_alias_or_name(bus, dev_path)
        label = human if human else dev_path
        print(f"[hid] connected: {label}")


        # Services resolved first
        await wait_until_services_resolved(bus, dev_path, timeout_s=30)

        # Wait up to ~3s for CCCDs to be enabled on any HID input char
        deadline = time.monotonic() + 3.0
        kb = boot = cc = False
        while time.monotonic() < deadline:
            try:
                kb, boot, cc = hid._notif_state()  # (keyboard, boot-kb, consumer)
            except Exception:
                kb = boot = cc = False
            if kb or boot or cc:
                break
            await asyncio.sleep(0.10)

        # Link ready: host subscribed to at least one input
        hid._link_ready = True
        print(f"[hid] link ready — notify: kb={kb} boot={boot} cc={cc}")

        # Stop advertising while connected
        if await _adv_unregister(bus, advert):
            print("[hid] advertising unregistered (connected device)")

        # Block here until disconnect
        await wait_for_disconnect(bus, dev_path)

        # Link down
        hid._link_ready = False
        print("[hid] disconnected")

        # Re-register (and start) advertising for next client
        mode = await _adv_register_and_start(bus, advert)
        if mode not in ("noop", "error"):
            print("[hid] advertising registered (no device)")

# --------------------------
# GATT Services
# --------------------------
# --- Battery Service (0x180F) ---
class BatteryService(Service):
    def __init__(self, initial_level: int = 100):
        super().__init__("180F", True)
        lvl = max(0, min(100, int(initial_level)))
        self._level = bytearray([lvl])

    @characteristic("2A19", CharFlags.READ | CharFlags.NOTIFY)
    def battery_level(self, _):
        # 0..100
        return bytes(self._level)

    # Convenience: call this to update + notify
    def set_level(self, pct: int):
        pct = max(0, min(100, int(pct)))
        if self._level[0] != pct:
            self._level[0] = pct
            try:
                self.battery_level.changed(bytes(self._level))
            except Exception:
                pass

class DeviceInfoService(Service):
    def __init__(self, manufacturer="PiKB Labs", model="PiKB-1", vid=0xFFFF, pid=0x0001, ver=0x0100):
        super().__init__("180A", True)
        self._mfg   = manufacturer.encode("utf-8")
        self._model = model.encode("utf-8")
        self._pnp   = bytes([0x02, vid & 0xFF, (vid>>8)&0xFF, pid & 0xFF, (pid>>8)&0xFF, ver & 0xFF, (ver>>8)&0xFF])

    @characteristic("2A29", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def manufacturer_name(self, _):
        return self._mfg

    @characteristic("2A24", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def model_number(self, _):
        return self._model

    @characteristic("2A50", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def pnp_id(self, _):
        return self._pnp

class HIDService(Service):
    def __init__(self):
        super().__init__("1812", True)
        self._proto = bytearray([1])  # Report Protocol
        self._link_ready: bool = False

    # -------- subscription helpers --------
    def _is_subscribed(self, char) -> bool:
        # Supports both property and method styles found in bluez_peripheral
        for attr in ("is_notifying", "notifying"):
            if hasattr(char, attr):
                v = getattr(char, attr)
                return v() if callable(v) else bool(v)
        # If the library doesn’t expose state, assume subscribed
        return True

    def _notif_state(self) -> tuple[bool, bool, bool]:
        kb   = self._is_subscribed(self.input_keyboard)
        boot = self._is_subscribed(self.boot_keyboard_input)
        cc   = self._is_subscribed(self.input_consumer)
        return (kb, boot, cc)

    # ---------------- GATT Characteristics ----------------
    # Protocol Mode (2A4E): READ/WRITE (encrypted both)
    @characteristic("2A4E", CharFlags.READ | CharFlags.WRITE | CharFlags.ENCRYPT_READ | CharFlags.ENCRYPT_WRITE)
    def protocol_mode(self, _):
        return bytes(self._proto)
    @protocol_mode.setter
    def protocol_mode_set(self, value, _):
        self._proto[:] = value

    # HID Information (2A4A): READ (encrypted)
    @characteristic("2A4A", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def hid_info(self, _):
        return bytes([0x11, 0x01, 0x00, 0x03])  # bcdHID=0x0111, country=0, flags=0x03

    # HID Control Point (2A4C): WRITE (encrypted)
    @characteristic("2A4C", CharFlags.WRITE | CharFlags.WRITE_WITHOUT_RESPONSE | CharFlags.ENCRYPT_WRITE)
    def hid_cp(self, _):
        return b""
    @hid_cp.setter
    def hid_cp_set(self, _value, _):
        pass

    # Report Map (2A4B): READ (encrypted)
    @characteristic("2A4B", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def report_map(self, _):
        return REPORT_MAP

    # Keyboard input (Report-mode, RID 1) — 8-byte payload
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_keyboard(self, _):
        return bytes([0,0,0,0,0,0,0,0])
    @input_keyboard.descriptor("2908", DescFlags.READ)
    def input_keyboard_ref(self, _):
        return bytes([RID_KEYBOARD, 0x01])

    # Consumer input (RID 2) — 2-byte payload (16-bit usage)
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_consumer(self, _):
        return bytes([0,0])
    @input_consumer.descriptor("2908", DescFlags.READ)
    def input_consumer_ref(self, _):
        return bytes([RID_CONSUMER, 0x01])

    # Boot Keyboard Input (2A22) — 8-byte payload (no report ID)
    @characteristic("2A22", CharFlags.READ | CharFlags.NOTIFY)
    def boot_keyboard_input(self, _):
        return bytes([0,0,0,0,0,0,0,0])

    # ---------------- Send helpers ---------------- 
    @staticmethod
    def _kb_payload(keys=(), modifiers=0) -> bytes:
        keys = list(keys)[:6] + [0] * (6 - len(keys))
        return bytes([modifiers, 0] + keys)  # 8-byte boot/report keyboard frame
    
    def send_keyboard(self, payload: bytes) -> None:
        if not self._link_ready:
            return
        try:
            # Protocol Mode: 0x01 = Report (default), 0x00 = Boot
            if getattr(self, "_proto", b"\x01")[0] == 0x01:
                self.input_keyboard.changed(payload)        # Report
            else:
                self.boot_keyboard_input.changed(payload)   # Boot
        except Exception:
            pass
    
    def send_consumer(self, payload: bytes) -> None:
        if not self._link_ready:
            return
        try:
            self.input_consumer.changed(payload)
        except Exception:
            pass

    async def key_tap(self, usage, hold_ms=40, modifiers=0):
        down = self._kb_payload([usage], modifiers)
        self.send_keyboard(down)
        await asyncio.sleep(hold_ms / 1000)
        up = self._kb_payload([], 0)
        self.send_keyboard(up)

    def cc_payload_usage(self, usage_id: int) -> bytes:
        return bytes([usage_id & 0xFF, (usage_id >> 8) & 0xFF])

    async def consumer_tap(self, usage_id, hold_ms=60):
        self.send_consumer(self.cc_payload_usage(usage_id))
        await asyncio.sleep(hold_ms/1000)
        self.send_consumer(self.cc_payload_usage(0))

    def release_all(self):
        self.send_keyboard(self._kb_payload([], 0))
        self.send_consumer(self.cc_payload_usage(0))

@dataclass
class HidRuntime:
    bus: any
    adapter: any
    advert: any
    hid: any
    tasks: list

async def start_hid(config) -> tuple[HidRuntime, callable]:
    """
    Start the BLE HID server. Returns (runtime, shutdown) where shutdown is an async callable.
    - config.device_name   : BLE local name (string)
    - config.appearance    : GAP appearance (int, default 0x03C1)
    """
    device_name = getattr(config, "device_name", None) or os.uname().nodename
    appearance  = int(getattr(config, "appearance", APPEARANCE))

    bus = await get_message_bus()
    if not await is_bluez_available(bus):
        raise RuntimeError("BlueZ not available on system DBus.")

    # Adapter
    adapter_name = getattr(config, "adapter_name", getattr(config, "adapter", "hci0"))
    try:
        xml = await bus.introspect("org.bluez", f"/org/bluez/{adapter_name}")
    except Exception as exc:
        raise RuntimeError(f"Bluetooth adapter {adapter_name} not found") from exc

    proxy = bus.get_proxy_object("org.bluez", f"/org/bluez/{adapter_name}", xml)
    adapter = Adapter(proxy)
    await adapter.set_alias(device_name)

    # Agent
    agent = NoIoAgent()
    await agent.register(bus, default=True)
    
    # Services
    dis = DeviceInfoService()
    bas = BatteryService(initial_level=100)
    hid = HIDService()
    
    global _hid_service_singleton
    _hid_service_singleton = hid

    app = ServiceCollection()
    app.add_service(dis)
    app.add_service(bas)
    app.add_service(hid)
    
    async def _power_cycle_adapter():
        try:
            await adapter.set_powered(False)
            await asyncio.sleep(0.4)
            await adapter.set_powered(True)
            await asyncio.sleep(0.8)
        except Exception as e:
            print(f"[hid] Bluetooth adapter power-cycle failed: {e}")
    
    # --- Register GATT application (with one retry) ---
    try:
        await app.register(bus, adapter=adapter)
    except Exception as e:
        print(f"[hid] BTLE service register failed: {e} — retrying after power-cycle")
        await _power_cycle_adapter()
        try:
            await app.register(bus, adapter=adapter)
        except Exception as e2:
            # Hard fail: do not return None
            raise RuntimeError(f"GATT application register failed after retry: {e2}") from e2
    
    # --- Register + start advertising (with one retry) ---
    advert = Advertisement(
        localName=device_name,
        serviceUUIDs=["1812", "180F", "180A"],
        appearance=appearance,
        timeout=0,
        discoverable=True,
    )
    mode = await _adv_register_and_start(bus, advert)
    if mode in ("error", "noop"):
        print(f"[hid] advert register/start failed ({mode}) — retrying after power-cycle")
        with contextlib.suppress(Exception):
            await advert.unregister()
        await _power_cycle_adapter()
    
        # Recreate a fresh advert object (fresh DBus path)
        advert = Advertisement(
            localName=device_name,
            serviceUUIDs=["1812", "180F", "180A"],
            appearance=appearance,
            timeout=0,
            discoverable=True,
        )
        mode = await _adv_register_and_start(bus, advert)
        if mode in ("error", "noop"):
            with contextlib.suppress(Exception):
                await app.unregister()
            raise RuntimeError(f"Advertising register failed after retry: mode={mode}")
    
    print(f'[hid] advertising registered as {device_name} on {adapter_name}')
    
    # Start link watcher (flips _link_ready and prints concise pairing log)
    link_task = asyncio.create_task(watch_link(bus, advert, hid), name="hid_watch_link")
    tasks = [link_task]
    
    # Make sure nothing is logically "held" at startup
    with contextlib.suppress(Exception):
        hid.release_all()
    
    async def shutdown():
        # stop watcher(s)
        for t in list(tasks):
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        # Unregister advert (use your helper)
        with contextlib.suppress(Exception):
            await _adv_unregister(bus, advert)
        # Unregister services
        with contextlib.suppress(Exception):
            await app.unregister()
        # Release any held keys
        with contextlib.suppress(Exception):
            hid.release_all()
        hid._link_ready = False
    
    runtime = HidRuntime(bus=bus, adapter=adapter, advert=advert, hid=hid, tasks=tasks)
    return runtime, shutdown
