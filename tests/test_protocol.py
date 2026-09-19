"""F3 — Protocol parsing & request matching tests (docs/ble_session_refactor.md
section 4.1 / 4.8 / 10).

Owned by the F3 test author (see section 11, "File ownership"). Implements
every case in the F3 matrix (23 cases) plus a handful of additional
regression guards marked ``F3-Xn``.

Three layers are covered, per the plan:

* **Pure unit tests** (F3-01..F3-09, plus F3-X1..F3-X5) against the
  stateless helpers in ``device.py`` — ``parse_sensor_value``,
  ``apply_negative_filter``, ``build_sensor_request``, ``build_buzzer_query``,
  ``build_buzzer_set``, ``match_key``. No ``hass``, no fake BLE world: these
  can never hang and never need a timeout budget.
* **Device-level tests against the full public API** (F3-10..F3-18, F3-X6)
  using ``EM1003Device.async_poll`` / ``read_all_sensors`` /
  ``read_buzzer_state`` / ``set_buzzer_state`` through the ``ble_world`` +
  ``fast_timeouts`` fixtures — these prove the parsing/matching contract as
  the rest of the integration actually observes it (stale-value retention,
  wire bytes, buzzer ack/verify).
* **Direct request/response machinery tests** (F3-19..F3-23) that open one
  fake link and call ``EM1003Device._async_request`` /
  ``_notification_handler`` / ``_alloc_seq_id`` / ``_release_seq_id``
  directly, bypassing the lock/breaker/session machinery entirely. This is
  deliberate: those concerns (one link at a time, timeouts, teardown) are
  F1's ("test_device_session.py") responsibility, not F3's. Testing the
  matching/allocation contract in isolation, with an already-open link,
  keeps these tests fast, deterministic, and focused on exactly what F3
  owns: "does the right response complete the right, and only the right,
  pending request".

The source under test (``custom_components/em1003/device.py``) is being
rewritten concurrently by another agent to match the plan's contract. These
tests are written against **that contract** (section 4.1 for the pure
parsers, section 4.8 for the request/response machinery), not against the
pre-refactor code — see the module docstring of ``tests/test_smoke.py`` for
why a plain ``import`` failure elsewhere is expected and tolerated, but a
fixture-name or syntax error in *this* file is not.

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_protocol.py -q
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bleak.exc import BleakError

from custom_components.em1003 import const as em1003_const
from custom_components.em1003.device import (
    BUZZER_QUERY_KEY,
    BUZZER_SET_KEY,
    EM1003Device,
    apply_negative_filter,
    build_buzzer_query,
    build_buzzer_set,
    build_sensor_request,
    match_key,
    parse_sensor_value,
)

from .conftest import MAC
from .fakes import FakeBleakClient, build_frame, encode_raw

# ---------------------------------------------------------------------------
# Local test helpers.
#
# Per the task brief: "If the scaffold lacks a knob you need, build the
# helper locally inside your test file rather than editing tests/fakes.py."
# ---------------------------------------------------------------------------


async def _bounded(coro, timeout: float = 5.0):
    """Await `coro` under a hard wall-clock bound.

    A regression that reintroduces an unbounded await on the BLE path (D2)
    must fail this test suite loudly instead of hanging it forever.
    """
    return await asyncio.wait_for(coro, timeout=timeout)


async def _wait_for_writes(ble_world, count: int, timeout: float = 1.0) -> None:
    """Block until at least `count` writes have been recorded on `ble_world`.

    Needed because a request task's first suspension point is the response
    future (not the write itself, which the fake completes synchronously) —
    polling on the write count is a deterministic way to know "the request
    has definitely been sent" before injecting a follow-up notification,
    without guessing how many `asyncio.sleep(0)` calls that takes.
    """

    async def _poll() -> None:
        while len(ble_world.writes) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout)


async def _open_client(device: EM1003Device, ble_world: Any) -> FakeBleakClient:
    """Open one fake link and wire up notifications exactly as the real
    session does (plan section 4.7 steps 5-7), WITHOUT engaging the device's
    lock/breaker/teardown machinery.

    Used only by the direct request/response-machinery tests (F3-19..F3-23),
    which exercise `_async_request` / `_notification_handler` / sequence-id
    allocation in isolation — full-session concerns (locking, timeouts on
    connect/start_notify/disconnect, teardown-on-cancellation) are F1's
    responsibility, tested in `test_device_session.py`.
    """
    client = await ble_world.establish_connection(
        None,
        None,
        device.mac_address,
        disconnected_callback=device._handle_disconnect,
        max_attempts=1,
        timeout=1.0,
    )
    device._client = client
    await client.start_notify(em1003_const.EM1003_NOTIFY_CHAR_UUID, device._notification_handler)
    return client


@pytest.fixture
def quick_poll_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink only the per-step PACING delays (`BLE_INTER_REQUEST_DELAY`,
    `BLE_BUZZER_SETTLE_DELAY`, `BLE_RECONNECT_GUARD`) so a full, healthy
    8-sensor sweep runs in well under 0.2s of real sleep — WITHOUT touching
    `BLE_SESSION_TIMEOUT` or any per-await bound.

    Deliberately NOT the shared `fast_timeouts` fixture: that shrinks
    `BLE_SESSION_TIMEOUT` down to 0.2s too, which is *tighter* than
    `BLE_INTER_REQUEST_DELAY`'s own shrunk value (0.05s) times the 7 gaps in
    a full 8-sensor sweep (0.35s). Against a perfectly healthy world (no
    hang, no fault injected) that combination trips the session deadline
    partway through the sweep and returns partial/None values for reasons
    that have nothing to do with what these tests check (filtering, exact
    wire bytes, garbage-frame tolerance) — a false pass/fail that would mask
    the real behaviour under test. `fast_timeouts` remains the right tool
    for hang/fault-injection tests (F1's domain); this one is for "just make
    the healthy-path pacing delays fast".
    """
    monkeypatch.setattr(em1003_const, "BLE_INTER_REQUEST_DELAY", 0.01)
    monkeypatch.setattr(em1003_const, "BLE_BUZZER_SETTLE_DELAY", 0.01)
    monkeypatch.setattr(em1003_const, "BLE_RECONNECT_GUARD", 0.0)


