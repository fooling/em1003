"""F4 — Coordinator, sensor and switch entities (docs/ble_session_refactor.md
section 10, table "F4").

Owned by the F4 test author (see section 11, "File ownership"). Implements
every case in the F4 matrix (21 cases, F4-01..F4-21) plus two additional
regression guards marked ``F4-Xn`` for gaps the matrix does not spell out
explicitly but that the plan's contract implies.

Scope, per the task brief: full integration through ``hass`` with the fake
BLE world — sensor/switch entity states, coordinator polling cadence, the
20-minute stale-value cache, breaker interaction as seen from the
coordinator, and the buzzer switch's lack of its own connection. Session
internals (lock, timeouts, seq ids) are F1's job; breaker internals are F2's;
protocol parsing is F3's — this file only asserts what a poll/toggle
produces as HA-observable state.

The source under test (``coordinator.py``, ``sensor.py``, ``switch.py``,
``__init__.py``) is being written concurrently by another agent to match the
plan's contract (sections 5, 6, 7). These tests are written against *that*
contract, not against whatever currently happens to be on disk.

Timing notes:

* Every test that triggers a real poll uses the local ``quick_ble`` fixture
  (built here, not in ``tests/conftest.py`` — see the note on its
  definition) rather than ``conftest.py``'s ``fast_timeouts``: that fixture
  also shrinks ``BLE_SESSION_TIMEOUT`` to 0.2s, which is *tighter* than
  ``7 * BLE_INTER_REQUEST_DELAY`` even at its own shrunk value, so a normal
  full 8-sensor-plus-buzzer poll blows the session deadline through no
  fault of the code under test. That's the right trade-off for F1's
  hang-injection tests; it is wrong for this file's plain happy-path polls.
  ``quick_ble`` shrinks every per-step delay/guard/timeout the same way but
  leaves the session deadline generously large.
* F4-04/18 need to prove the *scheduling* cadence itself. They do **not**
  use freezegun: ``async_fire_time_changed(hass, target_datetime)`` directly
  runs whatever real ``loop.call_at`` timer is already due by
  ``target_datetime`` (see ``pytest_homeassistant_custom_component.common``)
  without needing wall-clock time to actually pass, so a plain
  ``dt_util.utcnow() + timedelta(...)`` target is enough — no clock
  patching needed or wanted here.
* F4-08/09/10 need to fast-forward the sensor entity's own 20-minute cache
  clock, which is plain, *naive, local* ``datetime.now()`` (see
  ``sensor.py``), not anything HA's dt_util/event-loop scheduling touches.
  They use ``freezegun.freeze_time(datetime.now(), tick=True)`` — seeded
  from local naive ``datetime.now()`` (matching what the entity actually
  calls; seeding from ``dt_util.utcnow()`` instead silently jumps the local
  clock backwards by the system's UTC offset the moment the freeze starts,
  which made the entity's own timestamp look like it was still fresh) —
  and drive the actual re-poll with a direct, bounded
  ``coordinator.async_refresh()`` call (via the ``_refresh`` helper below)
  rather than the real interval scheduler; ``tick=True`` keeps real
  ``asyncio`` timers usable in case any await path still needs one.
* Every poll/refresh in this file goes through ``_refresh``, which wraps it
  in ``asyncio.wait_for`` — a regression that reintroduces an unbounded BLE
  await (D2) fails these tests with a clear timeout instead of hanging the
  suite.
* Every ``hass.async_block_till_done()`` call in this file passes
  ``wait_background_tasks=True`` (default ``False``): the coordinator's own
  interval-triggered refresh runs via
  ``config_entry.async_create_background_task`` (HA's
  ``DataUpdateCoordinator.__wrap_handle_refresh_interval``), and the
  session's link teardown runs via ``hass.async_create_task`` from inside
  the poll — without this flag, ``async_block_till_done()`` can return
  before either has actually finished (observed as a scheduled poll
  silently not happening, or the next scheduled timer not existing yet).

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_coordinator_entities.py -q
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.em1003 import const as em1003_const

from .conftest import MAC

# ---------------------------------------------------------------------------
# Local helpers (per the task brief: build what's missing here rather than
# touching tests/fakes.py or tests/conftest.py).
# ---------------------------------------------------------------------------

DOMAIN = em1003_const.DOMAIN

#: Known-good engineering values for every sensor id, chosen so each one
#: round-trips exactly through FakeBleWorld.set_sensor_value's raw
#: conversion and back through device.py's parse formula (no rounding
#: surprises for the state-value assertions below).
_KNOWN_VALUES: dict[int, float | int] = {
    em1003_const.SENSOR_ID_01: 23.4,     # temperature, exact at *100
    em1003_const.SENSOR_ID_06: 45.7,     # humidity, exact at *100
    em1003_const.SENSOR_ID_08: 52,       # noise, direct int
    em1003_const.SENSOR_ID_09: 31,       # pm2.5, direct int
    em1003_const.SENSOR_ID_0A: 0.062,    # formaldehyde, exact at *1000
    em1003_const.SENSOR_ID_11: 19,       # pm10, direct int
    em1003_const.SENSOR_ID_12: 24,       # tvoc, direct int
    em1003_const.SENSOR_ID_13: 523,      # eco2, direct int
}

#: Display precision the plan (BC-12) pins for each sensor id; everything
#: not listed here is 0.
_PRECISION_BY_SENSOR_ID: dict[int, int] = {
    em1003_const.SENSOR_ID_01: 2,
    em1003_const.SENSOR_ID_06: 1,
    em1003_const.SENSOR_ID_0A: 3,
}


@pytest.fixture
def quick_ble(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink every BLE_* per-step delay/guard/timeout so a poll or a
    buzzer toggle finishes in well under a second, but leave
    ``BLE_SESSION_TIMEOUT`` generously large.

    Built locally rather than reusing ``conftest.py``'s ``fast_timeouts``:
    that fixture shrinks ``BLE_SESSION_TIMEOUT`` to 0.2s too, which is
    *tighter* than ``7 * BLE_INTER_REQUEST_DELAY`` even at its own shrunk
    0.05s value — exactly right for F1's hang-injection tests (where the
    session is expected to abort), wrong for this file's plain full
    8-sensor-plus-buzzer happy-path polls, which would then spuriously blow
    the session deadline on every single test.
    """
    for name, value in (
        ("BLE_CONNECT_TIMEOUT", 0.1),
        ("BLE_START_NOTIFY_TIMEOUT", 0.1),
        ("BLE_WRITE_TIMEOUT", 0.1),
        ("BLE_RESPONSE_TIMEOUT", 0.2),  # margin over the 0.05s response_delay used in F4-17
        ("BLE_READ_CHAR_TIMEOUT", 0.1),
        ("BLE_STOP_NOTIFY_TIMEOUT", 0.1),
        ("BLE_DISCONNECT_TIMEOUT", 0.1),
        ("BLE_RECONNECT_GUARD", 0.01),
        ("BLE_INTER_REQUEST_DELAY", 0.01),
        ("BLE_BUZZER_SETTLE_DELAY", 0.01),
        ("BLE_SESSION_TIMEOUT", 5.0),
    ):
        monkeypatch.setattr(em1003_const, name, value)


