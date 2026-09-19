"""F1 -- Session, lock, timeouts, no leaks (docs/ble_session_refactor.md section 10, F1 table).

Owned by the F1 test author (see docs/ble_session_refactor.md section 11). This file is the
regression guard for D1 (no asyncio.Lock -> races/leaked links), D2 (unbounded awaits -> permanent
coordinator freeze) and D4 (extra connections per poll), as expressed by `EM1003Device`'s single
session helper (section 4.7 of the plan).

Every test drives `custom_components.em1003.device.EM1003Device` directly against the `ble_world`
fixture (`tests/fakes.py` via `tests/conftest.py`) -- no config entry / coordinator / HA setup is
needed for this feature, since the contract under test lives entirely inside `device.py`. Every
test that could hang if the defect it targets were reintroduced is wrapped in `asyncio.timeout(...)`
so a regression fails fast instead of freezing the whole suite; the `fast_timeouts` fixture shrinks
every `BLE_*` constant in `custom_components.em1003.const` (see its docstring in conftest.py) so
these bounded waits are themselves short.

Case IDs implemented: F1-01 .. F1-22 (docs/ble_session_refactor.md section 10, "F1" table), plus
locally-added cases F1-X1 .. F1-X6 for scenarios in scope for this feature (D1/D2/D4/BC-21) that the
matrix does not spell out explicitly: the pre-connect "device not found" fast path, the
buzzer-only-session counterpart to F1-21, the `is_busy` property, and `async_with_client` (the debug
services' choke point) sharing the same lock/session machinery.

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_device_session.py -q
"""
from __future__ import annotations

import asyncio
import time

import pytest
from bleak.exc import BleakError

from custom_components.em1003 import const as em1003_const
from custom_components.em1003.device import EM1003Device, EM1003PollResult

from .conftest import MAC

# Generous but bounded wall-clock ceilings for operations that must never hang. These are far
# larger than the (already shrunk, by `fast_timeouts`) internal BLE_* timeouts, so they only ever
# fire on a genuine regression (an unbounded await), never on normal, if slow, CI scheduling.
SAFE_TIMEOUT = 2.0
SAFE_TIMEOUT_LONG = 3.0


# ---------------------------------------------------------------------------
# Local helpers (this feature's own scaffolding -- tests/fakes.py is not touched)
# ---------------------------------------------------------------------------


def _all_sensor_ids() -> list[int]:
    return list(em1003_const.SENSOR_TYPES.keys())


def _assert_ok_invariant(result: EM1003PollResult) -> None:
    """Section 4.3's pinned invariant: ok is True iff at least one sensor value is not None,
    and error is None exactly when ok is True."""
    any_value = any(v is not None for v in result.sensors.values())
    assert result.ok == any_value, f"ok={result.ok} but sensors={result.sensors}"
    if result.ok:
        assert result.error is None
    else:
        assert result.error is not None


def _assert_all_keys_present_in_order(result: EM1003PollResult) -> None:
    assert list(result.sensors.keys()) == _all_sensor_ids()


def _assert_failed_shape(result: EM1003PollResult) -> None:
    """What `EM1003PollResult.failed(...)` must look like (section 4.3)."""
    assert result.ok is False
    assert result.buzzer is None
    assert result.error is not None
    assert all(v is None for v in result.sensors.values())
    _assert_all_keys_present_in_order(result)


def _assert_links_clean(device: EM1003Device, world) -> None:
    """The exact "links: 0 after" definition from section 10's conventions."""
    assert world.links_are_clean, f"{world.open_links} link(s) left open"
    assert device._client is None
    assert not device._lock.locked()


def _widen_session_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float = 5.0) -> None:
    """`fast_timeouts` shrinks `BLE_SESSION_TIMEOUT` to 0.2s -- the scaffold's one shared value
    for every timeout in the suite. But a fault-free 8-sensor + buzzer poll's own inter-request
    delays alone (7 * the shrunk `BLE_INTER_REQUEST_DELAY`) already add up to more than that once
    everything is shrunk uniformly, so a plain healthy poll would spuriously hit the session
    deadline under `fast_timeouts` alone. Call this (after `fast_timeouts`) in any test that needs
    one or more *complete*, healthy poll(s) to actually finish while every other BLE_* constant
    stays fast enough to bound a genuine hang quickly. F1-16 is the one test that wants the tight
    budget on purpose, to exercise the deadline itself, and does not call this.
    """
    monkeypatch.setattr(em1003_const, "BLE_SESSION_TIMEOUT", seconds)