@pytest.fixture
def write_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes, bool]]:
    """Records every `(char_uuid, data, response)` passed to
    `FakeBleakClient.write_gatt_char`, across every connection opened during
    the test.

    Built locally (not added to tests/fakes.py) because the scaffold's own
    write accounting (`ble_world.writes`) does not capture the `response`
    kwarg, and F3-12/13/14 need to assert `response=False` exactly as
    device.py's session machinery (plan section 4.8) is required to pass it.
    """
    calls: list[tuple[str, bytes, bool]] = []
    original = FakeBleakClient.write_gatt_char

    async def _spy(self: FakeBleakClient, char_uuid: str, data: Any, response: bool = False) -> None:
        calls.append((char_uuid, bytes(data), response))
        await original(self, char_uuid, data, response=response)

    monkeypatch.setattr(FakeBleakClient, "write_gatt_char", _spy)
    return calls


# ===========================================================================
# Pure unit tests: parse_sensor_value (F3-01..F3-07)
# ===========================================================================


def test_f3_01_temperature_formula_and_endianness():
    """F3-01: temperature formula (raw - 4000) / 100, little-endian."""
    value = parse_sensor_value(em1003_const.SENSOR_ID_01, b"\x2c\x1a")
    assert value == pytest.approx(27.00)
    assert isinstance(value, float)


def test_f3_02_temperature_below_zero_not_filtered():
    """F3-02: a below-zero temperature is a valid float and is NEVER
    filtered — 0x01 is exempt from the negative filter even though the
    result is negative."""
    value = parse_sensor_value(em1003_const.SENSOR_ID_01, b"\x3c\x0f")  # raw 3900
    assert value == pytest.approx(-1.0)
    assert apply_negative_filter(em1003_const.SENSOR_ID_01, value) == pytest.approx(-1.0)


def test_f3_03_humidity_formula():
    """F3-03: humidity formula raw / 100."""
    value = parse_sensor_value(em1003_const.SENSOR_ID_06, b"\xdb\x11")  # raw 4571
    assert value == pytest.approx(45.71)