def _apply_known_values(world) -> None:
    """Set every sensor id on `world` to its `_KNOWN_VALUES` reading."""
    for sid, value in _KNOWN_VALUES.items():
        world.set_sensor_value(sid, value)


def _expected_unit(raw_unit: str | None) -> str | None:
    """HA rewrites the legacy micro-sign spelling (µ, U+00B5) of a unit to
    the recommended Greek-mu spelling (μ, U+03BC) for every sensor entity
    (see ``homeassistant.components.sensor.const.AMBIGUOUS_UNITS``) — a
    permanent, integration-independent HA normalisation, so the "unit
    unchanged" assertion has to compare against *that*, not the raw
    ``SENSOR_TYPES`` string."""
    return raw_unit.replace("µ", "μ") if raw_unit else raw_unit


def _f4_entry(mock_entry, *, options: dict | None = None):
    """Build the config entry the F4 matrix preamble specifies: a MAC entry
    with a *cached* device name (so setup makes zero name-read connections
    and every `establish_connection` call in a test is attributable to a
    poll or a buzzer toggle)."""
    return mock_entry(MAC, data={"device_name": "3GCG300ZY4"}, options=options)


def _sensor_entity_id(hass, sensor_id: int) -> str | None:
    key = em1003_const.SENSOR_TYPES[sensor_id]["key"]
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{MAC}_{key}")