async def _wait_until(predicate, *, timeout: float = 1.0, interval: float = 0.005) -> None:
    """Poll `predicate()` (a zero-arg callable) until truthy, real time, bounded by `timeout`.

    Used to synchronise with a concurrently-running task deterministically (e.g. "wait until a
    link is open" / "wait until a connection attempt has started") instead of a guessed
    `asyncio.sleep`, which would otherwise make the cancellation/concurrency tests timing-fragile.
    """
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


class _DeviceTimeNoTimeTime:
    """A drop-in replacement for the `time` module as seen from `device.py`'s own `import time`
    statement: every attribute delegates to the real `time` module except `.time()`, which raises.
    Used for F1-18's "device.py must not call time.time()" assertion (D8/BC section 3) without
    touching the *global* `time.time`, which real Home Assistant/pytest-asyncio internals do call
    and would make a global patch unsafe.
    """

    def time(self, *_a, **_kw):  # noqa: D401 - intentional trap
        raise AssertionError("device.py must not call time.time() -- use the injected clock")

    def __getattr__(self, name: str):
        return getattr(time, name)


# ---------------------------------------------------------------------------
# F1-01 / F1-02 / F1-04 / F1-21 / F1-22 -- one connection per operation, no extra work
# ---------------------------------------------------------------------------