def test_f3_04_direct_sensors_keep_int():
    """F3-04: every "direct" sensor id returns the raw uint16 unchanged,
    typed as `int` — never coerced to float."""
    cases = {
        em1003_const.SENSOR_ID_08: (b"\x31\x00", 49),
        em1003_const.SENSOR_ID_09: (b"\x1c\x00", 28),
        em1003_const.SENSOR_ID_11: (b"\x16\x00", 22),
        em1003_const.SENSOR_ID_12: (b"\x01\x00", 1),
        em1003_const.SENSOR_ID_13: (b"\x9b\x01", 411),
    }
    for sensor_id, (raw_bytes, expected) in cases.items():
        value = parse_sensor_value(sensor_id, raw_bytes)
        assert value == expected, f"sensor 0x{sensor_id:02x}"
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"sensor 0x{sensor_id:02x} must stay a plain int, got {type(value)}"
        )


def test_f3_05_formaldehyde_baseline():
    """F3-05: formaldehyde raw 16384 is exactly the zero baseline."""
    value = parse_sensor_value(em1003_const.SENSOR_ID_0A, encode_raw(16384))
    assert value == pytest.approx(0.0)


def test_f3_06_formaldehyde_below_baseline_is_negative_and_filtered():
    """F3-06: formaldehyde raw < 16384 parses to a negative value, which the
    negative filter then drops to None."""
    value = parse_sensor_value(em1003_const.SENSOR_ID_0A, encode_raw(16000))
    assert value == pytest.approx(-0.384)
    assert apply_negative_filter(em1003_const.SENSOR_ID_0A, value) is None


def test_f3_07_0xffff_boundary_for_every_id():
    """F3-07: the raw uint16 boundary 0xFFFF never raises for any sensor id,
    and the formula ids compute their documented boundary values."""
    raw_bytes = encode_raw(0xFFFF)
    exact_formula = {
        em1003_const.SENSOR_ID_01: 615.35,
        em1003_const.SENSOR_ID_06: 655.35,
        em1003_const.SENSOR_ID_0A: 49.151,
    }
    for sensor_id in em1003_const.SENSOR_TYPES:
        value = parse_sensor_value(sensor_id, raw_bytes)
        if sensor_id in exact_formula:
            assert value == pytest.approx(exact_formula[sensor_id]), f"sensor 0x{sensor_id:02x}"
        else:
            assert value == 65535, f"sensor 0x{sensor_id:02x}"
            assert isinstance(value, int)


# ===========================================================================
# Pure unit tests: apply_negative_filter (F3-08, F3-09)
# ===========================================================================


def test_f3_08_filter_set_is_exactly_bc18():
    """F3-08: the negative filter applies to exactly the BC-18 id set, and
    nowhere else — a wrong id added or removed here would silently change
    which sensors can report a stale/bogus reading vs. going unavailable."""
    filtered_ids = set(em1003_const.NEGATIVE_FILTER_SENSOR_IDS)
    assert filtered_ids == {0x06, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13}

    for sensor_id in em1003_const.SENSOR_TYPES:
        result = apply_negative_filter(sensor_id, -1)
        if sensor_id in filtered_ids:
            assert result is None, f"sensor 0x{sensor_id:02x} must be filtered"
        else:
            assert result == -1, f"sensor 0x{sensor_id:02x} must NOT be filtered"
    assert em1003_const.SENSOR_ID_01 not in filtered_ids


def test_f3_09_filter_passes_zero_and_positives():
    """F3-09: zero and positive values pass through unchanged for every
    sensor id, filtered or not."""
    for sensor_id in em1003_const.SENSOR_TYPES:
        assert apply_negative_filter(sensor_id, 0) == 0, f"sensor 0x{sensor_id:02x}"
        assert apply_negative_filter(sensor_id, 1) == 1, f"sensor 0x{sensor_id:02x}"


# ===========================================================================
# Device-level tests: stale value retention across polls (F3-10, F3-11)
# ===========================================================================


async def _poll_with_formaldehyde_raw(device: EM1003Device, ble_world: Any, raw: int):
    ble_world.set_sensor_raw(em1003_const.SENSOR_ID_0A, raw)
    return await _bounded(device.async_poll())


async def test_f3_10_filtered_reading_is_none_but_stale_value_kept(hass, ble_world, quick_poll_timeouts):
    """F3-10: a filtered reading reports `None` in that poll's result, while
    `device.sensor_data` keeps serving the last valid value (the entity-level
    20-minute cache relies on this)."""
    device = EM1003Device(hass, MAC)

    poll1 = await _poll_with_formaldehyde_raw(device, ble_world, 16384)
    assert poll1.ok is True
    assert poll1.sensors[em1003_const.SENSOR_ID_0A] == pytest.approx(0.0)
    assert device.sensor_data[em1003_const.SENSOR_ID_0A] == pytest.approx(0.0)

    poll2 = await _poll_with_formaldehyde_raw(device, ble_world, 16000)
    assert poll2.ok is True, "the other 7 sensors still answered"
    assert poll2.sensors[em1003_const.SENSOR_ID_0A] is None
    assert device.sensor_data[em1003_const.SENSOR_ID_0A] == pytest.approx(0.0), "stale value must survive"


