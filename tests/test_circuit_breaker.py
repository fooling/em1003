"""F2 — Circuit breaker tests (docs/ble_session_refactor.md section 4.4 / 10).

Owned by the F2 test author (see section 11, "File ownership"). This file
implements every case in the F2 matrix (20 cases) plus a handful of
additional regression guards marked ``F2-Xn``.

Two layers are covered, per the plan:

* **Pure unit tests** (F2-01..F2-15) against ``CircuitBreaker`` directly,
  with an injected fake clock (a tiny local ``FakeClock`` helper — no
  changes to ``tests/fakes.py`` were needed). These never touch ``hass`` or
  the fake BLE world.
* **Device-level tests** (F2-16..F2-20) against ``EM1003Device.async_poll`` /
  ``read_buzzer_state`` / ``set_buzzer_state``, using the ``ble_world`` +
  ``fast_timeouts`` fixtures from ``tests/conftest.py``, asserting the
  breaker only ever counts real connect/IO outcomes and that a rejected
  call makes zero BLE calls.

The source under test (``custom_components/em1003/device.py``) is being
rewritten concurrently by another agent to match the plan's contract
(section 4.4 for ``CircuitBreaker``, section 4.5/4.6 for the device-level
breaker integration). These tests are written against **that contract**,
not against the pre-refactor code — see the module docstring of
``tests/test_smoke.py`` for why a plain ``import`` failure elsewhere is
expected and tolerated, but a fixture-name or syntax error in *this* file
is not.

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_circuit_breaker.py -q
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest
from bleak.exc import BleakError

from custom_components.em1003 import const as em1003_const
from custom_components.em1003.device import CircuitBreaker, EM1003Device

from .conftest import MAC

# ---------------------------------------------------------------------------
# Local test helper: an injectable, manually-advanced clock.
#
# Built here (not in tests/fakes.py) per the task brief: "If the scaffold
# lacks a knob you need, build the helper locally inside your test file
# rather than editing tests/fakes.py." CircuitBreaker's `clock` constructor
# argument (section 4.4) just needs a zero-arg callable returning a float.
# ---------------------------------------------------------------------------


class FakeClock:
    """A manually-advanced fake monotonic clock for CircuitBreaker tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


async def _bounded(coro, timeout: float = 5.0):
    """Await `coro` under a hard wall-clock bound.

    A regression that reintroduces an unbounded await on the BLE path (D2)
    must fail this test suite loudly instead of hanging it forever.
    """
    return await asyncio.wait_for(coro, timeout=timeout)


def _make_breaker(clock: FakeClock, **kwargs) -> CircuitBreaker:
    """Construct a CircuitBreaker with the matrix's canonical tuning
    (threshold=3, open_duration=60.0, max_backoff=3600.0) and the given
    fake clock, unless overridden."""
    kwargs.setdefault("failure_threshold", 3)
    kwargs.setdefault("open_duration", 60.0)
    kwargs.setdefault("max_backoff", 3600.0)
    return CircuitBreaker(clock=clock, **kwargs)


# ===========================================================================
# Pure unit tests: CircuitBreaker in isolation (F2-01..F2-15)
# ===========================================================================


def test_f2_01_fresh_breaker_admits():
    """F2-01: a brand-new breaker is CLOSED, admits, and has zero failures."""
    clock = FakeClock()
    cb = _make_breaker(clock)

    assert cb.state == CircuitBreaker.STATE_CLOSED
    assert cb.failure_count == 0

    admitted, reason = cb.can_attempt()
    assert admitted is True
    assert isinstance(reason, str)


def test_f2_02_closed_to_open_at_threshold():
    """F2-02: exactly the 3rd failure (not the 2nd) flips CLOSED -> OPEN."""
    clock = FakeClock()
    cb = _make_breaker(clock)

    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_CLOSED
    admitted, _ = cb.can_attempt()
    assert admitted is True, "must still admit after only 2/3 failures"

    cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN
    assert cb.open_time == clock.now
    assert cb.current_open_duration == 60.0