async def test_f1_01_happy_poll_opens_exactly_one_link(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-01: a healthy poll returns all 8 sensors + buzzer through exactly one link."""
    _widen_session_timeout(monkeypatch)
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    assert isinstance(result, EM1003PollResult)
    assert result.ok is True
    _assert_ok_invariant(result)
    _assert_all_keys_present_in_order(result)
    assert all(v is not None for v in result.sensors.values())
    assert result.buzzer is not None

    assert ble_world.connect_attempts == 1
    assert ble_world.max_concurrent_connects == 1
    assert ble_world.total_links_opened == 1
    _assert_links_clean(device, ble_world)


async def test_f1_02_poll_uses_one_connection_for_sensors_and_buzzer(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-02: the 8 sensor requests and the buzzer query are written, in that order, on the
    *same* connection -- not two separate sessions (D4)."""
    _widen_session_timeout(monkeypatch)
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()
    assert result.ok is True

    sensor_ids = _all_sensor_ids()
    writes = ble_world.writes
    assert len(writes) == len(sensor_ids) + 1, "expected 8 sensor writes + 1 buzzer query write"

    client_ids = {w.client_id for w in writes}
    assert client_ids == {ble_world.clients[0].id}, "all frames must go out on the same client"

    for write, sid in zip(writes[:-1], sensor_ids):
        assert write.data[1] == em1003_const.CMD_READ_SENSOR
        assert write.data[2] == sid

    buzzer_write = writes[-1]
    assert buzzer_write.data[1] == em1003_const.CMD_BUZZER
    assert buzzer_write.data[2] == 0x00

    assert ble_world.connect_attempts == 1
    _assert_links_clean(device, ble_world)


async def test_f1_03_lock_serialises_n_mixed_operations(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-03: five concurrent, different kinds of operations never open more than one link at
    once, and `establish_connection` is never re-entered while another call is in flight."""
    _widen_session_timeout(monkeypatch)
    ble_world.response_delay = 0.01  # each op takes >= ~5ms so overlap would be caught if it happened
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        poll1, buzzer, set_result, sensors, poll2 = await asyncio.gather(
            device.async_poll(),
            device.read_buzzer_state(),
            device.set_buzzer_state(True),
            device.read_all_sensors(),
            device.async_poll(),
        )

    assert ble_world.max_concurrent_connects == 1, "two links were open at once -- D1 regression"
    assert poll1.ok is True and poll2.ok is True
    assert buzzer in (True, False)
    assert isinstance(set_result, bool)
    assert set(sensors.keys()) == set(_all_sensor_ids())
    assert all(v is not None for v in sensors.values())
    _assert_links_clean(device, ble_world)


async def test_f1_04_twenty_concurrent_read_buzzer_state(hass, ble_world, fast_timeouts):
    """F1-04: 20 concurrent `read_buzzer_state()` calls all serialise cleanly and agree."""
    ble_world.buzzer_state = True
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        results = await asyncio.gather(*(device.read_buzzer_state() for _ in range(20)))

    assert results == [True] * 20
    assert ble_world.max_concurrent_connects == 1
    assert ble_world.connect_attempts == 20
    _assert_links_clean(device, ble_world)


async def test_f1_21_read_all_sensors_opens_one_link_and_skips_the_buzzer(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-21: `read_all_sensors()` is the same loop without the buzzer step."""
    _widen_session_timeout(monkeypatch)
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        data = await device.read_all_sensors()

    assert set(data.keys()) == set(_all_sensor_ids())
    assert all(v is not None for v in data.values())
    assert ble_world.connect_attempts == 1
    assert not any(w.data[1] == em1003_const.CMD_BUZZER for w in ble_world.writes)
    _assert_links_clean(device, ble_world)


async def test_f1_22_fifty_sequential_polls_leak_nothing(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-22: 50 sequential polls open and close exactly 50 links each, and leave no pending
    requests or in-flight sequence ids behind."""
    # Keep this test fast: shrink the inter-request delay further than fast_timeouts already
    # does (0.05s * 7 gaps * 50 polls would otherwise dominate the suite's run time).
    monkeypatch.setattr(em1003_const, "BLE_INTER_REQUEST_DELAY", 0.005)
    device = EM1003Device(hass, MAC)

    for _ in range(50):
        async with asyncio.timeout(SAFE_TIMEOUT):
            result = await device.async_poll()
        assert result.ok is True

    assert ble_world.total_links_opened == 50
    assert ble_world.total_links_closed == 50
    assert len(device._pending_requests) == 0
    assert len(device._used_seq_ids) == 0
    _assert_links_clean(device, ble_world)


# ---------------------------------------------------------------------------
# F1-05 .. F1-10 -- every named hang point is bounded and still tears down cleanly
# ---------------------------------------------------------------------------


async def test_f1_05_hang_at_connect(hass, ble_world, fast_timeouts):
    """F1-05: `establish_connection` never returning still yields a failed result quickly."""
    ble_world.hang_at.add("connect")
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    _assert_failed_shape(result)
    assert ble_world.total_links_opened == 0
    _assert_links_clean(device, ble_world)


async def test_f1_06_hang_at_start_notify(hass, ble_world, fast_timeouts):
    """F1-06: a hang at `start_notify` still results in the just-opened link being closed."""
    ble_world.hang_at.add("start_notify")
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    _assert_failed_shape(result)
    assert ble_world.total_links_opened == 1
    assert ble_world.total_links_closed == 1
    _assert_links_clean(device, ble_world)


async def test_f1_07_hang_at_write(hass, ble_world, fast_timeouts):
    """F1-07: a hang on the first write returns with all sensors None and ok False."""
    ble_world.hang_at.add("write")
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    assert result.ok is False
    assert all(v is None for v in result.sensors.values())
    _assert_links_clean(device, ble_world)


async def test_f1_08_no_response_continues_polling_the_rest(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-08: one sensor never answering does not abort the poll -- the rest are still read."""
    _widen_session_timeout(monkeypatch)
    silent_id = em1003_const.SENSOR_ID_01
    ble_world.silent_sensors.add(silent_id)
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        result = await device.async_poll()

    assert result.sensors[silent_id] is None
    other_ids = [sid for sid in _all_sensor_ids() if sid != silent_id]
    assert all(result.sensors[sid] is not None for sid in other_ids)
    assert result.ok is True
    _assert_ok_invariant(result)
    _assert_links_clean(device, ble_world)


async def test_f1_09_hang_at_stop_notify_still_returns_data_and_disconnects(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-09: a hang in teardown's `stop_notify` is swallowed -- data already collected still
    comes back, and the link is still closed."""
    _widen_session_timeout(monkeypatch)
    ble_world.hang_at.add("stop_notify")
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    assert result.ok is True
    assert all(v is not None for v in result.sensors.values())
    assert ble_world.total_links_opened == 1
    assert ble_world.total_links_closed == 1
    assert sum(ble_world.disconnect_calls.values()) >= 1
    _assert_links_clean(device, ble_world)


async def test_f1_10_hang_at_disconnect_drops_reference_and_recovers(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-10: a hang at `disconnect()` still returns the poll's data, drops `_client` regardless,
    and the *next* poll (against a healthy fake again) succeeds.

    Deliberately not asserted here: `ble_world.links_are_clean`. Cancelling a hung
    `client.disconnect()` means bleak itself never learns whether the physical link actually
    closed (this is what real hardware can also do) -- the fake mirrors this by leaving that one
    connection's own bookkeeping marked "still open" forever once its `disconnect()` coroutine was
    cut off before it could flip that state. `device.py` is only required to give up waiting and
    drop its own reference (section 4.7 step 9's "log a warning and simply drop the reference"),
    which is exactly what `device._client is None` here proves.
    """
    _widen_session_timeout(monkeypatch)
    ble_world.hang_once.add("disconnect")
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    assert result.ok is True
    assert device._client is None
    assert not device._lock.locked()

    async with asyncio.timeout(SAFE_TIMEOUT):
        result2 = await device.async_poll()
    assert result2.ok is True
    assert all(v is not None for v in result2.sensors.values())


# ---------------------------------------------------------------------------
# F1-11 -- recovery after every hang point, parametrised
# ---------------------------------------------------------------------------

_F1_11_FAULT_POINTS = ["connect", "start_notify", "write", "no_response", "stop_notify", "disconnect"]


@pytest.mark.parametrize("fault", _F1_11_FAULT_POINTS)
async def test_f1_11_next_operation_works_after_every_hang_point(hass, ble_world, fast_timeouts, monkeypatch, fault):
    """F1-11: whichever of the six points hung/failed, once cleared, the *next* `async_poll()`
    opens exactly one new link, succeeds fully, and leaves the lock free.

    The first poll's own outcome is intentionally not asserted (the matrix only pins the second):
    for "connect"/"start_notify"/"write" the fault prevents the *first* poll from reading any
    sensor at all (ok is False), while for "no_response"/"stop_notify"/"disconnect" it still
    returns full data (ok is True) -- both are correct, and it is the recovery that this case
    guards.
    """
    _widen_session_timeout(monkeypatch)
    device = EM1003Device(hass, MAC)
    silent_id = em1003_const.SENSOR_ID_01

    if fault == "no_response":
        ble_world.silent_sensors.add(silent_id)
    else:
        ble_world.hang_once.add(fault)

    async with asyncio.timeout(SAFE_TIMEOUT):
        await device.async_poll()  # outcome intentionally unchecked, see docstring

    ble_world.silent_sensors.discard(silent_id)
    ble_world.hang_once.discard(fault)

    connects_before = ble_world.connect_attempts
    opened_before = ble_world.total_links_opened
    closed_before = ble_world.total_links_closed

    async with asyncio.timeout(SAFE_TIMEOUT):
        second = await device.async_poll()

    assert second.ok is True
    assert all(v is not None for v in second.sensors.values())
    assert ble_world.connect_attempts == connects_before + 1
    assert ble_world.total_links_opened == opened_before + 1
    assert ble_world.total_links_closed == closed_before + 1
    assert device._client is None
    assert not device._lock.locked()
    if fault != "disconnect":
        # see test_f1_10's docstring for why "disconnect" is the one exception.
        assert ble_world.links_are_clean


# ---------------------------------------------------------------------------
# F1-12 / F1-13 -- errors never propagate out of async_poll
# ---------------------------------------------------------------------------


async def test_f1_12_bleak_error_from_connect(hass, ble_world, fast_timeouts):
    """F1-12: `establish_connection` raising `BleakError` yields a failed result, not a raise."""
    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    _assert_failed_shape(result)
    assert ble_world.total_links_opened == 0
    _assert_links_clean(device, ble_world)


async def test_f1_13_unexpected_exception_mid_session_does_not_propagate(hass, ble_world, fast_timeouts):
    """F1-13: a completely unexpected exception type (not BleakError) raised mid-session must
    not escape `async_poll`, and the link that was opened must still be disconnected."""
    ble_world.raise_at["write"] = RuntimeError
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()  # must not raise RuntimeError

    assert result.ok is False
    assert ble_world.total_links_opened == 1
    assert ble_world.total_links_closed == 1
    _assert_links_clean(device, ble_world)


# ---------------------------------------------------------------------------
# F1-14 / F1-15 -- cancellation safety
# ---------------------------------------------------------------------------


async def test_f1_14_cancellation_mid_session_propagates_and_still_cleans_up(hass, ble_world):
    """F1-14: cancelling the task while it is genuinely suspended mid-session (here: stuck on a
    write) must propagate CancelledError, not swallow it -- and the link must still close.

    Timeouts are *not* shrunk here on purpose: the point is to prove real task cancellation
    interrupts an in-progress session, not that an internal timeout fired first.
    """
    ble_world.hang_at.add("write")
    device = EM1003Device(hass, MAC)

    task = asyncio.ensure_future(device.async_poll())
    await _wait_until(lambda: ble_world.open_links >= 1)  # connected, now stuck on the write
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(SAFE_TIMEOUT):
            await task

    assert ble_world.total_links_opened == ble_world.total_links_closed == 1
    _assert_links_clean(device, ble_world)


async def test_f1_15_cancellation_during_connect(hass, ble_world, monkeypatch):
    """F1-15: cancelling while `establish_connection` itself is in flight leaves no link open,
    and the device recovers for the next call.

    Timeouts are not shrunk via `fast_timeouts` here either (same reasoning as F1-14), except for
    `BLE_RECONNECT_GUARD` and `BLE_INTER_REQUEST_DELAY`: their defaults (2.0s, and 0.3s * 7 gaps)
    are otherwise irrelevant to what this case tests but would make the *recovery* poll below take
    several seconds for no reason.
    """
    monkeypatch.setattr(em1003_const, "BLE_RECONNECT_GUARD", 0.0)
    monkeypatch.setattr(em1003_const, "BLE_INTER_REQUEST_DELAY", 0.01)
    ble_world.hang_at.add("connect")
    device = EM1003Device(hass, MAC)

    task = asyncio.ensure_future(device.async_poll())
    await _wait_until(lambda: ble_world.connect_attempts >= 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(SAFE_TIMEOUT):
            await task

    assert ble_world.total_links_opened == 0
    _assert_links_clean(device, ble_world)

    ble_world.hang_at.discard("connect")
    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()
    assert result.ok is True


# ---------------------------------------------------------------------------
# F1-16 / F1-17 -- deadlines and mid-poll disconnects fail fast, not slowly
# ---------------------------------------------------------------------------


async def test_f1_16_session_deadline_returns_partial_result(hass, ble_world, fast_timeouts):
    """F1-16: when every step is slow enough that the (shrunk) `BLE_SESSION_TIMEOUT` fires mid
    loop, the poll still returns -- promptly -- with whatever partial data it managed, and `ok`
    matches whether anything got through."""
    ble_world.response_delay = 0.1  # >> the shrunk BLE_SESSION_TIMEOUT (0.2s) after a sensor or two
    device = EM1003Device(hass, MAC)

    start = time.monotonic()
    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        result = await device.async_poll()
    elapsed = time.monotonic() - start

    session_timeout = em1003_const.BLE_SESSION_TIMEOUT
    assert elapsed < max(4 * session_timeout, 1.0), "the deadline did not bound the session"
    _assert_all_keys_present_in_order(result)
    assert any(v is None for v in result.sensors.values()), "not all sensors could have finished"
    _assert_ok_invariant(result)
    _assert_links_clean(device, ble_world)


async def test_f1_17_mid_poll_disconnect_fails_pending_futures_fast(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-17: an unexpected disconnect while a request is *in flight* must fail that request's
    future immediately, not leave it to time out on its own `BLE_RESPONSE_TIMEOUT` (the old
    "wait out a full response timeout per sensor" bug).

    The drop-triggering sensor is deliberately silent, so its request is genuinely still sitting in
    `device._pending_requests` when the fake fires the disconnect callback -- the earlier version of
    this test let the fake answer first, so `_fail_pending` always ran against an empty dict and
    deleting its call from `_handle_disconnect` still passed.
    """
    _widen_session_timeout(monkeypatch)
    # A response timeout much larger than a healthy round trip, so "the pending future was failed
    # on disconnect" and "the pending future waited out its own timeout" are far apart in wall
    # clock. Every other bound stays at its `fast_timeouts` value.
    monkeypatch.setattr(em1003_const, "BLE_RESPONSE_TIMEOUT", 1.0)

    sensor_ids = _all_sensor_ids()
    silent_id = sensor_ids[2]  # the 3rd write triggers the drop, and never gets an answer
    ble_world.silent_sensors = {silent_id}
    ble_world.drop_after_writes = 3

    pending_at_call: dict[str, list[int]] = {}
    orig_fail_pending = EM1003Device._fail_pending

    def spy_fail_pending(self, message: str) -> None:
        pending_at_call.setdefault(message, []).append(len(self._pending_requests))
        return orig_fail_pending(self, message)

    monkeypatch.setattr(EM1003Device, "_fail_pending", spy_fail_pending)

    device = EM1003Device(hass, MAC)

    start = time.monotonic()
    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()
    elapsed = time.monotonic() - start

    for sid in sensor_ids[:2]:
        assert result.sensors[sid] is not None, f"sensor {sid:#x} should have answered before the drop"
    for sid in sensor_ids[2:]:
        assert result.sensors[sid] is None, f"sensor {sid:#x} should never have been reachable after the drop"
    _assert_all_keys_present_in_order(result)

    # The mechanism itself: the disconnect callback really did find an in-flight request to fail.
    assert "device disconnected" in pending_at_call, "the disconnect callback never ran _fail_pending"
    assert any(n >= 1 for n in pending_at_call["device disconnected"]), (
        f"the disconnect found nothing pending to fail -- recorded: {pending_at_call}"
    )

    # And the poll came back long before that request's own response timeout would have expired.
    assert elapsed < 0.5 * em1003_const.BLE_RESPONSE_TIMEOUT, (
        f"took {elapsed:.3f}s to notice the disconnect (BLE_RESPONSE_TIMEOUT is "
        f"{em1003_const.BLE_RESPONSE_TIMEOUT}s) -- the pending future was not failed fast"
    )
    _assert_links_clean(device, ble_world)


# ---------------------------------------------------------------------------
# F1-18 -- reconnect guard uses the injected (monotonic) clock, never time.time
# ---------------------------------------------------------------------------


async def test_f1_18_reconnect_guard_uses_injected_monotonic_clock(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-18: the guard between our own disconnect and our next connect is computed from the
    injected clock (sleeping `guard - elapsed`), shrinks to nothing once the clock has advanced
    past the guard, and `device.py` never reads `time.time()`."""
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(delay, *args, **kwargs):
        sleep_calls.append(delay)
        # Cap whatever we actually wait on wall clock so a bug that computes an inflated delay
        # cannot turn this test into a real hang; the *recorded* value is what we assert on.
        return await real_sleep(min(delay, 0.05) if delay else delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", spy_sleep)

    import custom_components.em1003.device as device_module

    monkeypatch.setattr(device_module, "time", _DeviceTimeNoTimeTime())

    fake_now = {"t": 1000.0}
    device = EM1003Device(hass, MAC, clock=lambda: fake_now["t"])

    async with asyncio.timeout(SAFE_TIMEOUT):
        first = await device.async_poll()
    assert first.ok is True

    guard = em1003_const.BLE_RECONNECT_GUARD
    partial_elapsed = guard * 0.3
    expected_remaining = guard - partial_elapsed

    sleep_calls.clear()
    fake_now["t"] += partial_elapsed
    async with asyncio.timeout(SAFE_TIMEOUT):
        second = await device.async_poll()
    assert second.ok is True
    assert any(abs(c - expected_remaining) < 0.01 for c in sleep_calls), (
        f"expected a ~{expected_remaining:.3f}s guard sleep, got {sleep_calls}"
    )

    sleep_calls.clear()
    fake_now["t"] += guard * 10  # now well past the guard
    async with asyncio.timeout(SAFE_TIMEOUT):
        third = await device.async_poll()
    assert third.ok is True
    assert not any(abs(c - expected_remaining) < 0.01 for c in sleep_calls), (
        "a guard sleep happened even though the clock was already past the guard window"
    )


# ---------------------------------------------------------------------------
# F1-19 / F1-20 -- buzzer set and disconnect() interleave correctly with an in-flight poll
# ---------------------------------------------------------------------------


async def test_f1_19_set_buzzer_state_during_inflight_poll_waits_its_turn(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-19: `set_buzzer_state` issued while a poll is in flight waits for the poll's session to
    finish entirely (its first write happens strictly after the poll's last write), and neither
    operation's result is corrupted."""
    _widen_session_timeout(monkeypatch)
    ble_world.response_delay = 0.01
    device = EM1003Device(hass, MAC)

    poll_task = asyncio.ensure_future(device.async_poll())
    await _wait_until(lambda: ble_world.open_links >= 1)
    set_task = asyncio.ensure_future(device.set_buzzer_state(True))

    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        poll_result, set_result = await asyncio.gather(poll_task, set_task)

    assert poll_result.ok is True
    assert all(v is not None for v in poll_result.sensors.values())
    assert set_result is True
    assert ble_world.max_concurrent_connects == 1
    _assert_links_clean(device, ble_world)

    poll_write_count = len(_all_sensor_ids()) + 1  # 8 sensors + 1 buzzer query
    assert len(ble_world.writes) == poll_write_count + 2  # + SET + verify QUERY
    poll_writes = ble_world.writes[:poll_write_count]
    set_write = ble_world.writes[poll_write_count]
    assert set_write.index > poll_writes[-1].index
    assert set_write.data[1] == em1003_const.CMD_BUZZER and set_write.data[2] == 0x01
    assert set_write.data[3] == em1003_const.BUZZER_ON


async def test_f1_20_disconnect_while_poll_in_flight_waits_then_is_a_noop_when_idle(
    hass, ble_world, fast_timeouts, monkeypatch
):
    """F1-20: `disconnect()` called during an in-flight poll only returns once the poll's own
    session has finished (never tears down the poll's link out from under it), and calling it
    again on an idle device never raises."""
    _widen_session_timeout(monkeypatch)
    ble_world.response_delay = 0.01
    device = EM1003Device(hass, MAC)

    poll_task = asyncio.ensure_future(device.async_poll())
    await _wait_until(lambda: ble_world.open_links >= 1)
    disconnect_task = asyncio.ensure_future(device.disconnect())

    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        poll_result, _ = await asyncio.gather(poll_task, disconnect_task)

    assert poll_result.ok is True
    assert all(v is not None for v in poll_result.sensors.values())
    _assert_links_clean(device, ble_world)

    # idle disconnect is a safe no-op
    async with asyncio.timeout(SAFE_TIMEOUT):
        await device.disconnect()
    _assert_links_clean(device, ble_world)


# ---------------------------------------------------------------------------
# F1-X1 .. F1-X6 -- additional cases found while writing this feature's tests
# ---------------------------------------------------------------------------


async def test_f1_x1_device_not_found_fails_fast_without_connecting(hass, ble_world, fast_timeouts):
    """F1-X1 (added): when the HA bluetooth lookup can't find the device at all, `async_poll`
    fails immediately without ever calling `establish_connection` (session step 4) -- this is a
    distinct fast path from every hang/error-at-connect case above, worth its own regression
    guard since a bug here would instead try to connect to a `None` device object."""
    ble_world.device_present = False
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await device.async_poll()

    _assert_failed_shape(result)
    assert ble_world.connect_attempts == 0
    _assert_links_clean(device, ble_world)


async def test_f1_x2_read_buzzer_state_opens_one_link_and_writes_only_the_buzzer_query(hass, ble_world, fast_timeouts):
    """F1-X2 (added): the buzzer-only counterpart to F1-21 -- `read_buzzer_state()` must not
    also run the full 8-sensor loop (it is a distinct, smaller session, not `async_poll` with the
    sensor values thrown away)."""
    ble_world.buzzer_state = True
    device = EM1003Device(hass, MAC)

    async with asyncio.timeout(SAFE_TIMEOUT):
        state = await device.read_buzzer_state()

    assert state is True
    assert ble_world.connect_attempts == 1
    assert len(ble_world.writes) == 1
    assert ble_world.writes[0].data[1] == em1003_const.CMD_BUZZER
    assert ble_world.writes[0].data[2] == 0x00
    assert not any(w.data[1] == em1003_const.CMD_READ_SENSOR for w in ble_world.writes)
    _assert_links_clean(device, ble_world)


async def test_f1_x3_is_busy_reflects_the_lock_during_and_after_an_operation(hass, ble_world, fast_timeouts, monkeypatch):
    """F1-X3 (added): `is_busy` (documented in section 4.5 as `self._lock.locked()`) is False
    before and after an operation and True while one is genuinely in flight -- a direct
    regression guard for the property itself, which no F1-01..22 case inspects."""
    _widen_session_timeout(monkeypatch)
    # Well under the (fast_timeouts-shrunk) BLE_RESPONSE_TIMEOUT of 0.05s -- this only needs to
    # be slow enough that the poll is still in flight when we check `is_busy`, not so slow that
    # it races the response timeout.
    ble_world.response_delay = 0.02
    device = EM1003Device(hass, MAC)
    assert device.is_busy is False

    poll_task = asyncio.ensure_future(device.async_poll())
    await _wait_until(lambda: ble_world.open_links >= 1)
    assert device.is_busy is True
    assert device._lock.locked() is True

    async with asyncio.timeout(SAFE_TIMEOUT):
        result = await poll_task
    assert result.ok is True
    assert device.is_busy is False


async def test_f1_x4_async_with_client_shares_the_session_and_serialises_with_a_poll(
    hass, ble_world, fast_timeouts, monkeypatch
):
    """F1-X4 (added): `async_with_client` (the debug services' one entry point per BC-21) is not
    a second BLE choke point -- it goes through the very same lock and session machinery, so it
    cannot race a concurrent `async_poll()` (D1) and still closes the one link it opened."""
    _widen_session_timeout(monkeypatch)
    device = EM1003Device(hass, MAC)

    async def read_name(client):
        return await client.read_gatt_char(em1003_const.DEVICE_NAME_UUID)

    async with asyncio.timeout(SAFE_TIMEOUT):
        name_bytes = await device.async_with_client(read_name)
    assert name_bytes == ble_world.device_name.encode("utf-8")
    assert ble_world.connect_attempts == 1
    _assert_links_clean(device, ble_world)

    # now prove it serialises with a concurrent poll
    ble_world.response_delay = 0.02
    poll_task = asyncio.ensure_future(device.async_poll())
    await _wait_until(lambda: ble_world.open_links >= 1)

    async with asyncio.timeout(SAFE_TIMEOUT_LONG):
        _name_bytes2, poll_result = await asyncio.gather(device.async_with_client(read_name), poll_task)

    assert poll_result.ok is True
    assert ble_world.max_concurrent_connects == 1
    _assert_links_clean(device, ble_world)


async def test_f1_x5_async_with_client_reraises_func_error_after_disconnecting(hass, ble_world, fast_timeouts):
    """F1-X5 (added): `async_with_client` is the one public coroutine allowed to raise (section
    4.5) -- whatever `func` raised must come out to the caller, but only after the link it opened
    was closed."""
    device = EM1003Device(hass, MAC)

    async def boom(_client):
        raise RuntimeError("debug service exploded")

    with pytest.raises(RuntimeError, match="debug service exploded"):
        async with asyncio.timeout(SAFE_TIMEOUT):
            await device.async_with_client(boom)

    _assert_links_clean(device, ble_world)


async def test_f1_x6_async_with_client_raises_bleak_error_without_connecting_when_device_missing(
    hass, ble_world, fast_timeouts
):
    """F1-X6 (added): `async_with_client` raises `BleakError` (not some other exception, and
    without ever calling `establish_connection`) when the BLE device cannot be found -- mirrors
    F1-X1 for the one public method that is allowed to propagate."""
    ble_world.device_present = False
    device = EM1003Device(hass, MAC)

    async def noop(_client):
        return None

    with pytest.raises(BleakError):
        async with asyncio.timeout(SAFE_TIMEOUT):
            await device.async_with_client(noop)

    assert ble_world.connect_attempts == 0
    _assert_links_clean(device, ble_world)


# ---------------------------------------------------------------------------
# Review-round regression guards (see docs/ble_session_refactor.md, "Review round")
# ---------------------------------------------------------------------------


async def test_r_connect_uses_service_cache_capable_client(hass, ble_world, fast_timeouts, monkeypatch):
    """The client class handed to `establish_connection` must support a service cache.

    bleak-retry-connector only calls `clear_cache()` on a stale-GATT-cache `KeyError` when the
    client is a `BleakClientWithServiceCache`; with a plain `BleakClient` such a device fails
    every poll until HA restarts instead of self-healing.

    The assertion is on the *capability* (`clear_cache`), not on class identity: once the
    bluetooth integration is set up, habluetooth rebinds
    `bleak_retry_connector.BleakClientWithServiceCache` to its own wrapper class, so which object
    that name refers to depends on import order (it rebinds `bleak.BleakClient` too). Neither
    plain-client class has `clear_cache`; both service-cache classes do.
    """
    import custom_components.em1003.device as em1003_device_module

    seen: list[type] = []
    orig_establish = em1003_device_module.establish_connection

    async def spy_establish(client_class, *args, **kwargs):
        seen.append(client_class)
        return await orig_establish(client_class, *args, **kwargs)

    monkeypatch.setattr(em1003_device_module, "establish_connection", spy_establish)

    device = EM1003Device(hass, MAC)
    async with asyncio.timeout(SAFE_TIMEOUT):
        await device.read_buzzer_state()

    assert seen, "establish_connection was never called"
    for client_class in seen:
        assert isinstance(client_class, type)
        assert hasattr(client_class, "clear_cache"), (
            f"device.py passed {client_class!r}; a plain BleakClient has no clear_cache(), so "
            "bleak-retry-connector can never recover from a stale GATT cache"
        )