async def test_f3_11_filtered_reading_not_reported_as_fresh_twice(hass, ble_world, quick_poll_timeouts):
    """F3-11: the historic D8 bug re-reported the previous value as if it
    were a fresh reading. The poll result must carry `None`, not a copy of
    the stale value, for the sensor that was just filtered."""
    device = EM1003Device(hass, MAC)

    await _poll_with_formaldehyde_raw(device, ble_world, 16384)
    poll2 = await _poll_with_formaldehyde_raw(device, ble_world, 16000)

    assert poll2.sensors[em1003_const.SENSOR_ID_0A] is None, (
        "the previous valid value must not be copied into the poll result as if it were fresh"
    )


# ===========================================================================
# Device-level tests: exact wire bytes (F3-12, F3-13, F3-14)
# ===========================================================================


async def test_f3_12_request_bytes_on_wire_sensor(hass, ble_world, quick_poll_timeouts, write_spy):
    """F3-12: for a healthy poll, every sensor request frame on the wire is
    exactly `bytes([seq, 0x06, sensor_id])`, sent with `response=False`, in
    `SENSOR_TYPES` order."""
    device = EM1003Device(hass, MAC)

    result = await _bounded(device.async_poll(), timeout=10.0)
    assert result.ok is True

    sensor_writes = [call for call in write_spy if call[1][1] == em1003_const.CMD_READ_SENSOR]
    assert len(sensor_writes) == len(em1003_const.SENSOR_TYPES)

    for (char_uuid, data, response), sensor_id in zip(sensor_writes, em1003_const.SENSOR_TYPES):
        assert char_uuid == em1003_const.EM1003_WRITE_CHAR_UUID
        assert len(data) == 3
        assert data[1] == em1003_const.CMD_READ_SENSOR
        assert data[2] == sensor_id
        assert response is False


async def test_f3_13_request_bytes_buzzer_query(hass, ble_world, fast_timeouts, write_spy):
    """F3-13: `read_buzzer_state()` writes exactly `bytes([seq, 0x50, 0x00])`."""
    device = EM1003Device(hass, MAC)

    result = await _bounded(device.read_buzzer_state())
    assert result is not None

    assert len(write_spy) == 1
    char_uuid, data, response = write_spy[0]
    assert char_uuid == em1003_const.EM1003_WRITE_CHAR_UUID
    assert len(data) == 3
    assert data[1:] == bytes([em1003_const.CMD_BUZZER, 0x00])
    assert response is False


async def test_f3_14_request_bytes_buzzer_set_and_verify(hass, ble_world, fast_timeouts, write_spy):
    """F3-14: `set_buzzer_state(True/False)` writes exactly
    `bytes([seq, 0x50, 0x01, 0x01|0x00])` followed by a fresh-seq
    `bytes([seq2, 0x50, 0x00])` verify query."""
    device = EM1003Device(hass, MAC)

    assert await _bounded(device.set_buzzer_state(True)) is True
    assert len(write_spy) == 2
    (_, set_frame, resp1), (_, query_frame, resp2) = write_spy
    assert set_frame[1:] == bytes([em1003_const.CMD_BUZZER, 0x01, em1003_const.BUZZER_ON])
    assert query_frame[1:] == bytes([em1003_const.CMD_BUZZER, 0x00])
    assert set_frame[0] != query_frame[0], "the verify query must use a fresh sequence id"
    assert resp1 is False
    assert resp2 is False

    write_spy.clear()
    assert await _bounded(device.set_buzzer_state(False)) is True
    assert len(write_spy) == 2
    set_frame_off = write_spy[0][1]
    assert set_frame_off[1:] == bytes([em1003_const.CMD_BUZZER, 0x01, em1003_const.BUZZER_OFF])


# ===========================================================================
# Device-level tests: buzzer query/set parsing (F3-15..F3-18)
# ===========================================================================


