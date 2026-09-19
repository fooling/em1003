"""A fake BLE world for testing the EM1003 integration without real hardware.

This module is owned by the test-scaffold author (see
``docs/ble_session_refactor.md`` section 11, "File ownership"). Other test
files (``test_device_session.py``, ``test_circuit_breaker.py``,
``test_protocol.py``, ``test_coordinator_entities.py``, ``test_init_setup.py``,
``test_config_flow.py``, ``test_backward_compat.py``) consume it through the
fixtures in ``tests/conftest.py`` — read that module's docstring too, it wires
this one into ``hass``.

--------------------------------------------------------------------------
Why this exists
--------------------------------------------------------------------------
``custom_components.em1003.device`` (and, in the not-yet-refactored code,
``custom_components.em1003`` itself) talks to the real device through two
choke points:

* ``establish_connection(BleakClient, ble_device, mac, ...)`` from
  ``bleak_retry_connector`` — opens a link and returns a ``BleakClient``.
* ``bluetooth.async_ble_device_from_address(hass, mac, connectable=True)``
  (and, for the refactored code, ``bluetooth.async_last_service_info`` /
  ``bluetooth.async_discovered_service_info``) — HA's BLE device lookup.

``FakeBleWorld`` replaces both. It never touches real Bluetooth: it hands
back ``FakeBleakClient`` instances that speak the EM1003 wire protocol
(described in ``docs/protocol_reverse_engineering.md`` and implemented in
``device.py``'s ``_notification_handler``):

* write ``[seq, 0x06, sensor_id]``            -> notify ``[seq, 0x06, sensor_id, lo, hi]``
* write ``[seq, 0x50, 0x00]``                 -> notify ``[seq, 0x50, 0x00, state]``
* write ``[seq, 0x50, 0x01, state]``          -> notify ``[seq, 0x05, state]`` (ack) and
                                                  updates the world's buzzer truth

--------------------------------------------------------------------------
Quick start (most tests want the ``ble_world`` fixture from conftest.py,
not this module directly)
--------------------------------------------------------------------------
    async def test_something(hass, ble_world, mock_entry, setup_integration):
        ble_world.set_sensor_value(0x01, 21.5)   # temperature, engineering units
        entry = mock_entry()
        await setup_integration(hass, entry)
        assert ble_world.connect_attempts >= 1
        assert ble_world.links_are_clean

Using ``FakeBleWorld`` directly (e.g. to unit test ``device.py`` without
going through HA setup) — see also the ``ble_world`` fixture, which does
exactly this patching for you:

    world = FakeBleWorld()
    monkeypatch.setattr(
        "custom_components.em1003.device.establish_connection",
        world.establish_connection,
    )
    monkeypatch.setattr(
        "custom_components.em1003.device.bluetooth.async_ble_device_from_address",
        world.async_ble_device_from_address,
    )
    device = EM1003Device(hass, "AA:BB:CC:DD:EE:FF")
    data = await device.read_all_sensors()

--------------------------------------------------------------------------
Configuration knobs (set as plain attributes on the ``FakeBleWorld``
instance, any time before or during a test — nothing needs to be passed to
the constructor)
--------------------------------------------------------------------------
Sensor / buzzer data returned to the device under test:

* ``world.set_sensor_value(sensor_id, engineering_value)`` — set what a
  sensor reports, in the *engineering* unit device.py returns (e.g.
  ``world.set_sensor_value(0x01, 21.5)`` for 21.5 degrees). Inverts the
  formulas in ``device.py`` for you.
* ``world.set_sensor_raw(sensor_id, raw_uint16)`` — set the raw wire value
  directly instead (useful for testing formula edge cases, e.g. the
  0xFFFF boundary, or an intentionally-negative-after-formula raw value).
* ``world.sensor_raw`` — dict[int, int], the raw values actually used; all
  8 ids from ``SENSOR_TYPES`` are pre-populated with plausible positive
  defaults so a fresh world "just works" without configuration.
* ``world.silent_sensors`` — set[int] of sensor ids that never answer a
  query (the request is written, recorded, but no notification ever
  arrives — exercises the response-timeout path).
* ``world.buzzer_state`` — bool, the device's current buzzer truth.
* ``world.buzzer_query_silent`` / ``world.buzzer_set_silent`` — bool, make
  the buzzer query or the buzzer SET ack never arrive.
* ``world.apply_buzzer_set`` — bool (default True). When False, a SET
  write is acked normally but the world's ``buzzer_state`` is left
  unchanged — simulates the device accepting the command but not actually
  flipping state, so the caller's verification query reports a mismatch.
* ``world.device_name`` — str, returned by a Device Name (0x2A00)
  characteristic read.
* ``world.device_present`` — bool (default True). False makes
  ``async_ble_device_from_address`` return ``None`` and
  ``async_last_service_info`` return ``None`` — the "device not found /
  not currently advertising" case. ``establish_connection`` is not even
  reached by correctly-written code in this case, but will itself raise
  ``BleakError`` defensively if it somehow is.
* ``world.rssi`` — int, used by the ``async_last_service_info`` fake.
* ``world.discovered`` — list you populate with objects exposing
  ``.address`` / ``.name`` / ``.rssi``, returned by
  ``async_discovered_service_info`` (used by config-flow / scan_device
  tests). Empty by default.
* ``world.characteristics`` — dict[str, bytes] of extra
  characteristic-uuid -> value pairs for ``read_gatt_char`` (the Device
  Name characteristic is served from ``world.device_name`` automatically
  unless you also put an entry for it here).

Timing:

* ``world.response_delay`` — float seconds, added before every
  notification is delivered (default 0.0 — instant).
* ``world.extra_delay_for`` — dict[int, float] overriding
  ``response_delay`` for one specific request key (sensor id, or ``0x00``
  for the buzzer query / ``0x01`` for the buzzer SET ack).

Fault injection — five named stages: ``"connect"``, ``"start_notify"``,
``"write"``, ``"stop_notify"``, ``"disconnect"`` (plus ``"read_char"`` for
``read_gatt_char``, used by the device-name read and the debug services):

* ``world.hang_at`` — set[str] of stages that hang forever (``await``
  never returns) *every* time that stage is entered. Use for "the fake
  never comes back from connect" style tests. Cancelling the awaiting
  task works correctly (the hang is a plain ``asyncio.Event().wait()``).
* ``world.hang_once`` — set[str], like ``hang_at`` but only for the *next*
  call at that stage; it removes itself afterwards so the following call
  behaves normally. Use for "the first write hangs, the retry works".
* ``world.raise_at`` / ``world.raise_once`` — dict[str, exception-or-class]
  analogous to the two above, but raise instead of hanging. Pass a
  ``bleak.exc.BleakError`` (or subclass/instance) to simulate a BLE-layer
  failure, or any other exception class/instance for an unexpected error.
  A class is instantiated fresh each time; an instance is raised as-is.
* ``world.drop_after_writes`` — int | None. After this many writes have
  been sent *on one connection* (counting from 1), that connection drops
  unexpectedly right after its next scheduled response is delivered (or
  immediately, if that write's key doesn't get a response at all): the
  ``FakeBleakClient`` flips ``is_connected`` to False and calls the
  ``disconnected_callback`` that was passed to ``establish_connection``,
  exactly like a real mid-poll disconnect. Resets per new connection.
* ``world.garbage_notifications`` — list[bytes] fired once, right after
  ``start_notify`` succeeds on each new connection — simulates stray or
  malformed frames arriving unsolicited.
* ``world.inject(data, delay=0.0, client=None)`` — push an arbitrary raw
  notification into a live connection at any point in a test (defaults to
  the most recently opened still-open connection). Use this for
  wrong-seq / mismatched-sensor-id / garbage-frame tests, e.g.::

      ble_world.inject(bytes([0xEE, 0x06, 0x01, 0x2c, 0x1a]))

Accounting (read-only; inspect after/while exercising the device):

* ``world.connect_attempts`` — total ``establish_connection`` calls.
* ``world.max_concurrent_connects`` — the high-water mark of
  ``establish_connection`` calls in flight at once (must stay ``1`` once
  the device serialises with its lock — this is the D1 regression guard).
* ``world.open_links`` / ``world.max_open_links`` — currently-open and
  peak-simultaneously-open connections.
* ``world.total_links_opened`` / ``world.total_links_closed`` /
  ``world.total_links_dropped`` — lifetime counts (closed = graceful
  ``disconnect()``, dropped = ``drop_after_writes``-triggered).
* ``world.links_are_clean`` — ``True`` iff ``open_links == 0`` — the
  "nothing was leaked" one-liner tests want after every operation.
* ``world.writes`` — list[WriteRecord] in wire order, each with
  ``.client_id``, ``.char_uuid`` and ``.data`` (raw bytes as sent).
* ``world.disconnect_calls`` — dict[client_id, int], how many times
  ``.disconnect()`` was called on each connection (idle-disconnect and
  double-disconnect safety tests use this).
* ``world.clients`` — list[FakeBleakClient], every connection ever
  created, in order; ``.id`` is a small sequential int, stable and unique
  for the life of the world (use it to prove two calls reused, or did not
  reuse, "the same connection" / "the same device instance").

Module-level helpers usable without a ``FakeBleWorld`` at all:

* ``encode_raw(raw_uint16) -> bytes`` — little-endian 2-byte encoding.
* ``raw_for_engineering_value(sensor_id, value) -> int`` — inverts
  ``device.py``'s parse formula for one sensor id.
* ``build_frame(seq, cmd, *rest) -> bytes`` — ``bytes([seq, cmd, *rest])``,
  a tiny convenience for hand-building garbage/injected frames.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.em1003 import const as em1003_const

# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------

#: Fault-injection / accounting stage names this fake understands. Test
#: authors may use any of these as keys in ``hang_at`` / ``hang_once`` /
#: ``raise_at`` / ``raise_once``. Kept as a tuple (not enforced elsewhere)
#: so a future stage can be added without breaking existing tests.
HANG_STAGES = ("connect", "start_notify", "write", "stop_notify", "disconnect", "read_char")

#: Plausible positive default engineering values for every sensor id
#: currently in ``SENSOR_TYPES``. Anything added to ``SENSOR_TYPES`` later
#: that isn't listed here defaults to raw 0 — still valid, just less
#: realistic; tests that care should call ``set_sensor_value`` explicitly.
_DEFAULT_ENGINEERING_VALUES: dict[int, float] = {
    0x01: 25.3,     # temperature, °C
    0x06: 45.71,    # humidity, %
    0x08: 49,       # noise, dB
    0x09: 28,       # pm2.5, µg/m³
    0x0A: 0.05,     # formaldehyde, mg/m³
    0x11: 22,       # pm10, µg/m³
    0x12: 15,       # tvoc, µg/m³
    0x13: 411,      # eco2, ppm
}


def encode_raw(raw: int) -> bytes:
    """Encode a raw sensor value as the 2-byte little-endian wire format."""
    return int(raw).to_bytes(2, "little")


def raw_for_engineering_value(sensor_id: int, value: float | int) -> int:
    """Invert ``device.py``'s parse formula: engineering value -> raw uint16.

    Mirrors (as of this writing) the formulas documented in
    ``docs/ble_session_refactor.md`` section 4.1 / implemented in
    ``device.py``'s notification handler:

    * ``0x01`` (temperature): ``raw = round(value * 100) + 4000``
    * ``0x06`` (humidity):    ``raw = round(value * 100)``
    * ``0x0A`` (formaldehyde):``raw = round(value * 1000) + 16384``
    * everything else:        ``raw = round(value)`` (direct)

    Clamped to a valid uint16 (``0..0xFFFF``).
    """
    if sensor_id == em1003_const.SENSOR_ID_01:
        raw = round(value * 100) + 4000
    elif sensor_id == em1003_const.SENSOR_ID_06:
        raw = round(value * 100)
    elif sensor_id == em1003_const.SENSOR_ID_0A:
        raw = round(value * 1000) + 16384
    else:
        raw = round(value)
    return max(0, min(int(raw), 0xFFFF))


def build_frame(seq: int, cmd: int, *rest: int) -> bytes:
    """Build a raw notification frame: ``bytes([seq, cmd, *rest])``."""
    return bytes([seq, cmd, *rest])


def _materialize(exc: BaseException | type[BaseException], stage: str) -> BaseException:
    """Turn a ``raise_at``/``raise_once`` value into a raisable instance."""
    if isinstance(exc, type):
        return exc(f"fake BLE world: simulated failure at '{stage}'")
    return exc


def default_services(world: "FakeBleWorld") -> list["FakeService"]:
    """Build the default (fixed) GATT service table used by debug services.

    Includes the EM1003 write/notify characteristics and a standard
    Device Information service exposing the Device Name characteristic —
    enough for ``discover_all`` / ``list_services`` / ``read_characteristic``
    to have something real to walk without raising.
    """
    return [
        FakeService(
            uuid=em1003_const.EM1003_SERVICE_UUID,
            description="EM1003 sensor service",
            characteristics=(
                FakeCharacteristic(
                    uuid=em1003_const.EM1003_WRITE_CHAR_UUID,
                    description="EM1003 write",
                    properties=("write", "write-without-response"),
                    handle=0x10,
                ),
                FakeCharacteristic(
                    uuid=em1003_const.EM1003_NOTIFY_CHAR_UUID,
                    description="EM1003 notify",
                    properties=("notify",),
                    handle=0x11,
                ),
            ),
        ),
        FakeService(
            uuid="0000180a-0000-1000-8000-00805f9b34fb",
            description="Device Information",
            characteristics=(
                FakeCharacteristic(
                    uuid=em1003_const.DEVICE_NAME_UUID,
                    description="Device Name",
                    properties=("read",),
                    handle=0x02,
                ),
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Lightweight GATT-table stand-ins for the debug services
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeCharacteristic:
    """Duck-types ``bleak``'s ``BleakGATTCharacteristic`` far enough for the
    debug services (``discover_all`` / ``list_services``) to walk it."""

    uuid: str
    description: str = ""
    properties: tuple[str, ...] = ("read",)
    handle: int = 1
    descriptors: tuple[Any, ...] = ()


@dataclass(frozen=True)
class FakeService:
    """Duck-types one ``bleak`` GATT service."""

    uuid: str
    description: str = ""
    characteristics: tuple[FakeCharacteristic, ...] = ()


class FakeServiceCollection:
    """Duck-types ``bleak.backends.service.BleakGATTServiceCollection``.

    Iterable and indexable, and deliberately **without** ``__len__`` — the
    real bleak 2.x class defines only ``__iter__``/``__getitem__``, so
    ``len(client.services)`` raises ``TypeError`` against a real device.
    Handing tests a plain ``list`` here used to hide exactly that bug in
    the ``discover_all`` debug service.
    """

    def __init__(self, services: list[FakeService]) -> None:
        self._services = list(services)

    def __iter__(self):
        return iter(self._services)

    def __getitem__(self, item):
        return self._services[item]


@dataclass
class WriteRecord:
    """One recorded ``write_gatt_char`` call, in the order it happened."""

    index: int
    client_id: int
    char_uuid: str
    data: bytes


# ---------------------------------------------------------------------------
# FakeBleakClient
# ---------------------------------------------------------------------------


class FakeBleakClient:
    """Stand-in for a connected ``bleak.BleakClient``.

    Test code does not normally construct this directly — it is returned by
    ``FakeBleWorld.establish_connection`` (the patched replacement for
    ``bleak_retry_connector.establish_connection``). Reach already-created
    clients through ``world.clients`` (chronological, oldest first) when a
    test needs to assert something about "the same connection" or inject a
    notification into a specific one.
    """

    def __init__(
        self,
        world: "FakeBleWorld",
        mac_address: str,
        disconnected_callback: Callable[["FakeBleakClient"], None] | None,
    ) -> None:
        self.world = world
        self.mac_address = mac_address
        self.disconnected_callback = disconnected_callback
        self.id = world._next_client_id()
        self.services = world.build_services()
        self._is_connected = True
        self._notify_callback: Callable[[Any, bytearray], None] | None = None
        self._write_count = 0
        self._drop_scheduled = False

    # -- bleak.BleakClient surface -----------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def start_notify(self, char_uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        await self.world._gate("start_notify")
        self._check_connected()
        self._notify_callback = callback
        for garbage in self.world.garbage_notifications:
            self._fire(bytes(garbage))

    async def stop_notify(self, char_uuid: str) -> None:
        await self.world._gate("stop_notify")
        self._notify_callback = None

    async def write_gatt_char(self, char_uuid: str, data: bytes | bytearray, response: bool = False) -> None:
        await self.world._gate("write")
        self._check_connected()
        payload = bytes(data)
        self._write_count += 1
        self.world._record_write(self, char_uuid, payload)

        drop_now = (
            self.world.drop_after_writes is not None
            and self._write_count >= self.world.drop_after_writes
            and not self._drop_scheduled
        )
        if drop_now:
            self._drop_scheduled = True

        if char_uuid == em1003_const.EM1003_WRITE_CHAR_UUID:
            self._handle_protocol_write(payload, drop_after=drop_now)
        elif drop_now:
            asyncio.ensure_future(self._deliver(None, self.world.response_delay, drop_after=True))

    async def disconnect(self) -> None:
        await self.world._gate("disconnect")
        self.world._record_disconnect(self)
        if self._is_connected:
            self._is_connected = False
            self.world._close_link(self)
        self._notify_callback = None

    async def read_gatt_char(self, char_uuid: str) -> bytes:
        await self.world._gate("read_char")
        self._check_connected()
        if char_uuid in self.world.characteristics:
            return self.world.characteristics[char_uuid]
        if char_uuid == em1003_const.DEVICE_NAME_UUID:
            return self.world.device_name.encode("utf-8")
        raise BleakError(f"fake BLE world: unknown characteristic {char_uuid}")

    # -- internal -------------------------------------------------------

    def _check_connected(self) -> None:
        if not self._is_connected:
            raise BleakError(f"fake BLE world: {self.mac_address} is not connected")

    def _handle_protocol_write(self, data: bytes, *, drop_after: bool) -> None:
        if len(data) < 3:
            if drop_after:
                asyncio.ensure_future(self._deliver(None, self.world.response_delay, drop_after=True))
            return
        seq, cmd, key = data[0], data[1], data[2]
        if cmd == em1003_const.CMD_READ_SENSOR:
            self._respond_sensor(seq, key, drop_after)
        elif cmd == em1003_const.CMD_BUZZER and key == 0x00 and len(data) == 3:
            self._respond_buzzer_query(seq, drop_after)
        elif cmd == em1003_const.CMD_BUZZER and key == 0x01 and len(data) >= 4:
            self._respond_buzzer_set(seq, data[3], drop_after)
        elif drop_after:
            asyncio.ensure_future(self._deliver(None, self.world.response_delay, drop_after=True))

    def _respond_sensor(self, seq: int, sensor_id: int, drop_after: bool) -> None:
        if sensor_id in self.world.silent_sensors:
            if drop_after:
                asyncio.ensure_future(self._deliver(None, self._delay_for(sensor_id), drop_after=True))
            return
        raw = self.world.sensor_raw.get(sensor_id, 0)
        frame = bytes([seq, em1003_const.CMD_READ_SENSOR, sensor_id]) + encode_raw(raw)
        asyncio.ensure_future(self._deliver(frame, self._delay_for(sensor_id), drop_after=drop_after))

    def _respond_buzzer_query(self, seq: int, drop_after: bool) -> None:
        if self.world.buzzer_query_silent:
            if drop_after:
                asyncio.ensure_future(self._deliver(None, self._delay_for(0x00), drop_after=True))
            return
        state_byte = em1003_const.BUZZER_ON if self.world.buzzer_state else em1003_const.BUZZER_OFF
        frame = bytes([seq, em1003_const.CMD_BUZZER, 0x00, state_byte])
        asyncio.ensure_future(self._deliver(frame, self._delay_for(0x00), drop_after=drop_after))

    def _respond_buzzer_set(self, seq: int, state_byte: int, drop_after: bool) -> None:
        if self.world.apply_buzzer_set:
            self.world.buzzer_state = state_byte == em1003_const.BUZZER_ON
        if self.world.buzzer_set_silent:
            if drop_after:
                asyncio.ensure_future(self._deliver(None, self._delay_for(0x01), drop_after=True))
            return
        frame = bytes([seq, em1003_const.CMD_BUZZER_SET_RESPONSE, state_byte])
        asyncio.ensure_future(self._deliver(frame, self._delay_for(0x01), drop_after=drop_after))

    def _delay_for(self, key: int) -> float:
        return self.world.extra_delay_for.get(key, self.world.response_delay)

    async def _deliver(self, frame: bytes | None, delay: float, *, drop_after: bool = False) -> None:
        if delay:
            await asyncio.sleep(delay)
        if frame is not None and self._is_connected and self._notify_callback is not None:
            self._notify_callback(em1003_const.EM1003_NOTIFY_CHAR_UUID, bytearray(frame))
        if drop_after:
            self._simulate_drop()

    def _fire(self, frame: bytes, delay: float = 0.0) -> None:
        asyncio.ensure_future(self._deliver(frame, delay))

    def _simulate_drop(self) -> None:
        if not self._is_connected:
            return
        self._is_connected = False
        self.world._on_unexpected_drop(self)
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)


# ---------------------------------------------------------------------------
# FakeBleWorld
# ---------------------------------------------------------------------------


class FakeBleWorld:
    """A configurable fake replacement for the real BLE stack. See the
    module docstring for the full list of knobs and accounting fields."""

    def __init__(self) -> None:
        # -- protocol state -------------------------------------------------
        self.device_present: bool = True
        self.device_name: str = "3GCG300ZY4"
        self.rssi: int = -55
        self.sensor_raw: dict[int, int] = {
            sid: raw_for_engineering_value(sid, val)
            for sid, val in _DEFAULT_ENGINEERING_VALUES.items()
        }
        self.silent_sensors: set[int] = set()
        self.buzzer_state: bool = False
        self.buzzer_query_silent: bool = False
        self.buzzer_set_silent: bool = False
        self.apply_buzzer_set: bool = True
        self.characteristics: dict[str, bytes] = {}
        self.discovered: list[Any] = []

        # -- timing -----------------------------------------------------
        self.response_delay: float = 0.0
        self.extra_delay_for: dict[int, float] = {}

        # -- fault injection ----------------------------------------------
        self.drop_after_writes: int | None = None
        self.garbage_notifications: list[bytes] = []
        self.hang_at: set[str] = set()
        self.hang_once: set[str] = set()
        self.raise_at: dict[str, BaseException | type[BaseException]] = {}
        self.raise_once: dict[str, BaseException | type[BaseException]] = {}

        # -- accounting (read-only from a test's point of view) -----------
        self.connect_attempts: int = 0
        self.max_concurrent_connects: int = 0
        self.open_links: int = 0
        self.max_open_links: int = 0
        self.total_links_opened: int = 0
        self.total_links_closed: int = 0
        self.total_links_dropped: int = 0
        self.writes: list[WriteRecord] = []
        self.disconnect_calls: dict[int, int] = {}
        self.clients: list[FakeBleakClient] = []

        # -- private bookkeeping --------------------------------------------
        self._concurrent_connects = 0
        self._open_clients: list[FakeBleakClient] = []
        self._next_id_counter = 0
        self._hang_event = asyncio.Event()

    # -- patch targets: bleak_retry_connector.establish_connection ------

    async def establish_connection(
        self,
        client_class: Any,
        device: Any,
        mac_address: str,
        *,
        disconnected_callback: Callable[[FakeBleakClient], None] | None = None,
        max_attempts: int = 1,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> FakeBleakClient:
        """Patched-in replacement for ``bleak_retry_connector.establish_connection``."""
        self.connect_attempts += 1
        self._concurrent_connects += 1
        self.max_concurrent_connects = max(self.max_concurrent_connects, self._concurrent_connects)
        try:
            await self._gate("connect")
            if not self.device_present:
                raise BleakError(f"fake BLE world: device not found: {mac_address}")
            client = FakeBleakClient(self, mac_address, disconnected_callback)
            self.clients.append(client)
            self._open_clients.append(client)
            self.open_links += 1
            self.total_links_opened += 1
            self.max_open_links = max(self.max_open_links, self.open_links)
            return client
        finally:
            self._concurrent_connects -= 1

    # -- patch targets: homeassistant.components.bluetooth --------------

    def async_ble_device_from_address(self, hass: Any, address: str, connectable: bool = True) -> BLEDevice | None:
        """Patched-in replacement for ``bluetooth.async_ble_device_from_address``.

        Returns a real ``bleak.backends.device.BLEDevice`` (not a custom
        stand-in) so that a test asserting "``BLEDevice.rssi`` is never
        accessed" (D7) gets a genuine ``AttributeError`` for free — bleak
        >= 1.0's ``BLEDevice`` simply has no ``rssi`` attribute.
        """
        if not self.device_present:
            return None
        return BLEDevice(address=address, name=self.device_name, details={})

    def async_last_service_info(self, hass: Any, address: str, connectable: bool = True) -> Any | None:
        """Patched-in replacement for ``bluetooth.async_last_service_info``
        (the D7-correct way to get RSSI, instead of ``BLEDevice.rssi``)."""
        if not self.device_present:
            return None
        return SimpleNamespace(name=self.device_name, address=address, rssi=self.rssi, connectable=True)

    def async_discovered_service_info(self, hass: Any, connectable: bool = True) -> list[Any]:
        """Patched-in replacement for ``bluetooth.async_discovered_service_info``.

        Returns whatever test code has put in ``world.discovered`` (empty by
        default). Populate it with objects exposing ``.address``, ``.name``
        and ``.rssi``, e.g. ``SimpleNamespace(address=MAC, name="EM1003", rssi=-60)``.
        """
        return list(self.discovered)

    # -- test-facing configuration helpers -------------------------------

    def set_sensor_value(self, sensor_id: int, engineering_value: float | int) -> None:
        """Set a sensor's reading in engineering units (e.g. degrees, %)."""
        self.sensor_raw[sensor_id] = raw_for_engineering_value(sensor_id, engineering_value)

    def set_sensor_raw(self, sensor_id: int, raw: int) -> None:
        """Set a sensor's reading as the raw uint16 that goes on the wire."""
        self.sensor_raw[sensor_id] = raw

    def inject(self, data: bytes, *, delay: float = 0.0, client: FakeBleakClient | None = None) -> None:
        """Push an arbitrary raw notification into a live connection.

        Defaults to the most-recently-opened still-open connection. Raises
        ``RuntimeError`` if there is none — open one first (e.g. start the
        operation under test) before injecting.
        """
        target = client or self._most_recent_open_client()
        if target is None:
            raise RuntimeError("FakeBleWorld.inject: no open FakeBleakClient to inject into")
        target._fire(bytes(data))

    def build_services(self) -> FakeServiceCollection:
        """Build the GATT service table handed to a new ``FakeBleakClient``.

        Wrapped in a :class:`FakeServiceCollection` so it has the same
        surface as real bleak's ``BleakGATTServiceCollection`` (iterable,
        no ``__len__``).
        """
        return FakeServiceCollection(default_services(self))

    @property
    def links_are_clean(self) -> bool:
        """``True`` iff nothing is currently open — the standard
        "no leaks" assertion after exercising any device operation."""
        return self.open_links == 0

    # -- accounting / bookkeeping (private) -------------------------------

    def _next_client_id(self) -> int:
        self._next_id_counter += 1
        return self._next_id_counter

    def _record_write(self, client: FakeBleakClient, char_uuid: str, data: bytes) -> None:
        self.writes.append(
            WriteRecord(index=len(self.writes) + 1, client_id=client.id, char_uuid=char_uuid, data=data)
        )

    def _record_disconnect(self, client: FakeBleakClient) -> None:
        self.disconnect_calls[client.id] = self.disconnect_calls.get(client.id, 0) + 1

    def _close_link(self, client: FakeBleakClient) -> None:
        self.open_links = max(0, self.open_links - 1)
        self.total_links_closed += 1
        if client in self._open_clients:
            self._open_clients.remove(client)

    def _on_unexpected_drop(self, client: FakeBleakClient) -> None:
        self.open_links = max(0, self.open_links - 1)
        self.total_links_dropped += 1
        if client in self._open_clients:
            self._open_clients.remove(client)

    def _most_recent_open_client(self) -> FakeBleakClient | None:
        return self._open_clients[-1] if self._open_clients else None

    async def _gate(self, stage: str) -> None:
        """Apply hang/raise fault injection for one named stage. Order:
        one-shot hang, then one-shot raise, then persistent hang, then
        persistent raise — a test combining knobs gets the one-shot first."""
        if stage in self.hang_once:
            self.hang_once.discard(stage)
            await self._hang_event.wait()
            return
        if stage in self.raise_once:
            exc = self.raise_once.pop(stage)
            raise _materialize(exc, stage)
        if stage in self.hang_at:
            await self._hang_event.wait()
        if stage in self.raise_at:
            raise _materialize(self.raise_at[stage], stage)