def _switch_entity_id(hass) -> str | None:
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{MAC}_buzzer")


def _coordinator(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


def _device(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["device"]


async def _refresh(hass, coordinator, *, timeout: float = 5.0) -> None:
    """Trigger one on-demand coordinator refresh, bounded so a regression
    that reintroduces an unbounded BLE await (D2) fails loudly instead of
    hanging the suite."""
    await asyncio.wait_for(coordinator.async_refresh(), timeout=timeout)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _call_switch(hass, entity_id: str, service: str, *, timeout: float = 5.0) -> None:
    await asyncio.wait_for(
        hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True),
        timeout=timeout,
    )


# ===========================================================================
# F4-01..03: sensor entity shape (values, units, device classes, precision)
# ===========================================================================


async def test_f4_01_all_sensors_correct_state(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-01: all 8 sensor entities exist with values/units/device classes
    matching SENSOR_TYPES and formulas, and state_class == measurement."""
    _apply_known_values(ble_world)
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)

    for sensor_id, info in em1003_const.SENSOR_TYPES.items():
        entity_id = _sensor_entity_id(hass, sensor_id)
        assert entity_id is not None, f"missing entity for sensor 0x{sensor_id:02x}"
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state not in ("unknown", "unavailable")
        assert float(state.state) == pytest.approx(_KNOWN_VALUES[sensor_id])
        assert state.attributes.get("unit_of_measurement") == _expected_unit(info["unit"])
        assert state.attributes.get("device_class") == info["device_class"]
        assert state.attributes.get("state_class") == "measurement"


async def test_f4_02_sensor_unique_ids(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-02: every sensor's unique_id is registered under this config entry."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)

    registry = er.async_get(hass)
    expected = {f"{MAC}_{info['key']}" for info in em1003_const.SENSOR_TYPES.values()}
    actual = {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "sensor"
    }
    assert actual == expected


async def test_f4_03_display_precision(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-03: temperature=2, humidity=1, formaldehyde=3, everything else 0."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)

    registry = er.async_get(hass)
    for sensor_id, info in em1003_const.SENSOR_TYPES.items():
        entity_id = _sensor_entity_id(hass, sensor_id)
        reg_entry = registry.async_get(entity_id)
        assert reg_entry is not None
        precision = reg_entry.options.get("sensor", {}).get("suggested_display_precision")
        assert precision == _PRECISION_BY_SENSOR_ID.get(sensor_id, 0), (
            f"{info['key']}: expected precision {_PRECISION_BY_SENSOR_ID.get(sensor_id, 0)}, got {precision}"
        )


# ===========================================================================
# F4-04: one poll per interval, one connection per poll
# ===========================================================================


async def test_f4_04_one_poll_per_interval(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-04: establish_connection grows by exactly 1 per scan interval, and
    the fake world never sees more than one link open at once."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    assert ble_world.connect_attempts == 1  # the first poll, during setup

    base = dt_util.utcnow()
    async_fire_time_changed(hass, base + timedelta(seconds=60))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ble_world.connect_attempts == 2

    async_fire_time_changed(hass, base + timedelta(seconds=120))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ble_world.connect_attempts == 3

    assert ble_world.max_concurrent_connects == 1


# ===========================================================================
# F4-05..07: coordinator data / failure semantics
# ===========================================================================


async def test_f4_05_coordinator_returns_sensor_dict(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-05: coordinator.data is a dict keyed by the 8 sensor ids."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)

    assert coordinator.last_update_success is True
    assert set(coordinator.data.keys()) == set(em1003_const.SENSOR_TYPES.keys())


async def test_f4_06_all_none_poll_is_update_failed(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-06: a poll where nothing answers -> UpdateFailed, no exception
    escapes, and the previous data dict is kept (not wiped)."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    assert coordinator.last_update_success is True
    previous_data = dict(coordinator.data)

    ble_world.silent_sensors = set(em1003_const.SENSOR_TYPES.keys())
    ble_world.buzzer_query_silent = True
    await _refresh(hass, coordinator)  # must not raise out of this call

    assert coordinator.last_update_success is False
    assert coordinator.data == previous_data


async def test_f4_07_breaker_open_poll_is_update_failed_no_ble(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-07: with the breaker forced OPEN, a refresh fails without any new
    `establish_connection` call."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    device = _device(hass, entry)

    for _ in range(3):
        device.circuit_breaker.record_failure()
    assert device.circuit_breaker.state == "OPEN"

    connects_before = ble_world.connect_attempts
    await _refresh(hass, coordinator)

    assert coordinator.last_update_success is False
    assert ble_world.connect_attempts == connects_before


# ===========================================================================
# F4-08..10: the 20-minute stale-value cache
# ===========================================================================


async def test_f4_08_cached_value_kept_under_20_minutes(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-08: after one good poll, a dead device 10 minutes later still
    shows the last good values (not unavailable)."""
    _apply_known_values(ble_world)
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    assert coordinator.last_update_success is True

    freezer = freeze_time(datetime.now(), tick=True)
    clock = freezer.start()
    try:
        ble_world.device_present = False
        clock.tick(timedelta(minutes=10))
        await _refresh(hass, coordinator)
        assert coordinator.last_update_success is False

        for sensor_id in em1003_const.SENSOR_TYPES:
            entity_id = _sensor_entity_id(hass, sensor_id)
            state = hass.states.get(entity_id)
            assert state.state != "unavailable", f"{entity_id} went unavailable too early"
            assert float(state.state) == pytest.approx(_KNOWN_VALUES[sensor_id])
    finally:
        freezer.stop()


async def test_f4_09_unavailable_after_20_minutes(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-09: 21 minutes after the last good poll, with the device still
    dead, entities go unavailable."""
    _apply_known_values(ble_world)
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)

    freezer = freeze_time(datetime.now(), tick=True)
    clock = freezer.start()
    try:
        ble_world.device_present = False
        clock.tick(timedelta(minutes=21))
        await _refresh(hass, coordinator)
        assert coordinator.last_update_success is False

        for sensor_id in em1003_const.SENSOR_TYPES:
            entity_id = _sensor_entity_id(hass, sensor_id)
            assert hass.states.get(entity_id).state == "unavailable"
    finally:
        freezer.stop()


async def test_f4_10_recovery_after_unavailable(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-10: once the device answers again, entities return to fresh
    values and availability."""
    _apply_known_values(ble_world)
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)

    freezer = freeze_time(datetime.now(), tick=True)
    clock = freezer.start()
    try:
        ble_world.device_present = False
        clock.tick(timedelta(minutes=21))
        await _refresh(hass, coordinator)
        for sensor_id in em1003_const.SENSOR_TYPES:
            entity_id = _sensor_entity_id(hass, sensor_id)
            assert hass.states.get(entity_id).state == "unavailable"

        ble_world.device_present = True
        clock.tick(timedelta(seconds=1))
        await _refresh(hass, coordinator)
        assert coordinator.last_update_success is True

        for sensor_id in em1003_const.SENSOR_TYPES:
            entity_id = _sensor_entity_id(hass, sensor_id)
            state = hass.states.get(entity_id)
            assert state.state not in ("unknown", "unavailable")
            assert float(state.state) == pytest.approx(_KNOWN_VALUES[sensor_id])
    finally:
        freezer.stop()


# ===========================================================================
# F4-11: partial poll keeps the rest fresh
# ===========================================================================


async def test_f4_11_partial_poll_keeps_others_fresh(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-11: when one sensor stops answering, the other 7 update to their
    new values while the silent one keeps serving its old cached value."""
    _apply_known_values(ble_world)
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)

    silent_id = em1003_const.SENSOR_ID_13
    old_eco2 = _KNOWN_VALUES[silent_id]

    updated_values = {sid: (v + 1) for sid, v in _KNOWN_VALUES.items() if sid != silent_id}
    for sid, v in updated_values.items():
        ble_world.set_sensor_value(sid, v)
    ble_world.silent_sensors = {silent_id}

    await _refresh(hass, coordinator)

    assert coordinator.last_update_success is True
    assert coordinator.data[silent_id] is None

    for sensor_id in em1003_const.SENSOR_TYPES:
        entity_id = _sensor_entity_id(hass, sensor_id)
        state = hass.states.get(entity_id)
        assert state.state not in ("unknown", "unavailable")
        if sensor_id == silent_id:
            assert float(state.state) == pytest.approx(old_eco2), "silent sensor must keep its cached value"
        else:
            assert float(state.state) == pytest.approx(updated_values[sensor_id]), "the other sensors must update"


# ===========================================================================
# F4-12..17: the buzzer switch
# ===========================================================================


async def test_f4_12_switch_state_from_poll_no_own_connection(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-12: the switch shows the polled buzzer state, and setup opens
    exactly 1 connection total (the poll) — the switch never connects."""
    ble_world.buzzer_state = True
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)

    switch_entity_id = _switch_entity_id(hass)
    assert switch_entity_id is not None
    assert hass.states.get(switch_entity_id).state == "on"
    assert ble_world.connect_attempts == 1

    buzzer_writes = [w for w in ble_world.writes if w.data[1] == em1003_const.CMD_BUZZER]
    assert len(buzzer_writes) == 1
    # A query frame is 3 bytes [seq, 0x50, 0x00]; a SET frame is 4 bytes.
    assert len(buzzer_writes[0].data) == 3, "startup must query, never set, the buzzer"


async def test_f4_13_switch_unknown_before_first_successful_poll(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-13: with the device dead at startup, the switch is `unknown`
    (not `unavailable`), and the entity still exists."""
    ble_world.device_present = False
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)

    switch_entity_id = _switch_entity_id(hass)
    assert switch_entity_id is not None
    state = hass.states.get(switch_entity_id)
    assert state is not None
    assert state.state == "unknown"


async def test_f4_14_turn_on_writes_correct_bytes_and_verifies(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-14: turn_on writes [seq,0x50,0x01,0x01] then verifies with
    [seq2,0x50,0x00]; the state becomes `on`; exactly 1 new connection."""
    ble_world.buzzer_state = False
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    switch_entity_id = _switch_entity_id(hass)
    connects_before = ble_world.connect_attempts
    writes_before = len(ble_world.writes)

    await _call_switch(hass, switch_entity_id, "turn_on")

    assert ble_world.connect_attempts == connects_before + 1
    new_buzzer_writes = [w for w in ble_world.writes[writes_before:] if w.data[1] == em1003_const.CMD_BUZZER]
    assert len(new_buzzer_writes) == 2
    set_write, verify_write = new_buzzer_writes
    assert set_write.data[1:] == bytes([em1003_const.CMD_BUZZER, 0x01, em1003_const.BUZZER_ON])
    assert verify_write.data[1:] == bytes([em1003_const.CMD_BUZZER, 0x00])

    assert hass.states.get(switch_entity_id).state == "on"


async def test_f4_15_turn_off_writes_correct_bytes(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-15: turn_off writes [...,0x01,0x00] + verify; state becomes `off`."""
    ble_world.buzzer_state = True
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    switch_entity_id = _switch_entity_id(hass)
    writes_before = len(ble_world.writes)

    await _call_switch(hass, switch_entity_id, "turn_off")

    new_buzzer_writes = [w for w in ble_world.writes[writes_before:] if w.data[1] == em1003_const.CMD_BUZZER]
    assert len(new_buzzer_writes) == 2
    set_write, verify_write = new_buzzer_writes
    assert set_write.data[1:] == bytes([em1003_const.CMD_BUZZER, 0x01, em1003_const.BUZZER_OFF])
    assert verify_write.data[1:] == bytes([em1003_const.CMD_BUZZER, 0x00])

    assert hass.states.get(switch_entity_id).state == "off"


async def test_f4_16_failed_set_surfaces_error_and_recovers(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-16: a set whose verification mismatches raises HomeAssistantError
    and drops availability; the next good poll restores it."""
    ble_world.buzzer_state = False
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    switch_entity_id = _switch_entity_id(hass)

    # The SET is acked normally, but the device doesn't actually flip state,
    # so the verify query reports a mismatch -> set_buzzer_state() is False.
    ble_world.apply_buzzer_set = False
    with pytest.raises(HomeAssistantError):
        await _call_switch(hass, switch_entity_id, "turn_on")

    assert hass.states.get(switch_entity_id).state == "unavailable"

    ble_world.apply_buzzer_set = True  # restore normal device behaviour
    await _refresh(hass, coordinator)

    state = hass.states.get(switch_entity_id)
    assert state.state == "off"  # the device's real (unflipped) buzzer state
    assert state is not None


async def test_f4_17_toggle_during_poll_is_serialized(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-17: a toggle issued while a poll is in flight never opens a second
    concurrent link; both finish and the switch ends on the verified value."""
    ble_world.buzzer_state = False
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    switch_entity_id = _switch_entity_id(hass)

    ble_world.response_delay = 0.05  # slow enough that the toggle lands mid-poll
    connects_before = ble_world.connect_attempts

    poll_task = hass.async_create_task(coordinator.async_refresh(), "test-f4-17-poll")
    await asyncio.sleep(0.01)  # let the poll acquire the device lock first
    toggle_task = hass.async_create_task(
        hass.services.async_call("switch", "turn_on", {"entity_id": switch_entity_id}, blocking=True),
        "test-f4-17-toggle",
    )

    await asyncio.wait_for(asyncio.gather(poll_task, toggle_task), timeout=5.0)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert ble_world.max_concurrent_connects == 1, "poll and toggle must never hold two links at once"
    assert ble_world.connect_attempts == connects_before + 2, "both operations must still have run"
    assert ble_world.links_are_clean
    assert hass.states.get(switch_entity_id).state == "on"


# ===========================================================================
# F4-18..20: scan interval / coordinator identity
# ===========================================================================


async def test_f4_18_scan_interval_from_options(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-18: options={"scan_interval": 120} -> no poll at +60s, one at +120s."""
    entry = mock_entry(MAC, data={"device_name": "3GCG300ZY4"}, options={"scan_interval": 120})
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    assert coordinator.update_interval == timedelta(seconds=120)

    connects_after_setup = ble_world.connect_attempts
    base = dt_util.utcnow()

    async_fire_time_changed(hass, base + timedelta(seconds=60))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ble_world.connect_attempts == connects_after_setup, "must not poll yet at +60s"

    async_fire_time_changed(hass, base + timedelta(seconds=120))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ble_world.connect_attempts == connects_after_setup + 1, "must poll at +120s"


async def test_f4_19_default_interval_when_options_empty(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-19: options={} -> coordinator.update_interval defaults to 60s."""
    entry = mock_entry(MAC, data={"device_name": "3GCG300ZY4"}, options={})
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)

    assert coordinator.update_interval == timedelta(seconds=em1003_const.DEFAULT_SCAN_INTERVAL)
    assert em1003_const.DEFAULT_SCAN_INTERVAL == 60


async def test_f4_20_coordinator_identity_and_module(hass, ble_world, mock_entry, setup_integration, quick_ble):
    """F4-20: the coordinator class lives in coordinator.py and sensor.py's
    re-export is the exact same class object."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)

    from custom_components.em1003 import sensor as em1003_sensor

    assert type(coordinator).__name__ == "EM1003DataUpdateCoordinator"
    assert type(coordinator).__module__.endswith("coordinator")
    assert em1003_sensor.EM1003DataUpdateCoordinator is coordinator.__class__


# ===========================================================================
# F4-21: buzzer state survives a failed buzzer query
# ===========================================================================


async def test_f4_21_buzzer_state_survives_failed_query(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-21: a poll whose sensors succeed but whose buzzer query times out
    must not wipe the last known buzzer state."""
    ble_world.buzzer_state = True
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    switch_entity_id = _switch_entity_id(hass)
    assert coordinator.buzzer_state is True
    assert hass.states.get(switch_entity_id).state == "on"

    ble_world.buzzer_query_silent = True
    await _refresh(hass, coordinator)

    assert coordinator.last_update_success is True  # sensors still answered
    assert coordinator.buzzer_state is True
    assert hass.states.get(switch_entity_id).state == "on"


# ===========================================================================
# F4-X1, X2: additional regression guards found while writing this file
# ===========================================================================


async def test_f4_x1_turn_on_bypasses_open_breaker_and_closes_it(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-X1: a user-initiated toggle reaches the device even while the
    poll breaker is OPEN (plan section 4.6), through the real switch/service
    layer, and a successful bypassed toggle closes the breaker so the very
    next scheduled poll is admitted again. Not spelled out as its own F4 row,
    but the plan's bypass rule is meaningless if the entity layer routes the
    toggle through the same breaker gate as a poll — this pins that it does
    not."""
    ble_world.buzzer_state = False
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)
    coordinator = _coordinator(hass, entry)
    device = _device(hass, entry)
    switch_entity_id = _switch_entity_id(hass)

    for _ in range(3):
        device.circuit_breaker.record_failure()
    assert device.circuit_breaker.state == "OPEN"

    connects_before = ble_world.connect_attempts
    await _call_switch(hass, switch_entity_id, "turn_on")

    assert ble_world.connect_attempts == connects_before + 1, "the bypass must still open a real link"
    assert device.circuit_breaker.state == "CLOSED"
    assert device.circuit_breaker.failure_count == 0
    assert hass.states.get(switch_entity_id).state == "on"

    # The breaker being closed again must let the *next* poll through too.
    connects_before = ble_world.connect_attempts
    await _refresh(hass, coordinator)
    assert coordinator.last_update_success is True
    assert ble_world.connect_attempts == connects_before + 1


async def test_f4_x2_one_session_serves_both_sensors_and_buzzer(
    hass, ble_world, mock_entry, setup_integration, quick_ble
):
    """F4-X2: the entities are fed by exactly one BLE session per poll,
    covering all 8 sensor requests and the buzzer query on the same
    connection — a direct regression guard for D4 (the pre-refactor code
    opened one link for sensors and a second one for the buzzer)."""
    entry = _f4_entry(mock_entry)
    assert await setup_integration(hass, entry)

    assert ble_world.connect_attempts == 1
    sensor_write_keys = {w.data[2] for w in ble_world.writes if w.data[1] == em1003_const.CMD_READ_SENSOR}
    assert sensor_write_keys == set(em1003_const.SENSOR_TYPES.keys())
    buzzer_writes = [w for w in ble_world.writes if w.data[1] == em1003_const.CMD_BUZZER]
    assert len(buzzer_writes) == 1
    client_ids = {w.client_id for w in ble_world.writes}
    assert len(client_ids) == 1, "all of a poll's requests must share one connection"