async def test_f3_15_buzzer_query_response_parsing(hass, ble_world, fast_timeouts):
    """F3-15: a buzzer query response of `[s,0x50,0x00,0x01]` / `...,0x00]`
    parses to `True` / `False`, and updates `device.buzzer_state`."""
    device = EM1003Device(hass, MAC)

    ble_world.buzzer_state = True
    assert await _bounded(device.read_buzzer_state()) is True
    assert device.buzzer_state is True

    ble_world.buzzer_state = False
    assert await _bounded(device.read_buzzer_state()) is False
    assert device.buzzer_state is False


async def test_f3_16_buzzer_set_ack_parsing(hass, ble_world, fast_timeouts):
    """F3-16: a successful ack `[s,0x05,0x01]` followed by a query confirming
    ON makes `set_buzzer_state(True)` return `True`."""
    device = EM1003Device(hass, MAC)
    ble_world.buzzer_state = False

    assert await _bounded(device.set_buzzer_state(True)) is True
    assert device.buzzer_state is True


async def test_f3_17_buzzer_set_verify_mismatch(hass, ble_world, fast_timeouts):
    """F3-17: the SET is acked, but the verify query reports the device did
    not actually flip — `set_buzzer_state` must report `False`."""
    device = EM1003Device(hass, MAC)
    ble_world.buzzer_state = False
    ble_world.apply_buzzer_set = False  # device "accepts" the command but does not flip

    assert await _bounded(device.set_buzzer_state(True)) is False
    assert device.buzzer_state is False


async def test_f3_18_short_buzzer_query_frame_leaves_state_unchanged(hass, ble_world, fast_timeouts):
    """F3-18: a buzzer query response too short to carry a state byte
    (`[s,0x50,0x00]`) still completes the pending request (it IS a match),
    but must not touch `buzzer_state` — and must not raise."""
    device = EM1003Device(hass, MAC)
    device.buzzer_state = True  # a previously-known state
    client = await _open_client(device, ble_world)
    try:
        ble_world.buzzer_query_silent = True  # suppress the world's normal (correct-length) answer
        seq = device._alloc_seq_id()
        task = asyncio.ensure_future(device._async_request(client, build_buzzer_query(seq), BUZZER_QUERY_KEY))
        await _wait_for_writes(ble_world, 1)

        ble_world.inject(build_frame(seq, em1003_const.CMD_BUZZER, 0x00), client=client)  # no state byte

        frame = await _bounded(task)
        assert frame is not None, "the frame IS a match for the pending request and must complete it"
        assert device.buzzer_state is True, "no state byte to read -> must leave buzzer_state untouched"
    finally:
        await client.disconnect()


# ===========================================================================
# Direct request/response-machinery tests: matching (F3-19..F3-21)
# ===========================================================================


async def test_f3_19_unknown_seq_is_ignored(hass, ble_world, fast_timeouts):
    """F3-19: a notification carrying a seq id that matches no pending
    request must be dropped silently — it must not complete a DIFFERENT
    pending request for the same sensor id, and the request it (falsely)
    resembles must still time out normally."""
    device = EM1003Device(hass, MAC)
    client = await _open_client(device, ble_world)
    try:
        ble_world.silent_sensors.add(em1003_const.SENSOR_ID_01)
        seq1 = device._alloc_seq_id()
        task1 = asyncio.ensure_future(
            device._async_request(
                client, build_sensor_request(seq1, em1003_const.SENSOR_ID_01), em1003_const.SENSOR_ID_01
            )
        )
        await _wait_for_writes(ble_world, 1)

        # A second, unrelated request in flight at the same time, to prove
        # the bogus injection below cannot complete IT either.
        seq2 = device._alloc_seq_id()
        task2 = asyncio.ensure_future(
            device._async_request(
                client, build_sensor_request(seq2, em1003_const.SENSOR_ID_06), em1003_const.SENSOR_ID_06
            )
        )
        await _wait_for_writes(ble_world, 2)

        wrong_seq = (seq1 + 1) % 256
        if wrong_seq in (seq1, seq2):
            wrong_seq = (wrong_seq + 1) % 256
        ble_world.inject(
            build_frame(wrong_seq, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_01, 0x2C, 0x1A),
            client=client,
        )

        result1 = await _bounded(task1)
        result2 = await _bounded(task2)

        assert result1 is None, "a frame with the wrong seq id must not complete the pending request"
        assert result2 is not None, "an unrelated in-flight request must complete normally"
        assert em1003_const.SENSOR_ID_01 not in device.sensor_data
        assert device._pending_requests == {}
    finally:
        await client.disconnect()


