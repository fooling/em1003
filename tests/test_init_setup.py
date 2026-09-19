"""F5 -- Setup, unload, services, device name (docs/ble_session_refactor.md section 10, F5 table).

Owned by the F5 test author (see docs/ble_session_refactor.md section 11, "File ownership"). This
file is the regression guard for D4 (extra connections at startup: a device-name read plus a
sensor poll plus a buzzer poll, every HA start) and D8's setup/unload/service items: the dead
`asyncio.sleep(2)`, `async_unload_entry` never disconnecting or removing services, services
re-registered per entry, and the switch/sensor platforms racing the coordinator into `hass.data`.

Every test drives the integration the way Home Assistant itself does -- through
`hass.config_entries.async_setup` / `async_unload` / `async_reload` and `hass.services.async_call`
-- against the `ble_world` fixture (`tests/fakes.py` via `tests/conftest.py`). Nothing here assumes
`device.py`'s session internals beyond what section 6 of the plan pins down for `__init__.py`
(`_async_device_for_mac`, the exact `hass.data` shape, the six service names). Anything that talks
to the fake world is wrapped in a bounded `asyncio.timeout` (`_bounded`, below) so a reintroduced
unbounded await (D2) fails this suite instead of hanging it -- most of these tests don't need
`fast_timeouts` at all, since no test in this file injects a hang or a slow response; the one that
runs two real BLE sessions concurrently (F5-17) plugs in a short but real `response_delay` and
widens `BLE_SESSION_TIMEOUT` back up so a fault-free, slightly slower session isn't mistaken for the
deadline case that `test_device_session.py`'s F1-16 covers on its own.

Case IDs implemented: F5-01 .. F5-22 (docs/ble_session_refactor.md section 10, "F5" table), plus
locally-added cases F5-X1 .. F5-X3 for scenarios in scope for this feature (D4/D8) that the matrix
does not spell out explicitly: the device-name cache write must not itself trigger a reload (update
listener registration order), two entries set up truly concurrently must not clobber each other's
`hass.data` entry, and unload must stay safe after a setup that never reached the device at all.

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_init_setup.py -q
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any, Awaitable, TypeVar

import pytest
from bleak import BleakScanner
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

import custom_components.em1003 as em1003_pkg
import custom_components.em1003.device as em1003_device_module
from custom_components.em1003 import const as em1003_const

from .conftest import MAC
from .fakes import FakeBleakClient

_T = TypeVar("_T")

# Generous but bounded wall-clock ceilings for anything that could hang if a defect this file
# guards against (D2's unbounded awaits, reintroduced) came back. Far larger than any timing this
# suite actually needs when the fake is healthy, so these only ever fire on a genuine regression.
SAFE_TIMEOUT = 5.0
SAFE_TIMEOUT_LONG = 8.0

MAC2 = "11:22:33:44:55:66"

#: The 6 debug service names, in the order const.py declares them (F5-13/F5-14 don't care about
#: order, just membership -- kept as a tuple, not a set, only for stable iteration in loops).
_ALL_SERVICE_NAMES = (
    em1003_const.SERVICE_SCAN_DEVICE,
    em1003_const.SERVICE_DISCOVER_ALL,
    em1003_const.SERVICE_LIST_SERVICES,
    em1003_const.SERVICE_READ_CHARACTERISTIC,
    em1003_const.SERVICE_WRITE_CHARACTERISTIC,
    em1003_const.SERVICE_READ_DEVICE_NAME,
)

_SENSOR_KEYS = [info["key"] for info in em1003_const.SENSOR_TYPES.values()]


# ---------------------------------------------------------------------------
# Local helpers (this feature's own scaffolding -- tests/fakes.py and conftest.py are not touched)
# ---------------------------------------------------------------------------


async def _bounded(coro: Awaitable[_T], timeout: float = SAFE_TIMEOUT) -> _T:
    """Await `coro` under a bounded `asyncio.timeout`.

    Used around every call in this file that reaches the (fake) BLE stack, directly or through
    `hass.config_entries.async_setup/unload/reload` or `hass.services.async_call`, so that a
    reintroduced unbounded await fails the offending test with a `TimeoutError` instead of hanging
    the whole suite.
    """
    async with asyncio.timeout(timeout):
        return await coro


def _registry_entity_ids(hass: Any, mac: str = MAC) -> list[str | None]:
    """The 8 sensor entity ids followed by the switch entity id, by unique id, in `SENSOR_TYPES`
    order -- `None` for any not (yet) registered."""
    registry = er.async_get(hass)
    ids = [registry.async_get_entity_id("sensor", em1003_const.DOMAIN, f"{mac}_{key}") for key in _SENSOR_KEYS]
    ids.append(registry.async_get_entity_id("switch", em1003_const.DOMAIN, f"{mac}_buzzer"))
    return ids


async def _call_service(hass: Any, service: str, data: dict[str, Any], timeout: float = SAFE_TIMEOUT) -> None:
    await _bounded(hass.services.async_call(em1003_const.DOMAIN, service, data, blocking=True), timeout)


# ---------------------------------------------------------------------------
# F5-01 / F5-02 -- setup never fails even when the device can't be reached
# ---------------------------------------------------------------------------


async def test_f5_01_setup_succeeds_when_device_unreachable(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-01: `async_ble_device_from_address` returning `None` (device not currently advertising)
    still leaves the entry LOADED with all 9 entities registered -- sensors `unavailable`, the
    switch `unknown` -- and setup raises nothing (BC-16)."""
    ble_world.device_present = False
    entry = mock_entry()

    assert await _bounded(setup_integration(hass, entry)) is True
    assert entry.state is ConfigEntryState.LOADED

    entity_ids = _registry_entity_ids(hass)
    assert all(eid is not None for eid in entity_ids), f"missing entities: {entity_ids}"
    assert len(entity_ids) == 9

    for eid in entity_ids[:-1]:
        state = hass.states.get(eid)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE, f"{eid} -> {state.state}"

    switch_state = hass.states.get(entity_ids[-1])
    assert switch_state is not None
    assert switch_state.state == STATE_UNKNOWN

    coordinator = hass.data[em1003_const.DOMAIN][entry.entry_id]["coordinator"]
    assert coordinator.last_update_success is False
    assert ble_world.links_are_clean