def test_f2_03_success_resets_before_threshold():
    """F2-03: fail, fail, success -> failure_count is zeroed, stays CLOSED."""
    clock = FakeClock()
    cb = _make_breaker(clock)

    cb.record_failure()
    cb.record_failure()
    cb.record_success()

    assert cb.failure_count == 0
    assert cb.state == CircuitBreaker.STATE_CLOSED


def test_f2_04_rejected_calls_do_not_count():
    """F2-04: hammering can_attempt() while OPEN never mutates the breaker."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN

    failure_count_before = cb.failure_count
    open_time_before = cb.open_time
    duration_before = cb.current_open_duration

    for _ in range(10):
        admitted, reason = cb.can_attempt()
        assert admitted is False
        assert "open" in reason.lower()

    assert cb.failure_count == failure_count_before
    assert cb.open_time == open_time_before
    assert cb.current_open_duration == duration_before
    assert cb.state == CircuitBreaker.STATE_OPEN


def test_f2_05_open_duration_honoured_exactly():
    """F2-05: rejected one tick before the window, admitted exactly at it."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.open_time == 0.0

    clock.now = 59.9
    admitted, _ = cb.can_attempt()
    assert admitted is False
    assert cb.state == CircuitBreaker.STATE_OPEN

    clock.now = 60.0
    admitted, _ = cb.can_attempt()
    assert admitted is True
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN


def test_f2_06_half_open_admits_exactly_one_probe():
    """F2-06: once a probe is in flight, every further call is rejected."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.now = 60.0

    admitted, _ = cb.can_attempt()
    assert admitted is True
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN
    assert cb.probe_in_flight is True

    for _ in range(5):
        admitted, reason = cb.can_attempt()
        assert admitted is False
        assert "flight" in reason.lower() or "half" in reason.lower()
        assert cb.probe_in_flight is True
        assert cb.state == CircuitBreaker.STATE_HALF_OPEN


def test_f2_07_failed_probe_doubles_duration():
    """F2-07: a failed probe re-opens with doubled duration and clears the flag."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.now = 60.0
    cb.can_attempt()  # enters HALF_OPEN, consumes the probe slot
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN

    cb.record_failure()

    assert cb.state == CircuitBreaker.STATE_OPEN
    assert cb.open_time == 60.0
    assert cb.current_open_duration == 120.0
    assert cb.probe_in_flight is False


def test_f2_08_repeated_failed_probes_double_each_time():
    """F2-08: four probe/failure cycles double the duration each time:
    60 -> 120 -> 240 -> 480 -> 960."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.current_open_duration == 60.0

    t = 0.0
    expected_durations = [120.0, 240.0, 480.0, 960.0]
    for expected in expected_durations:
        t += cb.current_open_duration
        clock.now = t
        admitted, _ = cb.can_attempt()
        assert admitted is True
        assert cb.state == CircuitBreaker.STATE_HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitBreaker.STATE_OPEN
        assert cb.current_open_duration == expected
        assert cb.open_time == t


def test_f2_09_backoff_capped():
    """F2-09: doubling never exceeds max_backoff, even started right at the cap."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()

    # Force the breaker into OPEN with a duration already close to the cap,
    # as if several prior doublings had already happened.
    cb.current_open_duration = 3000.0
    cb.open_time = 0.0
    cb.state = CircuitBreaker.STATE_OPEN

    clock.now = 3000.0
    admitted, _ = cb.can_attempt()
    assert admitted is True
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN

    cb.record_failure()
    assert cb.current_open_duration == 3600.0  # min(3000*2, 3600)

    # One more failed probe cycle: must stay pinned at the cap, not blow past it.
    clock.now = cb.open_time + cb.current_open_duration
    admitted, _ = cb.can_attempt()
    assert admitted is True
    cb.record_failure()
    assert cb.current_open_duration == 3600.0