async def test_f3_20_mismatched_sensor_id_is_ignored(hass, ble_world, fast_timeouts):
    """F3-20: same seq id, but the sensor-id byte does not match the pending
    request's key — the notification must be dropped, not misapplied."""
    device = EM1003Device(hass, MAC)
    client = await _open_client(device, ble_world)
    try:
        ble_world.silent_sensors.add(em1003_const.SENSOR_ID_06)
        seq = device._alloc_seq_id()
        task = asyncio.ensure_future(
            device._async_request(
                client, build_sensor_request(seq, em1003_const.SENSOR_ID_06), em1003_const.SENSOR_ID_06
            )
        )
        await _wait_for_writes(ble_world, 1)

        ble_world.inject(
            build_frame(seq, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_09, 0x00, 0x00), client=client
        )

        result = await _bounded(task)

        assert result is None, "same seq id but a different sensor-id byte must not match"
        assert device._pending_requests == {}
    finally:
        await client.disconnect()


async def test_f3_21_short_garbage_empty_notifications_never_raise(hass, ble_world, quick_poll_timeouts):
    """F3-21: short/garbage/empty frames must never raise, whether fed
    directly to the (synchronous) notification handler or delivered through
    a live connection at the moment notifications start."""
    device = EM1003Device(hass, MAC)
    garbage_frames = [b"", b"\x01", b"\x01\x02", b"\xff\xff\xff\xff", bytes(range(64))]

    # Direct unit-level check: nothing is pending, so nothing may be touched.
    for frame in garbage_frames:
        device._notification_handler(None, bytearray(frame))
    assert device._pending_requests == {}

    # Full-stack check: fire the same frames unsolicited right when
    # notifications start (ble_world.garbage_notifications), then prove a
    # real poll still completes cleanly and no exception went unhandled.
    ble_world.garbage_notifications = list(garbage_frames)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        result = await _bounded(device.async_poll())
    finally:
        loop.set_exception_handler(previous_handler)

    assert not unhandled, f"garbage notifications caused unhandled exceptions: {unhandled}"
    assert result.ok is True
    assert all(value is not None for value in result.sensors.values())


# ===========================================================================
# Direct request/response-machinery tests: timing & allocation (F3-22, F3-23)
# ===========================================================================


async def test_f3_22_late_response_after_timeout_is_ignored(hass, ble_world, fast_timeouts):
    """F3-22: a response that finally arrives after `BLE_RESPONSE_TIMEOUT`
    has already elapsed must be silently ignored — no "future already done"
    error, `sensor_data` is untouched, the seq id was released on timeout,
    and later requests on the same connection are unaffected."""
    device = EM1003Device(hass, MAC)
    ble_world.silent_sensors.add(em1003_const.SENSOR_ID_01)
    client = await _open_client(device, ble_world)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        seq = device._alloc_seq_id()
        result = await _bounded(
            device._async_request(
                client, build_sensor_request(seq, em1003_const.SENSOR_ID_01), em1003_const.SENSOR_ID_01
            )
        )
        assert result is None, "no response was ever sent, so this must be a real response timeout"
        assert seq not in device._used_seq_ids, "the seq id must be released on timeout"
        assert device._pending_requests == {}

        # The "late" response finally shows up, well after the timeout.
        late_frame = build_frame(seq, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_01, *encode_raw(6700))
        ble_world.inject(late_frame, client=client)
        await asyncio.sleep(0.05)  # let the injected frame's delivery task run

        assert em1003_const.SENSOR_ID_01 not in device.sensor_data

        # Later requests on the very same connection still work normally.
        seq2 = device._alloc_seq_id()
        frame2 = await _bounded(
            device._async_request(
                client, build_sensor_request(seq2, em1003_const.SENSOR_ID_06), em1003_const.SENSOR_ID_06
            )
        )
        assert frame2 is not None
    finally:
        loop.set_exception_handler(previous_handler)
        await client.disconnect()

    assert not unhandled, f"the late response caused unhandled exceptions: {unhandled}"


async def test_f3_23a_seq_id_alloc_has_no_collisions_with_in_flight_ids(hass):
    """F3-23(a): allocating 8 seq ids back to back (simulating 8 concurrent
    pending requests) yields 8 distinct ids, none of which collides with an
    id still considered in-flight."""
    device = EM1003Device(hass, MAC)

    allocated = [device._alloc_seq_id() for _ in range(8)]

    assert len(set(allocated)) == 8, "all 8 allocated ids must be distinct"
    assert device._used_seq_ids == set(allocated)


