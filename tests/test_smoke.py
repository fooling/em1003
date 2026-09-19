"""Smoke test for the test scaffold itself.

This proves `tests/fakes.py` + `tests/conftest.py` work end to end against
the **current, pre-refactor** code in `custom_components/em1003`. It is
deliberately "contract-neutral": it only asserts legacy public behaviour
that must keep working after the refactor (a sensor gets a real state from
a config entry, and the fake world's connection accounting is internally
consistent) — nothing here pins down session/locking/circuit-breaker
behaviour that the refactor is explicitly allowed to change. The real
per-defect regression tests live in `test_device_session.py`,
`test_circuit_breaker.py`, `test_protocol.py`, `test_coordinator_entities.py`,
`test_init_setup.py`, `test_config_flow.py` and `test_backward_compat.py`
(see docs/ble_session_refactor.md section 10), owned by other test authors.

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_smoke.py -q
"""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.em1003 import const as em1003_const

from .conftest import MAC
from .fakes import FakeBleWorld


async def test_setup_entry_shows_temperature_sensor(hass, ble_world, mock_entry, setup_integration):
    """A config entry set up against a healthy fake device produces a real
    temperature sensor state — the scaffold can drive the integration."""
    ble_world.set_sensor_value(em1003_const.SENSOR_ID_01, 23.4)

    entry = mock_entry()
    assert await setup_integration(hass, entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", em1003_const.DOMAIN, f"{MAC}_{em1003_const.SENSOR_TYPES[em1003_const.SENSOR_ID_01]['key']}"
    )
    assert entity_id is not None, "temperature sensor was not registered"

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state not in ("unknown", "unavailable")
    assert float(state.state) == pytest.approx(23.4)


async def test_ble_world_link_accounting_after_setup(hass, ble_world, mock_entry, setup_integration):
    """After a full setup against a healthy fake, every connection the
    (current, not-yet-serialised) integration opened was also closed — the
    fake's accounting fields reflect reality and can be trusted by the
    session/leak tests that come later."""
    entry = mock_entry()
    assert await setup_integration(hass, entry)

    assert ble_world.connect_attempts >= 1
    assert ble_world.max_concurrent_connects >= 1
    assert ble_world.total_links_opened == ble_world.connect_attempts
    assert ble_world.total_links_dropped == 0
    # The current code always disconnects in a `finally`, so nothing should
    # be left open once setup (and the tasks it spawned) has settled.
    assert ble_world.links_are_clean, f"{ble_world.open_links} link(s) left open after setup"
    assert ble_world.total_links_opened == ble_world.total_links_closed

    # At least one real sensor request went out on the wire, correctly
    # addressed to the EM1003 write characteristic.
    assert ble_world.writes, "no GATT writes were recorded"
    assert all(w.char_uuid == em1003_const.EM1003_WRITE_CHAR_UUID for w in ble_world.writes)


async def test_fake_world_tracks_connections_writes_and_disconnects():
    """Unit-level proof of the fake itself, independent of Home Assistant:
    one connection, one write, one matching notification, one disconnect."""
    world = FakeBleWorld()
    received: list[bytes] = []

    client = await world.establish_connection(
        None, None, MAC, disconnected_callback=None, max_attempts=1, timeout=1.0
    )
    assert world.connect_attempts == 1
    assert world.open_links == 1
    assert world.max_concurrent_connects == 1

    await client.start_notify(em1003_const.EM1003_NOTIFY_CHAR_UUID, lambda _sender, data: received.append(bytes(data)))
    await client.write_gatt_char(
        em1003_const.EM1003_WRITE_CHAR_UUID,
        bytes([0x01, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_01]),
        response=False,
    )
    await asyncio.sleep(0)  # let the fake's fire-and-forget response task run

    assert len(world.writes) == 1
    assert received == [bytes([0x01, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_01]) + world.sensor_raw[em1003_const.SENSOR_ID_01].to_bytes(2, "little")]

    await client.disconnect()
    assert world.links_are_clean
    assert world.total_links_closed == 1
    assert world.disconnect_calls[client.id] == 1


async def test_fake_world_device_not_found(hass, ble_world):
    """`device_present = False` makes the HA bluetooth lookup patch points
    report "no device", without any connection being opened."""
    ble_world.device_present = False
    assert ble_world.async_ble_device_from_address(hass, MAC) is None
    assert ble_world.async_last_service_info(hass, MAC) is None
    assert ble_world.connect_attempts == 0