def test_f2_10_successful_probe_closes_and_resets_everything():
    """F2-10: a successful probe fully resets the breaker."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.now = 60.0
    cb.can_attempt()
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN

    cb.record_success()

    assert cb.state == CircuitBreaker.STATE_CLOSED
    assert cb.failure_count == 0
    assert cb.open_time is None
    assert cb.current_open_duration == 60.0
    assert cb.probe_in_flight is False

    admitted, _ = cb.can_attempt()
    assert admitted is True


def test_f2_11_failure_count_not_zeroed_on_entering_half_open():
    """F2-11: dead-code regression guard — HALF_OPEN must NOT reset failure_count."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.now = 60.0

    cb.can_attempt()

    assert cb.state == CircuitBreaker.STATE_HALF_OPEN
    assert cb.failure_count == 3


def test_f2_12_defensive_open_with_none_open_time():
    """F2-12: OPEN with open_time somehow None self-heals to CLOSED, no raise."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    cb.state = CircuitBreaker.STATE_OPEN
    cb.open_time = None

    admitted, reason = cb.can_attempt()

    assert admitted is True
    assert isinstance(reason, str)
    assert cb.state == CircuitBreaker.STATE_CLOSED
    assert cb.failure_count == 0


def test_f2_13_get_state_info_never_raises():
    """F2-13: get_state_info() is safe (and descriptive) in every state,
    including the OPEN/open_time=None defensive corner."""
    clock = FakeClock()
    cb = _make_breaker(clock)

    info = cb.get_state_info()
    assert isinstance(info, str)
    assert "CLOSED" in info

    for _ in range(3):
        cb.record_failure()
    info = cb.get_state_info()
    assert isinstance(info, str)
    assert "OPEN" in info

    # Defensive corner: must not raise even though open_time is None while
    # state claims OPEN (get_state_info, unlike can_attempt(), must not
    # mutate state to self-heal it — it is purely informational).
    cb.open_time = None
    info = cb.get_state_info()
    assert isinstance(info, str)

    # HALF_OPEN.
    cb.state = CircuitBreaker.STATE_OPEN
    cb.open_time = 0.0
    clock.now = 60.0
    cb.can_attempt()
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN
    info = cb.get_state_info()
    assert isinstance(info, str)
    assert "HALF_OPEN" in info


def test_f2_14_reset():
    """F2-14: reset() is equivalent to record_success(), from any state."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    cb.current_open_duration = 999.0
    assert cb.state == CircuitBreaker.STATE_OPEN

    cb.reset()

    assert cb.state == CircuitBreaker.STATE_CLOSED
    assert cb.failure_count == 0
    assert cb.open_time is None
    assert cb.current_open_duration == cb.base_open_duration == 60.0
    assert cb.probe_in_flight is False


def test_f2_15_default_clock_is_time_monotonic_based():
    """F2-15: with no `clock` argument, CircuitBreaker times itself off
    time.monotonic() (not time.time()) — bracket its `open_time` between two
    real time.monotonic() reads taken immediately around the failing calls.

    (See summary for a note on why this brackets real time rather than
    monkeypatching `time.monotonic`: the plan's own constructor signature —
    section 4.4, `clock: Callable[[], float] = time.monotonic` — binds the
    default at class-definition time, so a monkeypatch applied by a test
    that runs after the module has already been imported cannot affect
    it. Bracketing is a deterministic, implementation-detail-free way to
    prove the same thing.)
    """
    before = time.monotonic()
    cb = CircuitBreaker(3, 60.0, 3600.0)
    for _ in range(3):
        cb.record_failure()
    after = time.monotonic()

    assert cb.state == CircuitBreaker.STATE_OPEN
    assert cb.open_time is not None
    assert before <= cb.open_time <= after
    # And very much not a value that would come from `time.time()` (which is
    # a much larger, wall-clock epoch number) — guards against a regression
    # reintroducing `time.time()` under a `clock` label.
    assert cb.open_time < 10**7 or abs(cb.open_time - time.time()) > 1000


# ===========================================================================
# Extra unit-level regression guards not explicitly in the matrix
# ===========================================================================