async def test_f3_23b_seq_id_released_after_response_and_after_timeout(hass, ble_world, fast_timeouts):
    """F3-23(b): a seq id is released back to the free pool both when its
    request gets a real answer, and when it times out with no answer."""
    device = EM1003Device(hass, MAC)
    client = await _open_client(device, ble_world)
    try:
        seq1 = device._alloc_seq_id()
        frame = await _bounded(
            device._async_request(
                client, build_sensor_request(seq1, em1003_const.SENSOR_ID_01), em1003_const.SENSOR_ID_01
            )
        )
        assert frame is not None
        assert seq1 not in device._used_seq_ids, "must be released after a normal response"

        ble_world.silent_sensors.add(em1003_const.SENSOR_ID_06)
        seq2 = device._alloc_seq_id()
        result = await _bounded(
            device._async_request(
                client, build_sensor_request(seq2, em1003_const.SENSOR_ID_06), em1003_const.SENSOR_ID_06
            )
        )
        assert result is None
        assert seq2 not in device._used_seq_ids, "must be released after a response timeout too"
    finally:
        await client.disconnect()


async def test_f3_23c_seq_id_exhaustion_fails_fast_and_clears_on_teardown(hass, ble_world, fast_timeouts):
    """F3-23(c): with every one of the 256 seq ids already considered
    in-flight, a request must fail immediately (no hang), and the session's
    teardown must leave the in-flight set empty afterwards."""
    device = EM1003Device(hass, MAC)
    device._used_seq_ids = set(range(256))  # simulate total exhaustion

    result = await _bounded(device.read_all_sensors(), timeout=2.0)

    assert all(value is None for value in result.values())
    assert set(result.keys()) == set(em1003_const.SENSOR_TYPES.keys())
    assert ble_world.connect_attempts == 1, "a link is still opened before the exhaustion is hit"
    assert ble_world.links_are_clean
    assert device._used_seq_ids == set(), "teardown must clear the in-flight set unconditionally"


# ===========================================================================
# Extra regression guards not explicitly in the matrix (F3-Xn)
# ===========================================================================


def test_f3_x1_match_key_pure_contract():
    """F3-X1: `match_key`'s full contract, directly — the one function every
    F3-19/20/21 scenario ultimately depends on."""
    assert match_key(b"") is None
    assert match_key(b"\x01\x02") is None  # len < 3

    seq = 0x07
    assert match_key(bytes([seq, em1003_const.CMD_BUZZER_SET_RESPONSE, 0x01])) == (seq, BUZZER_SET_KEY)
    assert match_key(bytes([seq, em1003_const.CMD_BUZZER, 0x00, 0x01])) == (seq, 0x00)
    assert match_key(bytes([seq, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_09])) == (
        seq,
        em1003_const.SENSOR_ID_09,
    )
    assert match_key(bytes([seq, 0xEE, 0x00])) is None  # unrecognised command byte


def test_f3_x2_builder_functions_produce_exact_bytes():
    """F3-X2: the three frame builders, directly, independent of any device
    or session — pins the exact byte layout F3-12/13/14 assert on the wire."""
    assert build_sensor_request(0x12, em1003_const.SENSOR_ID_09) == bytes(
        [0x12, em1003_const.CMD_READ_SENSOR, em1003_const.SENSOR_ID_09]
    )
    assert build_buzzer_query(0x34) == bytes([0x34, em1003_const.CMD_BUZZER, 0x00])
    assert build_buzzer_set(0x56, True) == bytes([0x56, em1003_const.CMD_BUZZER, 0x01, em1003_const.BUZZER_ON])
    assert build_buzzer_set(0x56, False) == bytes([0x56, em1003_const.CMD_BUZZER, 0x01, em1003_const.BUZZER_OFF])


def test_f3_x3_formaldehyde_just_above_negative_threshold_is_not_filtered():
    """F3-X3: complements F3-05/F3-06 with the boundary directly above the
    negative threshold (raw 16385 -> +0.001), which must survive the filter."""
    value = parse_sensor_value(em1003_const.SENSOR_ID_0A, encode_raw(16385))
    assert value == pytest.approx(0.001)
    assert apply_negative_filter(em1003_const.SENSOR_ID_0A, value) == pytest.approx(0.001)