async def test_f5_02_setup_succeeds_when_connect_raises(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-02: a `BleakError` from every connect attempt (device present but unreachable) still
    leaves the entry LOADED, never SETUP_RETRY, with `last_update_success is False`."""
    ble_world.raise_at["connect"] = BleakError
    entry = mock_entry()

    assert await _bounded(setup_integration(hass, entry)) is True
    assert entry.state is ConfigEntryState.LOADED
    assert entry.state is not ConfigEntryState.SETUP_RETRY

    coordinator = hass.data[em1003_const.DOMAIN][entry.entry_id]["coordinator"]
    assert coordinator.last_update_success is False
    assert ble_world.links_are_clean


# ---------------------------------------------------------------------------
# F5-03 / F5-04 / F5-05 -- setup ordering and hass.data shape
# ---------------------------------------------------------------------------


async def test_f5_03_coordinator_in_hass_data_before_platforms(hass, ble_world, fast_timeouts, mock_entry, monkeypatch):
    """F5-03: the coordinator is already sitting in `hass.data` by the time platforms are
    forwarded, so `sensor.py`/`switch.py` can rely on `hass.data[DOMAIN][entry_id]["coordinator"]`
    unconditionally (no construction race, D8)."""
    seen: dict[str, Any] = {}
    orig_forward = hass.config_entries.async_forward_entry_setups

    async def spy_forward(entry_, platforms):
        entry_data = hass.data.get(em1003_const.DOMAIN, {}).get(entry_.entry_id, {})
        seen["coordinator"] = entry_data.get("coordinator")
        return await orig_forward(entry_, platforms)

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", spy_forward)

    entry = mock_entry()
    entry.add_to_hass(hass)
    assert await _bounded(hass.config_entries.async_setup(entry.entry_id)) is True
    await hass.async_block_till_done()

    assert seen.get("coordinator") is not None, "coordinator was not in hass.data when platforms were forwarded"
    assert seen["coordinator"] is hass.data[em1003_const.DOMAIN][entry.entry_id]["coordinator"]


async def test_f5_04_hass_data_payload_keys(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-04: the per-entry `hass.data` payload has exactly today's keys plus `"coordinator"`."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    data = hass.data[em1003_const.DOMAIN][entry.entry_id]
    assert set(data.keys()) == {"mac_address", "name", "device_name", "device", "coordinator"}
    assert data["mac_address"] == entry.data[em1003_const.CONF_MAC_ADDRESS]
    assert data["name"] == entry.title


async def test_f5_05_no_long_sleep_at_setup(hass, ble_world, fast_timeouts, mock_entry, monkeypatch):
    """F5-05: setup never sleeps for a second or more (regression guard for the old, unconditional
    `asyncio.sleep(2)`). `fast_timeouts` shrinks the legitimate BLE_* pacing delays so a healthy
    poll's own inter-request pauses don't need real multi-second waits either."""
    recorded: list[float] = []
    orig_sleep = asyncio.sleep

    async def spy_sleep(delay: float, *args: Any, **kwargs: Any) -> Any:
        recorded.append(delay)
        return await orig_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", spy_sleep)

    entry = mock_entry()
    entry.add_to_hass(hass)
    assert await _bounded(hass.config_entries.async_setup(entry.entry_id)) is True
    await hass.async_block_till_done()

    assert not any(d >= 1.0 for d in recorded), f"a sleep >= 1s happened during setup: {recorded}"


# ---------------------------------------------------------------------------
# F5-06 .. F5-10 -- device-name resolution
# ---------------------------------------------------------------------------


async def test_f5_06_cached_device_name_zero_name_read_connections(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch
):
    """F5-06: a cached `device_name` costs zero extra connections (only the first poll connects)
    and `read_gatt_char` for the Device Name characteristic is never called."""
    read_calls = {"n": 0}
    orig_read = FakeBleakClient.read_gatt_char

    async def spy_read(self: FakeBleakClient, char_uuid: str) -> bytes:
        if char_uuid == em1003_const.DEVICE_NAME_UUID:
            read_calls["n"] += 1
        return await orig_read(self, char_uuid)

    monkeypatch.setattr(FakeBleakClient, "read_gatt_char", spy_read)

    entry = mock_entry(data={em1003_const.CONF_DEVICE_NAME: "CACHED"})
    assert await _bounded(setup_integration(hass, entry))

    assert ble_world.connect_attempts == 1, "a cached name must not open a separate connection"
    assert read_calls["n"] == 0, "the Device Name characteristic must never be read when cached"

    device_entry = dr.async_get(hass).async_get_device(identifiers={(em1003_const.DOMAIN, MAC)})
    assert device_entry is not None
    assert device_entry.name == "CACHED"
    assert ble_world.links_are_clean


async def test_f5_07_uncached_name_one_attempt_cached_additively(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch
):
    """F5-07: an uncached name costs exactly one extra connection (2 total with the first poll),
    every connection used `max_attempts=1`, the resolved name is written into `entry.data`
    additively, and `entry.title` is untouched."""
    max_attempts_seen: list[Any] = []
    orig_establish = em1003_device_module.establish_connection

    async def spy_establish(*args: Any, **kwargs: Any) -> Any:
        max_attempts_seen.append(kwargs.get("max_attempts"))
        return await orig_establish(*args, **kwargs)

    monkeypatch.setattr(em1003_device_module, "establish_connection", spy_establish)

    entry = mock_entry()  # data == {"mac_address": MAC}, no cached name
    pre_title = entry.title
    assert await _bounded(setup_integration(hass, entry))

    assert ble_world.connect_attempts == 2, "expected exactly 2 connections: name read + first poll"
    assert entry.data == {
        em1003_const.CONF_MAC_ADDRESS: MAC,
        em1003_const.CONF_DEVICE_NAME: ble_world.device_name,
    }
    assert max_attempts_seen and all(m == 1 for m in max_attempts_seen), (
        f"establish_connection must always use max_attempts=1, saw {max_attempts_seen}"
    )
    assert entry.title == pre_title
    assert ble_world.links_are_clean


async def test_f5_08_uncached_name_read_fails_falls_back_nothing_written(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-08: a failed name read falls back to `entry.title` for the device registry name, writes
    nothing to `entry.data`, still leaves the entry LOADED, and does not retry (exactly one failed
    name attempt, i.e. 2 connections total with the first poll)."""
    ble_world.raise_once["read_char"] = BleakError
    entry = mock_entry()
    pre_title = entry.title

    assert await _bounded(setup_integration(hass, entry)) is True
    assert entry.state is ConfigEntryState.LOADED

    device_entry = dr.async_get(hass).async_get_device(identifiers={(em1003_const.DOMAIN, MAC)})
    assert device_entry is not None
    assert device_entry.name == pre_title
    assert em1003_const.CONF_DEVICE_NAME not in entry.data
    assert ble_world.connect_attempts == 2, "no retry loop: 1 failed name attempt + 1 poll"
    assert ble_world.links_are_clean


async def test_f5_09_title_unique_id_version_never_change(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-09: `entry.title`, `entry.unique_id` and `entry.version` are untouched by setup, on both
    the name-read-succeeds path (F5-07) and the name-read-fails path (F5-08)."""
    entry_ok = mock_entry(mac=MAC, entry_id="entry_ok")
    pre_ok = (entry_ok.title, entry_ok.unique_id, entry_ok.version)
    assert await _bounded(setup_integration(hass, entry_ok))
    assert (entry_ok.title, entry_ok.unique_id, entry_ok.version) == pre_ok

    ble_world.raise_once["read_char"] = BleakError
    entry_fail = mock_entry(mac=MAC2, entry_id="entry_fail")
    pre_fail = (entry_fail.title, entry_fail.unique_id, entry_fail.version)
    assert await _bounded(setup_integration(hass, entry_fail))
    assert (entry_fail.title, entry_fail.unique_id, entry_fail.version) == pre_fail


async def test_f5_10_name_read_goes_through_device_py(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-10: `__init__.py` has no `establish_connection` import of its own (BC-21) -- the name
    read is only visible through `device.establish_connection`, which is exactly what the 2-total
    connection count already proves."""
    assert not hasattr(em1003_pkg, "establish_connection"), "__init__.py must route all BLE through device.py"

    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    assert ble_world.connect_attempts == 2


# ---------------------------------------------------------------------------
# F5-11 / F5-12 -- unload
# ---------------------------------------------------------------------------


async def test_f5_11_unload_disconnects_an_open_link(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-11: `async_unload_entry` calls `device.disconnect()`, which is a real safety net, not
    dead code -- if a link were somehow still referenced by the device when unload runs (there
    should never be one; this simulates a session that failed to clean up after itself),
    `disconnect()` still closes it. World ends at 0 open links; the entry ends NOT_LOADED."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    device = hass.data[em1003_const.DOMAIN][entry.entry_id]["device"]
    assert ble_world.links_are_clean, "the setup poll should already have closed its own link"

    # Simulate a lingering link the session machinery failed to close (device._client and _lock
    # are named as test-relevant privates in section 4.5 of the plan).
    stray_client = await ble_world.establish_connection(
        None, None, MAC, disconnected_callback=None, max_attempts=1, timeout=1.0
    )
    device._client = stray_client
    assert ble_world.open_links == 1

    unload_ok = await _bounded(hass.config_entries.async_unload(entry.entry_id))
    await hass.async_block_till_done()

    assert unload_ok is True
    assert ble_world.links_are_clean, "unload did not close the lingering link"
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_f5_12_unload_shuts_down_coordinator_and_pops_hass_data(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration
):
    """F5-12: after unload, `entry.entry_id` (and, being the only entry, the whole `DOMAIN` key)
    is gone from `hass.data`, and the coordinator's periodic refresh is truly stopped -- advancing
    the clock past 5 scan intervals opens no further connections.

    Uses `async_fire_time_changed(hass, <explicit datetime>)` rather than `freezegun`/the `freezer`
    fixture: it force-runs due `TimerHandle`s in place (see `pytest_homeassistant_custom_component
    .common._async_fire_time_changed`) without touching `time.monotonic`, so it can't desync
    asyncio's own event-loop clock the way globally freezing time can.
    """
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    baseline = ble_world.connect_attempts
    assert em1003_const.DOMAIN in hass.data
    assert entry.entry_id in hass.data[em1003_const.DOMAIN]

    unload_ok = await _bounded(hass.config_entries.async_unload(entry.entry_id))
    await hass.async_block_till_done()

    assert unload_ok is True
    assert entry.entry_id not in hass.data.get(em1003_const.DOMAIN, {})

    now = dt_util.utcnow()
    for i in range(1, 6):
        async_fire_time_changed(hass, now + timedelta(seconds=em1003_const.DEFAULT_SCAN_INTERVAL * i + 1))
        await hass.async_block_till_done()

    assert ble_world.connect_attempts == baseline, "the coordinator kept polling after unload"


# ---------------------------------------------------------------------------
# F5-13 / F5-14 / F5-15 -- services lifecycle, reload
# ---------------------------------------------------------------------------


async def test_f5_13_services_registered_once_for_two_entries(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-13: two config entries (different MACs) still leave exactly one registration of each of
    the 6 services -- the `has_service` guard works."""
    entry1 = mock_entry(mac=MAC, entry_id="entry1")
    entry2 = mock_entry(mac=MAC2, entry_id="entry2")
    assert await _bounded(setup_integration(hass, entry1))
    assert await _bounded(setup_integration(hass, entry2))

    for service in _ALL_SERVICE_NAMES:
        assert hass.services.has_service(em1003_const.DOMAIN, service)
    assert set(hass.services.async_services()[em1003_const.DOMAIN].keys()) == set(_ALL_SERVICE_NAMES)


async def test_f5_14_services_survive_every_unload(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-14 (amended in the review round): the 6 debug services are registered once and never
    removed -- neither when the first of two entries unloads, nor when the last one does.

    The original F5-14 asserted the opposite ("removed on the last unload"). That turned out to be
    a regression against v1.0.3, which registered them once for the life of HA: a single-device
    install is fully unloaded in the middle of every options-triggered reload, so removing them
    there makes an automation calling `em1003.scan_device` fail with `ServiceNotFound`. The
    handlers resolve their device by MAC (`_async_device_for_mac` falls back to a transient
    `EM1003Device`), so they stay correct with no entry loaded. `hass.data[DOMAIN]` is still
    dropped on the last unload.
    """
    entry1 = mock_entry(mac=MAC, entry_id="entry1")
    entry2 = mock_entry(mac=MAC2, entry_id="entry2")
    assert await _bounded(setup_integration(hass, entry1))
    assert await _bounded(setup_integration(hass, entry2))

    assert await _bounded(hass.config_entries.async_unload(entry1.entry_id))
    await hass.async_block_till_done()
    for service in _ALL_SERVICE_NAMES:
        assert hass.services.has_service(em1003_const.DOMAIN, service), f"{service} removed too early"

    assert await _bounded(hass.config_entries.async_unload(entry2.entry_id))
    await hass.async_block_till_done()
    for service in _ALL_SERVICE_NAMES:
        assert hass.services.has_service(em1003_const.DOMAIN, service), (
            f"{service} was removed on the last unload (v1.0.3 kept it for the life of HA)"
        )
    assert em1003_const.DOMAIN not in hass.data


async def test_f5_15_reload_works(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-15: `async_reload` ends LOADED, keeps the same entity ids and unique ids (no duplicate
    registry entries), and the 6 services survive the round trip."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    registry = er.async_get(hass)
    before = er.async_entries_for_config_entry(registry, entry.entry_id)
    before_ids = {e.entity_id for e in before}
    before_unique = {e.unique_id for e in before}
    assert len(before_ids) == 9

    assert await _bounded(hass.config_entries.async_reload(entry.entry_id))
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    after = er.async_entries_for_config_entry(registry, entry.entry_id)
    after_ids = {e.entity_id for e in after}
    after_unique = {e.unique_id for e in after}
    assert after_ids == before_ids
    assert after_unique == before_unique
    for service in _ALL_SERVICE_NAMES:
        assert hass.services.has_service(em1003_const.DOMAIN, service)


# ---------------------------------------------------------------------------
# F5-16 .. F5-22 -- debug services
# ---------------------------------------------------------------------------


async def test_f5_16_every_debug_service_runs_without_raising(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-16: all 6 debug services complete against a healthy fake without raising, including
    `scan_device` and `read_device_name`, and leave 0 open links behind."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    calls = [
        (em1003_const.SERVICE_SCAN_DEVICE, {em1003_const.ATTR_MAC_ADDRESS: MAC}),
        (em1003_const.SERVICE_DISCOVER_ALL, {em1003_const.ATTR_MAC_ADDRESS: MAC}),
        (em1003_const.SERVICE_LIST_SERVICES, {em1003_const.ATTR_MAC_ADDRESS: MAC}),
        (
            em1003_const.SERVICE_READ_CHARACTERISTIC,
            {em1003_const.ATTR_MAC_ADDRESS: MAC, em1003_const.ATTR_CHARACTERISTIC_UUID: em1003_const.DEVICE_NAME_UUID},
        ),
        (
            em1003_const.SERVICE_WRITE_CHARACTERISTIC,
            {
                em1003_const.ATTR_MAC_ADDRESS: MAC,
                em1003_const.ATTR_CHARACTERISTIC_UUID: em1003_const.EM1003_WRITE_CHAR_UUID,
                em1003_const.ATTR_DATA: "0102ff",
            },
        ),
        (em1003_const.SERVICE_READ_DEVICE_NAME, {em1003_const.ATTR_MAC_ADDRESS: MAC}),
    ]
    for service, data in calls:
        await _call_service(hass, service, data)

    assert ble_world.links_are_clean


async def test_f5_17_debug_service_serialises_with_concurrent_poll(
    hass, ble_world, mock_entry, setup_integration, monkeypatch
):
    """F5-17: a debug service call for the same MAC as an in-flight poll serialises with it (max
    concurrent links 1, both complete) because it reuses the configured entry's own `EM1003Device`
    (same instance, same lock) -- `_async_device_for_mac` never spins up a second device for a MAC
    that's already configured."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    device = hass.data[em1003_const.DOMAIN][entry.entry_id]["device"]

    # A small but real per-request delay keeps the poll's session open long enough to overlap the
    # service call; BLE_SESSION_TIMEOUT is widened back up so this fault-free, slightly slower
    # session isn't mistaken for the deadline case F1-16 covers on its own.
    ble_world.response_delay = 0.02
    monkeypatch.setattr(em1003_const, "BLE_SESSION_TIMEOUT", 5.0)

    poll_result, _ = await _bounded(
        asyncio.gather(
            device.async_poll(),
            hass.services.async_call(
                em1003_const.DOMAIN,
                em1003_const.SERVICE_LIST_SERVICES,
                {em1003_const.ATTR_MAC_ADDRESS: MAC},
                blocking=True,
            ),
        ),
        SAFE_TIMEOUT_LONG,
    )

    assert poll_result.ok is True
    assert ble_world.max_concurrent_connects == 1
    assert ble_world.links_are_clean
    assert em1003_pkg._async_device_for_mac(hass, MAC) is device


async def test_f5_18_debug_service_for_unconfigured_mac_uses_transient_device(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration
):
    """F5-18: a debug service call for a MAC that isn't configured works against a throwaway
    device and never touches the configured device's lock or circuit breaker."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    device = hass.data[em1003_const.DOMAIN][entry.entry_id]["device"]

    await _call_service(hass, em1003_const.SERVICE_LIST_SERVICES, {em1003_const.ATTR_MAC_ADDRESS: MAC2})

    assert ble_world.links_are_clean
    assert device.is_busy is False
    assert device.circuit_breaker.failure_count == 0
    assert device.circuit_breaker.state == "CLOSED"


async def test_f5_19_write_characteristic_data_parsing(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-19: `write_characteristic`'s `data` field accepts a hex string (with or without spaces),
    a plain utf-8 string, or a list of ints, and writes exactly the corresponding bytes; an invalid
    value is logged and never opens a link."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    uuid = "0000ffe1-0000-1000-8000-00805f9b34fb"

    cases: list[tuple[Any, bytes]] = [
        ("0102ff", b"\x01\x02\xff"),
        ("01 02 ff", b"\x01\x02\xff"),
        ("hello", b"hello"),
        ([1, 2, 255], b"\x01\x02\xff"),
    ]
    for value, expected in cases:
        await _call_service(
            hass,
            em1003_const.SERVICE_WRITE_CHARACTERISTIC,
            {em1003_const.ATTR_MAC_ADDRESS: MAC, em1003_const.ATTR_CHARACTERISTIC_UUID: uuid, em1003_const.ATTR_DATA: value},
        )
        assert ble_world.writes[-1].data == expected, f"{value!r} -> {ble_world.writes[-1].data!r}, want {expected!r}"

    writes_before = len(ble_world.writes)
    await _call_service(
        hass,
        em1003_const.SERVICE_WRITE_CHARACTERISTIC,
        {em1003_const.ATTR_MAC_ADDRESS: MAC, em1003_const.ATTR_CHARACTERISTIC_UUID: uuid, em1003_const.ATTR_DATA: [1, 2, 999]},
    )
    assert len(ble_world.writes) == writes_before, "an invalid value must not open a link or write anything"
    assert ble_world.links_are_clean


async def test_f5_20_scan_device_does_not_touch_bleakscanner(hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch):
    """F5-20 (D7): `scan_device` never calls `BleakScanner.discover`, and `__init__.py` does not
    even import `BleakScanner`."""
    calls = {"n": 0}

    async def boom(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        raise AssertionError("BleakScanner.discover must not be called")

    monkeypatch.setattr(BleakScanner, "discover", boom)

    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    await _call_service(hass, em1003_const.SERVICE_SCAN_DEVICE, {em1003_const.ATTR_MAC_ADDRESS: MAC})

    assert calls["n"] == 0
    assert not hasattr(em1003_pkg, "BleakScanner")


async def test_f5_21_scan_device_never_reads_bledevice_rssi(hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch):
    """F5-21 (D7): `scan_device` gets RSSI from `bluetooth.async_last_service_info`, never from
    `BLEDevice.rssi` (which doesn't exist on bleak>=1.0's real `BLEDevice` -- the fake's
    `async_ble_device_from_address` returns one of those, so any access would raise
    `AttributeError` and the service's broad `except Exception` would otherwise hide it)."""
    calls = {"n": 0}
    orig_last_service_info = em1003_pkg.bluetooth.async_last_service_info

    def spy_last_service_info(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return orig_last_service_info(*args, **kwargs)

    monkeypatch.setattr(em1003_pkg.bluetooth, "async_last_service_info", spy_last_service_info)

    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    await _call_service(hass, em1003_const.SERVICE_SCAN_DEVICE, {em1003_const.ATTR_MAC_ADDRESS: MAC})

    assert calls["n"] >= 1, "scan_device must call bluetooth.async_last_service_info for RSSI"


async def test_f5_22_failing_debug_service_still_disconnects(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-22: `read_characteristic` on a uuid the fake doesn't know about raises inside the
    service body, but the service call itself still returns (never raises out of
    `hass.services.async_call`), the link is still closed, and the device's lock is free."""
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    device = hass.data[em1003_const.DOMAIN][entry.entry_id]["device"]

    await _call_service(
        hass,
        em1003_const.SERVICE_READ_CHARACTERISTIC,
        {em1003_const.ATTR_MAC_ADDRESS: MAC, em1003_const.ATTR_CHARACTERISTIC_UUID: "0000dead-0000-1000-8000-00805f9b34fb"},
    )

    assert ble_world.links_are_clean
    assert device.is_busy is False


# ---------------------------------------------------------------------------
# F5-X1 .. F5-X3 -- additional cases not spelled out by the matrix
# ---------------------------------------------------------------------------


async def test_f5_x1_name_caching_does_not_trigger_a_spurious_reload(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch
):
    """F5-X1 (added): caching the freshly-read device name (`async_update_entry`, step 3 of
    section 6.1) happens *before* the update listener is registered (step 8) -- so it must not
    itself trigger `async_reload`, which would otherwise silently double every first-time setup
    (and double its connection count, contradicting F5-07's exact-2 assertion)."""
    reload_calls = {"n": 0}
    orig_reload = hass.config_entries.async_reload

    async def spy_reload(entry_id: str, *args: Any, **kwargs: Any) -> Any:
        reload_calls["n"] += 1
        return await orig_reload(entry_id, *args, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_reload", spy_reload)

    entry = mock_entry()  # uncached name -> entry.data is updated additively during setup
    assert await _bounded(setup_integration(hass, entry))

    assert em1003_const.CONF_DEVICE_NAME in entry.data
    assert reload_calls["n"] == 0, "caching the device name during setup must not trigger a reload"
    assert ble_world.connect_attempts == 2


async def test_f5_x2_two_entries_set_up_concurrently_keep_independent_data(hass, ble_world, fast_timeouts, mock_entry):
    """F5-X2 (added): two config entries for different MACs, set up truly concurrently
    (`asyncio.gather`, not sequentially), each end up with their own `EM1003Device` and their own
    correct `hass.data` entry -- `hass.data.setdefault(DOMAIN, {})` isn't a race."""
    entry1 = mock_entry(mac=MAC, entry_id="entry1")
    entry2 = mock_entry(mac=MAC2, entry_id="entry2")
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    results = await _bounded(
        asyncio.gather(
            hass.config_entries.async_setup(entry1.entry_id),
            hass.config_entries.async_setup(entry2.entry_id),
        ),
        SAFE_TIMEOUT_LONG,
    )
    await hass.async_block_till_done()

    assert results == [True, True]
    data1 = hass.data[em1003_const.DOMAIN][entry1.entry_id]
    data2 = hass.data[em1003_const.DOMAIN][entry2.entry_id]
    assert data1["mac_address"] == MAC
    assert data2["mac_address"] == MAC2
    assert data1["device"] is not data2["device"]
    assert data1["coordinator"] is not data2["coordinator"]


async def test_f5_x3_unload_after_unreachable_setup_is_safe(hass, ble_world, fast_timeouts, mock_entry, setup_integration):
    """F5-X3 (added): unloading an entry whose device was never reachable (F5-01's scenario) is
    still a clean, non-raising no-op disconnect -- `disconnect()` on an idle device must not open
    a connection just to close one that never existed."""
    ble_world.device_present = False
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    baseline = ble_world.connect_attempts

    unload_ok = await _bounded(hass.config_entries.async_unload(entry.entry_id))
    await hass.async_block_till_done()

    assert unload_ok is True
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert ble_world.links_are_clean
    assert ble_world.connect_attempts == baseline, "disconnect() on an idle device must not connect"


# ---------------------------------------------------------------------------
# Review-round regression guards (see docs/ble_session_refactor.md, "Review round")
# ---------------------------------------------------------------------------


async def test_r_discover_all_service_collection_has_no_len(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, caplog
):
    """`discover_all` must not call `len()` on `client.services`.

    Real bleak's `BleakGATTServiceCollection` defines only `__iter__` /
    `__getitem__`, so `len(client.services)` raises `TypeError` -- which
    `handle_discover_all`'s own `except Exception` swallows, leaving the
    service silently useless against every real device. `tests/fakes.py`
    mimics that shape, and this test pins the observable outcome (the GATT
    table really is walked and logged).
    """
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    with pytest.raises(TypeError):
        len(ble_world.build_services())  # the fake must keep mimicking real bleak

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="custom_components.em1003"):
        await _call_service(hass, em1003_const.SERVICE_DISCOVER_ALL, {em1003_const.ATTR_MAC_ADDRESS: MAC})

    assert "Error during discovery" not in caplog.text, caplog.text
    assert "Service:" in caplog.text, "discover_all never walked a single service"
    assert em1003_const.EM1003_WRITE_CHAR_UUID in caplog.text


async def test_r_services_survive_the_reload_window(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch
):
    """The 6 debug services must stay registered across an unload/reload.

    v1.0.3 registered them once and never removed them. Saving the options
    dialog now reloads the entry, and a single-device install is fully
    unloaded in the middle of that reload -- an automation calling
    `em1003.scan_device` in that window must not hit `ServiceNotFound`.
    """
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    real_setup = em1003_pkg.async_setup_entry
    inside = asyncio.Event()
    release = asyncio.Event()

    async def gated_setup(hass_: Any, entry_: Any) -> bool:
        inside.set()
        await release.wait()
        return await real_setup(hass_, entry_)

    monkeypatch.setattr(em1003_pkg, "async_setup_entry", gated_setup)

    task = asyncio.ensure_future(hass.config_entries.async_reload(entry.entry_id))
    try:
        await _bounded(inside.wait())
        # Mid-reload: the entry is unloaded and hass.data[DOMAIN] is empty.
        for service in _ALL_SERVICE_NAMES:
            assert hass.services.has_service(em1003_const.DOMAIN, service), (
                f"{service} disappeared during the reload window"
            )
        await _call_service(hass, em1003_const.SERVICE_SCAN_DEVICE, {em1003_const.ATTR_MAC_ADDRESS: MAC})
    finally:
        release.set()
        await _bounded(task)
        await hass.async_block_till_done()


async def test_r_unload_is_not_blocked_by_an_in_flight_poll(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch
):
    """Unloading must not wait out a poll that is stuck on an unreachable device.

    `coordinator.async_shutdown()` does not cancel a refresh that is already
    running, and `device.disconnect()` takes the same lock that poll holds --
    so an unbounded wait here stalls every options-triggered reload for up to
    a whole BLE session budget while the 9 entities are already gone.
    """
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))

    coordinator = hass.data[em1003_const.DOMAIN][entry.entry_id]["coordinator"]
    device = hass.data[em1003_const.DOMAIN][entry.entry_id]["device"]

    # Budgets large enough that an unbounded wait is unmistakable in wall clock,
    # while `BLE_UNLOAD_DISCONNECT_TIMEOUT` stays at its `fast_timeouts` value.
    monkeypatch.setattr(em1003_const, "BLE_CONNECT_TIMEOUT", 3.0)
    monkeypatch.setattr(em1003_const, "BLE_SESSION_TIMEOUT", 10.0)
    ble_world.hang_at.add("connect")  # device out of range: the poll sits in the lock

    poll = asyncio.ensure_future(coordinator.async_refresh())
    try:
        async with asyncio.timeout(SAFE_TIMEOUT):
            while not device.is_busy:
                await asyncio.sleep(0)

        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await _bounded(hass.config_entries.async_unload(entry.entry_id))
        elapsed = loop.time() - started

        assert elapsed < 1.0, (
            f"async_unload blocked {elapsed:.2f}s on the in-flight poll "
            "(BLE_CONNECT_TIMEOUT was 3.0s; in production up to BLE_SESSION_TIMEOUT)"
        )
    finally:
        poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll


async def test_r_write_characteristic_is_bounded_by_write_timeout(
    hass, ble_world, fast_timeouts, mock_entry, setup_integration, monkeypatch
):
    """The `write_characteristic` debug service must bound its own GATT write.

    It runs on the *configured* entry's device, so an unacknowledged write
    would otherwise pin the coordinator's lock for the whole session budget
    (120s in production), starving the next poll or two.
    """
    entry = mock_entry()
    assert await _bounded(setup_integration(hass, entry))
    device = hass.data[em1003_const.DOMAIN][entry.entry_id]["device"]

    # Leave the session deadline roomy: only a per-write bound can satisfy this.
    monkeypatch.setattr(em1003_const, "BLE_SESSION_TIMEOUT", 3.0)
    assert em1003_const.BLE_WRITE_TIMEOUT <= 0.05
    ble_world.hang_at.add("write")

    loop = asyncio.get_running_loop()
    started = loop.time()
    await _call_service(
        hass,
        em1003_const.SERVICE_WRITE_CHARACTERISTIC,
        {
            em1003_const.ATTR_MAC_ADDRESS: MAC,
            em1003_const.ATTR_CHARACTERISTIC_UUID: em1003_const.EM1003_WRITE_CHAR_UUID,
            em1003_const.ATTR_DATA: "0102ff",
        },
    )
    elapsed = loop.time() - started

    assert elapsed < 1.0, (
        f"write_characteristic held the device lock {elapsed:.2f}s -- bounded only by "
        f"BLE_SESSION_TIMEOUT, not by BLE_WRITE_TIMEOUT ({em1003_const.BLE_WRITE_TIMEOUT}s)"
    )
    assert device.is_busy is False
    assert ble_world.links_are_clean