def test_f2_x1_record_failure_while_open_is_defensive_only():
    """F2-X1: per the "From OPEN" row of the record_failure() table (only
    reachable via a bypassing caller): failure_count increments but
    open_time / current_open_duration are left untouched — a regression
    here would silently restart the open window on every stray failure
    report, defeating the timed backoff."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN
    open_time_before = cb.open_time
    duration_before = cb.current_open_duration

    cb.record_failure()

    assert cb.failure_count == 4
    assert cb.open_time == open_time_before
    assert cb.current_open_duration == duration_before
    assert cb.state == CircuitBreaker.STATE_OPEN


def test_f2_x2_can_attempt_when_closed_is_side_effect_free():
    """F2-X2: can_attempt() while CLOSED must never itself count as a
    success/failure or otherwise perturb failure_count — only
    record_success()/record_failure() may."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    cb.record_failure()

    for _ in range(5):
        admitted, _ = cb.can_attempt()
        assert admitted is True

    assert cb.failure_count == 1
    assert cb.state == CircuitBreaker.STATE_CLOSED


def test_f2_x3_get_state_info_half_open_preserves_failure_count_in_message():
    """F2-X3: get_state_info() while HALF_OPEN must not itself reset
    failure_count (a get_state_info() call is a common thing to do from a
    diagnostics/service handler and must be read-only)."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.now = 60.0
    cb.can_attempt()
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN

    cb.get_state_info()
    cb.get_state_info()

    assert cb.failure_count == 3
    assert cb.state == CircuitBreaker.STATE_HALF_OPEN
    assert cb.probe_in_flight is True


# ===========================================================================
# Device-level tests: CircuitBreaker wired into EM1003Device (F2-16..F2-20)
# ===========================================================================


async def test_f2_16_device_one_failed_poll_counts_exactly_one_failure(hass, ble_world, fast_timeouts):
    """F2-16: a connect failure during async_poll() must record exactly one
    breaker failure, not two (the historic D3 bug double-counted a single
    bad poll)."""
    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC)

    result = await _bounded(device.async_poll())

    assert result.ok is False
    assert device.circuit_breaker.failure_count == 1
    assert device.circuit_breaker.state == CircuitBreaker.STATE_CLOSED
    assert ble_world.links_are_clean


async def test_f2_17_device_three_failed_polls_open_the_breaker(hass, ble_world, fast_timeouts):
    """F2-17: exactly failure_threshold failed polls open the breaker; the
    threshold-th poll still attempts a real connection, the next does not."""
    threshold = em1003_const.CIRCUIT_FAILURE_THRESHOLD
    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC)

    for _ in range(threshold):
        await _bounded(device.async_poll())

    assert device.circuit_breaker.state == CircuitBreaker.STATE_OPEN
    assert ble_world.connect_attempts == threshold

    result = await _bounded(device.async_poll())

    assert result.ok is False
    assert ble_world.connect_attempts == threshold, "a rejected poll must not attempt a connection"


async def test_f2_18_device_breaker_rejected_poll_is_free(hass, ble_world, fast_timeouts):
    """F2-18: a poll rejected by an already-OPEN breaker makes zero BLE
    calls, returns an all-None/ok=False result with all 8 sensor keys still
    present, and leaves the breaker's failure_count untouched."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN
    failure_count_before = cb.failure_count

    device = EM1003Device(hass, MAC, circuit_breaker=cb)

    result = await _bounded(device.async_poll())

    assert result.ok is False
    assert result.buzzer is None
    assert set(result.sensors.keys()) == set(em1003_const.SENSOR_TYPES.keys())
    assert all(v is None for v in result.sensors.values())
    assert ble_world.connect_attempts == 0
    assert cb.failure_count == failure_count_before
    assert cb.state == CircuitBreaker.STATE_OPEN


async def test_f2_19_device_buzzer_only_failure_in_good_poll_does_not_count(hass, ble_world, fast_timeouts):
    """F2-19: all 8 sensors answer but the buzzer query never does — the
    poll is still `ok=True` and the breaker records a SUCCESS, not a
    failure (this was the historic D3 bug: a buzzer-query failure blocked
    sensor reads by poisoning the shared breaker)."""
    ble_world.buzzer_query_silent = True
    device = EM1003Device(hass, MAC)

    result = await _bounded(device.async_poll())

    assert result.ok is True
    assert result.buzzer is None
    assert all(v is not None for v in result.sensors.values())
    assert device.circuit_breaker.failure_count == 0
    assert device.circuit_breaker.state == CircuitBreaker.STATE_CLOSED