def test_f3_x4_negative_filter_never_touches_temperature_at_any_magnitude():
    """F3-X4: 0x01 is exempt from the negative filter regardless of how
    negative the value is, not just at -1 (which F3-08 already checks)."""
    for value in (-0.01, -1, -50.5, -4000):
        assert apply_negative_filter(em1003_const.SENSOR_ID_01, value) == value


def test_f3_x5_parse_sensor_value_ignores_trailing_bytes():
    """F3-X5: `parse_sensor_value` must only ever look at the first two
    bytes — a caller handing it `frame[3:]` (which may be longer than 2 for
    a malformed/extended frame) must not get a different result."""
    payload = b"\x2c\x1a\xff\xff\xff"  # temperature raw plus trailing garbage
    assert parse_sensor_value(em1003_const.SENSOR_ID_01, payload) == pytest.approx(27.0)


async def test_f3_x6_buzzer_query_silence_returns_none_and_preserves_state(hass, ble_world, fast_timeouts):
    """F3-X6: complements F3-18 (malformed-but-received frame) with the
    "never answers at all" case — `read_buzzer_state()` must return `None`
    without erasing the device's last-known `buzzer_state`."""
    device = EM1003Device(hass, MAC)
    device.buzzer_state = True  # a previously-known state
    ble_world.buzzer_query_silent = True

    result = await _bounded(device.read_buzzer_state())

    assert result is None
    assert device.buzzer_state is True, "a query that never answers must not erase the last-known state"


async def test_f3_x7_alloc_falls_back_to_quarantined_id_when_it_is_the_only_option(hass):
    """F3-X7: when every id but one is genuinely in-flight and the only free
    id is quarantined (recently used), allocation must still succeed by
    falling back to it rather than raising — the two-tier fallback in
    section 4.8 ("avoid quarantine first, then fall back to in-flight-only")
    is not the same thing as full 256-id exhaustion (F3-23c)."""
    device = EM1003Device(hass, MAC)
    seq = device._alloc_seq_id()
    device._release_seq_id(seq)  # now free, but quarantined
    assert seq in device._recent_seq_ids

    device._used_seq_ids = set(range(256)) - {seq}  # every other id in flight

    reused = device._alloc_seq_id()

    assert reused == seq, "must fall back to the only free (quarantined) id rather than raising"
    assert seq in device._used_seq_ids


# ---------------------------------------------------------------------------
# Review-round regression guards (see docs/ble_session_refactor.md, "Review round")
# ---------------------------------------------------------------------------


async def test_r_buzzer_set_never_acknowledged(hass, ble_world, fast_timeouts):
    """The SET command is written but never acked (`ack is None`).

    `set_buzzer_state` must report `False` without raising, leave the
    last-known `buzzer_state` alone, close its link and free the lock -- and
    the next operation on the same device must work.
    """
    device = EM1003Device(hass, MAC)
    device.buzzer_state = False  # a previously-known state
    ble_world.buzzer_state = False
    ble_world.buzzer_set_silent = True  # the world never answers the SET write

    assert await _bounded(device.set_buzzer_state(True)) is False

    assert device.buzzer_state is False, "an unacknowledged set must not invent a new state"
    assert ble_world.links_are_clean
    assert device.is_busy is False

    # The device recovers: the very next operation works on a fresh link.
    ble_world.buzzer_set_silent = False
    assert await _bounded(device.set_buzzer_state(True)) is True


async def test_r_buzzer_set_verify_query_never_answered(hass, ble_world, fast_timeouts):
    """The SET is acked, but the verify query never answers (`frame is None`).

    Distinct from F3-17 (the query *does* answer, reporting the wrong state):
    here nothing comes back at all, so `set_buzzer_state` must still report
    `False` without raising and leave no link or lock behind.
    """
    device = EM1003Device(hass, MAC)
    ble_world.buzzer_state = False
    ble_world.buzzer_query_silent = True  # only the verify query goes unanswered

    assert await _bounded(device.set_buzzer_state(True)) is False

    assert ble_world.links_are_clean
    assert device.is_busy is False
    # The world did apply the set; it just never confirmed it.
    assert ble_world.buzzer_state is True

    ble_world.buzzer_query_silent = False
    assert await _bounded(device.read_buzzer_state()) is True