async def test_f2_20a_set_buzzer_state_while_open_healthy_world_bypasses_and_closes(
    hass, ble_world, fast_timeouts
):
    """F2-20(a): set_buzzer_state runs even while OPEN (a user-initiated
    action must not be silently swallowed for up to an hour). Against a
    healthy world it succeeds, a connection is genuinely attempted, and the
    breaker is closed and reset by the bypassed success — without ever
    consuming the HALF_OPEN probe slot."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN

    device = EM1003Device(hass, MAC, circuit_breaker=cb)

    result = await _bounded(device.set_buzzer_state(True))

    assert result is True
    assert ble_world.connect_attempts == 1, "the bypassed call must genuinely attempt BLE I/O"
    assert cb.state == CircuitBreaker.STATE_CLOSED
    assert cb.failure_count == 0
    assert cb.probe_in_flight is False
    assert ble_world.links_are_clean


async def test_f2_20b_set_buzzer_state_while_open_failing_world_bypasses_without_recording(
    hass, ble_world, fast_timeouts
):
    """F2-20(b): the same bypass against a failing world returns False and
    records NOTHING — a bypassed failure must never extend the outage, and
    must not consume the HALF_OPEN probe slot either."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN
    open_time_before = cb.open_time
    failure_count_before = cb.failure_count
    duration_before = cb.current_open_duration

    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC, circuit_breaker=cb)

    result = await _bounded(device.set_buzzer_state(True))

    assert result is False
    assert cb.state == CircuitBreaker.STATE_OPEN
    assert cb.failure_count == failure_count_before
    assert cb.open_time == open_time_before
    assert cb.current_open_duration == duration_before
    assert cb.probe_in_flight is False
    assert ble_world.links_are_clean


# ===========================================================================
# Extra device-level regression guards not explicitly in the matrix
# ===========================================================================


async def test_f2_x4_read_buzzer_state_failure_counts_like_a_poll(hass, ble_world, fast_timeouts):
    """F2-X4: the contract table says read_buzzer_state() is "identical to
    async_poll" for breaker purposes — a connect failure there must also
    count as exactly one breaker failure, not zero and not two."""
    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC)

    result = await _bounded(device.read_buzzer_state())

    assert result is None
    assert device.circuit_breaker.failure_count == 1
    assert device.circuit_breaker.state == CircuitBreaker.STATE_CLOSED


async def test_f2_x5_device_name_read_never_touches_the_breaker(hass, ble_world, fast_timeouts):
    """F2-X5: async_read_device_name() must be invisible to the poll
    breaker in both directions — neither "checked" (a forced-OPEN breaker
    must not block it) nor "recorded" (its own failures must not poison
    the poll breaker). This guards the plan's explicit rule that a startup
    name read must never poison the poll breaker."""
    clock = FakeClock()
    cb = _make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitBreaker.STATE_OPEN

    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC, circuit_breaker=cb)

    name = await _bounded(device.async_read_device_name())

    assert name is None
    # Not checked: the call was not rejected by the (OPEN) breaker — it
    # genuinely tried the connection (and failed, since the world raises).
    assert ble_world.connect_attempts == 1
    # Not recorded: the breaker's counters are exactly as they were.
    assert cb.failure_count == 3
    assert cb.state == CircuitBreaker.STATE_OPEN


async def test_f2_x6_async_with_client_default_does_not_touch_breaker(hass, ble_world, fast_timeouts):
    """F2-X6: async_with_client(..., use_breaker=False) (the default) must
    neither be gated by the breaker nor report an outcome to it, even when
    the underlying connection fails and the exception propagates."""
    ble_world.raise_at["connect"] = BleakError
    device = EM1003Device(hass, MAC)

    async def _fn(client):
        return "unreachable"

    with pytest.raises(BleakError):
        await _bounded(device.async_with_client(_fn))

    assert device.circuit_breaker.failure_count == 0
    assert device.circuit_breaker.state == CircuitBreaker.STATE_CLOSED
    assert ble_world.links_are_clean
