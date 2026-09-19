"""BLE device client for the EM1003 air quality sensor.

Everything that talks to the BLE stack lives here, behind a single
``asyncio.Lock``: one link at a time per device, always torn down, and every
``await`` on the BLE path bounded by a timeout from :mod:`.const`.

See ``docs/ble_session_refactor.md`` section 4 for the authoritative contract.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import const
from .const import (
    BUZZER_OFF,
    BUZZER_ON,
    CMD_BUZZER,
    CMD_BUZZER_SET_RESPONSE,
    CMD_READ_SENSOR,
    DEVICE_NAME_UUID,
    EM1003_NOTIFY_CHAR_UUID,
    EM1003_WRITE_CHAR_UUID,
    NEGATIVE_FILTER_SENSOR_IDS,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)

#: Match key used for the buzzer state query response (``[seq, 0x50, 0x00, x]``).
BUZZER_QUERY_KEY = 0x00
#: Match key used for the buzzer set acknowledgement (``[seq, 0x05, state]``).
BUZZER_SET_KEY = 0x01

# Breaker gate modes for :meth:`EM1003Device._async_session`.
_GATE_NONE = "none"  # breaker neither consulted nor updated
_GATE_CHECK = "check"  # normal: rejected calls do not run at all
_GATE_BYPASS = "bypass"  # user action: runs anyway, can only help the breaker


# ---------------------------------------------------------------------------
# Pure helpers (no I/O, no state)
# ---------------------------------------------------------------------------


def parse_sensor_value(sensor_id: int, value_bytes: bytes | bytearray) -> float | int | None:
    """Decode the 2-byte little-endian payload of one sensor response.

    Returns ``None`` for a payload that is too short. Never raises.
    """
    if value_bytes is None or len(value_bytes) < 2:
        return None

    raw = int.from_bytes(bytes(value_bytes[:2]), byteorder="little")

    if sensor_id == 0x01:
        # Temperature: (raw - 4000) / 100
        return (raw - 4000) / 100.0
    if sensor_id == 0x06:
        # Humidity: raw / 100
        return raw / 100.0
    if sensor_id == 0x0A:
        # Formaldehyde: (raw - 16384) / 1000
        return (raw - 16384) / 1000.0
    # Everything else is the raw value, kept as an int.
    return raw


def apply_negative_filter(sensor_id: int, value: float | int | None) -> float | int | None:
    """Drop bogus negative readings for the sensors that cannot go below zero."""
    if value is None:
        return None
    if sensor_id in NEGATIVE_FILTER_SENSOR_IDS and value < 0:
        return None
    return value


def build_sensor_request(seq_id: int, sensor_id: int) -> bytes:
    """Build a sensor read request frame."""
    return bytes([seq_id, CMD_READ_SENSOR, sensor_id])


def build_buzzer_query(seq_id: int) -> bytes:
    """Build a buzzer state query frame."""
    return bytes([seq_id, CMD_BUZZER, 0x00])


def build_buzzer_set(seq_id: int, turn_on: bool) -> bytes:
    """Build a buzzer set frame."""
    return bytes([seq_id, CMD_BUZZER, 0x01, BUZZER_ON if turn_on else BUZZER_OFF])


def _describe_error(err: BaseException) -> str:
    """One short, always-non-empty description of a failure."""
    if isinstance(err, TimeoutError):
        return str(err) or "operation timed out"
    return str(err) or type(err).__name__


def match_key(data: bytes | bytearray) -> tuple[int, int] | None:
    """Return the ``(seq_id, key)`` a notification answers, or ``None``.

    ``None`` means "not a response we understand" — the frame is ignored and
    must never complete an unrelated pending request.
    """
    if data is None or len(data) < 3:
        return None
    seq_id = data[0]
    cmd = data[1]
    if cmd == CMD_BUZZER_SET_RESPONSE:
        return (seq_id, BUZZER_SET_KEY)
    if cmd in (CMD_BUZZER, CMD_READ_SENSOR):
        return (seq_id, data[2])
    return None


# ---------------------------------------------------------------------------
# Small data carriers
# ---------------------------------------------------------------------------


@dataclass
class PendingRequest:
    """One request waiting for its notification."""

    seq_id: int
    sensor_id: int  # match key: sensor id, 0x00 (buzzer query) or 0x01 (buzzer set)
    future: asyncio.Future
    timestamp: float  # monotonic, from the owning device's clock


@dataclass(frozen=True)
class EM1003PollResult:
    """Result of one full poll (all sensors plus the buzzer state)."""

    sensors: dict[int, float | int | None]
    buzzer: bool | None
    ok: bool
    error: str | None = None

    @classmethod
    def failed(cls, error: str) -> "EM1003PollResult":
        """Build an all-``None`` failure result carrying every sensor key."""
        return cls(
            sensors={sensor_id: None for sensor_id in SENSOR_TYPES},
            buzzer=None,
            ok=False,
            error=error,
        )


@dataclass
class _SessionResult:
    """Internal outcome of one :meth:`EM1003Device._async_session` call."""

    value: Any = None
    admitted: bool = True
    rejected_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Keeps a dead device from being hammered once it stops answering.

    CLOSED   - normal operation.
    OPEN     - blocked until ``current_open_duration`` has elapsed.
    HALF_OPEN- exactly one probe is admitted; its outcome closes or re-opens.

    Callers must report exactly one outcome per admitted call (either
    :meth:`record_success` or :meth:`record_failure`). Rejected calls report
    nothing, so they can never extend an outage.
    """

    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int | None = None,
        open_duration: float | None = None,
        max_backoff: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialise the breaker; ``clock`` defaults to ``time.monotonic``."""
        self.failure_threshold = (
            const.CIRCUIT_FAILURE_THRESHOLD if failure_threshold is None else failure_threshold
        )
        self.base_open_duration = (
            const.CIRCUIT_OPEN_DURATION if open_duration is None else open_duration
        )
        self.max_backoff = (
            const.CIRCUIT_MAX_OPEN_DURATION if max_backoff is None else max_backoff
        )
        self._clock = clock

        self.state = self.STATE_CLOSED
        self.failure_count = 0
        self.open_time: float | None = None
        self.current_open_duration = self.base_open_duration
        self.probe_in_flight = False

    # -- clock ----------------------------------------------------------

    def _now(self) -> float:
        """Read the (late-bound) clock, monotonic by default."""
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    # -- admission ------------------------------------------------------

    def can_attempt(self) -> tuple[bool, str]:
        """Decide whether a call may run. Has side effects on state."""
        if self.state == self.STATE_CLOSED:
            return True, "closed"

        if self.state == self.STATE_OPEN:
            if self.open_time is None:
                # Defensive: OPEN without a timestamp cannot expire, so reset.
                self.state = self.STATE_CLOSED
                self.failure_count = 0
                self.probe_in_flight = False
                return True, "circuit reset"

            elapsed = self._now() - self.open_time
            if elapsed < self.current_open_duration:
                remaining = self.current_open_duration - elapsed
                return False, f"circuit open ({remaining:.0f}s remaining)"

            self.state = self.STATE_HALF_OPEN
            self.probe_in_flight = True
            _LOGGER.info(
                "[CIRCUIT] Half-open after %.0fs, admitting one probe", elapsed
            )
            return True, "half-open probe"

        # HALF_OPEN
        if self.probe_in_flight:
            return False, "half-open probe in flight"
        self.probe_in_flight = True
        return True, "half-open probe"

    # -- outcomes -------------------------------------------------------

    def record_success(self) -> None:
        """An admitted call succeeded: close the circuit and reset the backoff."""
        was_closed = self.state == self.STATE_CLOSED and self.failure_count == 0
        self.state = self.STATE_CLOSED
        self.failure_count = 0
        self.open_time = None
        self.current_open_duration = self.base_open_duration
        self.probe_in_flight = False
        if not was_closed:
            _LOGGER.info("[CIRCUIT] Recovered, circuit CLOSED")

    def record_failure(self) -> None:
        """An admitted call failed."""
        self.failure_count += 1

        if self.state == self.STATE_HALF_OPEN:
            self.state = self.STATE_OPEN
            self.open_time = self._now()
            self.current_open_duration = min(
                self.current_open_duration * 2, self.max_backoff
            )
            self.probe_in_flight = False
            _LOGGER.warning(
                "[CIRCUIT] Probe failed, circuit OPEN for %.0fs",
                self.current_open_duration,
            )
            return

        if self.state == self.STATE_OPEN:
            # Defensive: only reachable if a caller bypassed the gate.
            return

        if self.failure_count >= self.failure_threshold:
            self.state = self.STATE_OPEN
            self.open_time = self._now()
            self.current_open_duration = self.base_open_duration
            _LOGGER.warning(
                "[CIRCUIT] %d consecutive failures, circuit OPEN for %.0fs",
                self.failure_count,
                self.current_open_duration,
            )

    def reset(self) -> None:
        """Force the breaker back to its pristine CLOSED state."""
        self.state = self.STATE_CLOSED
        self.failure_count = 0
        self.open_time = None
        self.current_open_duration = self.base_open_duration
        self.probe_in_flight = False

    def get_state_info(self) -> str:
        """Human-readable state. Never raises, in any state."""
        if self.state == self.STATE_CLOSED:
            return f"CLOSED (failures: {self.failure_count})"
        if self.state == self.STATE_HALF_OPEN:
            return f"HALF_OPEN (probing, failures: {self.failure_count})"
        if self.open_time is None:
            return f"OPEN (no timestamp, failures: {self.failure_count})"
        remaining = max(0.0, self.current_open_duration - (self._now() - self.open_time))
        return f"OPEN (blocking for {remaining:.0f}s, failures: {self.failure_count})"


# ---------------------------------------------------------------------------
# The device
# ---------------------------------------------------------------------------


class EM1003Device:
    """One EM1003 peripheral, with exclusive access to its BLE link."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        device_name: str | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialise the device. Opens nothing."""
        self.hass = hass
        self.mac_address = mac_address
        self.device_name = device_name or mac_address

        self._clock_func = clock
        self._lock = asyncio.Lock()
        self._client: BleakClient | None = None
        self._last_disconnect_time: float | None = None

        self.sensor_data: dict[int, float | int | None] = {}
        self.buzzer_state: bool | None = None

        # (seq_id, match key) -> PendingRequest, in flight only.
        self._pending_requests: dict[tuple[int, int], PendingRequest] = {}
        self._used_seq_ids: set[int] = set()
        self._recent_seq_ids: deque[int] = deque(maxlen=const.SEQ_ID_QUARANTINE)

        self._circuit_breaker = circuit_breaker or CircuitBreaker(clock=clock)

    # -- introspection ---------------------------------------------------

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """The breaker guarding this device."""
        return self._circuit_breaker

    @property
    def is_busy(self) -> bool:
        """``True`` while a BLE session holds the lock."""
        return self._lock.locked()

    def _clock(self) -> float:
        """Monotonic clock, injectable for tests."""
        if self._clock_func is not None:
            return self._clock_func()
        return time.monotonic()

    def _device_id(self) -> str:
        """Identifier used in log lines."""
        if self.device_name and self.device_name != self.mac_address:
            return f"{self.device_name} ({self.mac_address})"
        return self.mac_address

    # -- sequence ids ----------------------------------------------------

    def _alloc_seq_id(self) -> int:
        """Reserve a sequence id that is neither in flight nor recently used."""
        all_ids = set(range(256))
        candidates = all_ids - self._used_seq_ids - set(self._recent_seq_ids)
        if not candidates:
            candidates = all_ids - self._used_seq_ids
        if not candidates:
            raise BleakError("no free sequence id")
        seq_id = random.choice(sorted(candidates))
        self._used_seq_ids.add(seq_id)
        return seq_id

    def _release_seq_id(self, seq_id: int) -> None:
        """Return a sequence id to the pool, quarantining it for a while."""
        self._used_seq_ids.discard(seq_id)
        self._recent_seq_ids.append(seq_id)

    # -- pending requests -------------------------------------------------

    def _discard_pending(self, key: tuple[int, int]) -> None:
        """Drop one pending request and free its sequence id."""
        request = self._pending_requests.pop(key, None)
        self._release_seq_id(key[0])
        if request is not None and not request.future.done():
            request.future.cancel()

    def _fail_pending(self, message: str) -> None:
        """Fail every in-flight request at once (disconnect / session end)."""
        if not self._pending_requests:
            return
        requests = list(self._pending_requests.values())
        self._pending_requests.clear()
        for request in requests:
            self._release_seq_id(request.seq_id)
            future = request.future
            if future.done():
                continue
            future.set_exception(BleakError(message))
            # Mark the exception as retrieved: nobody may be waiting any more.
            if not future.cancelled():
                future.exception()

    # -- callbacks --------------------------------------------------------

    def _notification_handler(self, sender: Any, data: bytearray) -> None:
        """Route one notification to its waiter. Sync, never raises."""
        try:
            frame = bytes(data)
            key = match_key(frame)
            if key is None:
                _LOGGER.debug("[RX] Ignoring frame: %s", frame.hex())
                return

            request = self._pending_requests.get(key)
            if request is None:
                _LOGGER.debug(
                    "[RX] Unexpected response (seq=0x%02x, key=0x%02x)", key[0], key[1]
                )
                return

            if frame[1] == CMD_BUZZER and len(frame) >= 4:
                self.buzzer_state = frame[3] == BUZZER_ON

            self._pending_requests.pop(key, None)
            self._release_seq_id(key[0])
            if not request.future.done():
                request.future.set_result(frame)
            _LOGGER.debug("[RX] %s", frame.hex(" "))
        except Exception as err:  # noqa: BLE001 - a callback must never propagate
            _LOGGER.error("[RX] Error handling notification: %s", err)

    def _handle_disconnect(self, client: Any) -> None:
        """Bleak disconnect callback: fail everything in flight immediately."""
        if client is not self._client:
            # A link we no longer own; its waiters are already gone.
            return
        self._last_disconnect_time = self._clock()
        _LOGGER.debug("[CONN] %s dropped the link", self._device_id())
        self._fail_pending("device disconnected")

    # -- session machinery -------------------------------------------------

    async def _async_wait_reconnect_guard(self) -> None:
        """Give the controller a moment between our disconnect and next connect."""
        if self._last_disconnect_time is None:
            return
        remaining = const.BLE_RECONNECT_GUARD - (self._clock() - self._last_disconnect_time)
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _async_connect(self) -> BleakClient:
        """Look the device up in HA and open one link to it, bounded."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )
        if not ble_device:
            raise BleakError(f"Device not found: {self.mac_address}")

        try:
            async with asyncio.timeout(const.BLE_CONNECT_TIMEOUT):
                # BleakClientWithServiceCache (a BleakClient subclass) is the
                # class HA recommends: it is the only one bleak-retry-connector
                # will call clear_cache() on when a stale GATT cache makes the
                # connect raise KeyError, so a device whose GATT table changed
                # self-heals instead of failing every poll until HA restarts.
                return await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.mac_address,
                    disconnected_callback=self._handle_disconnect,
                    max_attempts=1,
                    timeout=const.BLE_CONNECT_TIMEOUT,
                )
        except TimeoutError as err:
            raise BleakError(f"Connection to {self.mac_address} timed out") from err

    async def _async_teardown(self, client: Any, notify_started: bool) -> None:
        """Close one link. Swallows everything, bounded at every step."""
        try:
            self._fail_pending("session ended")
            # Nothing can be in flight outside a session.
            self._used_seq_ids.clear()

            if notify_started:
                try:
                    async with asyncio.timeout(const.BLE_STOP_NOTIFY_TIMEOUT):
                        await client.stop_notify(EM1003_NOTIFY_CHAR_UUID)
                except TimeoutError:
                    _LOGGER.debug("[CONN] stop_notify timed out for %s", self.mac_address)
                except Exception as err:  # noqa: BLE001 - best effort
                    _LOGGER.debug("[CONN] stop_notify failed for %s: %s", self.mac_address, err)

            try:
                async with asyncio.timeout(const.BLE_DISCONNECT_TIMEOUT):
                    await client.disconnect()
            except TimeoutError:
                _LOGGER.warning(
                    "[CONN] Disconnect from %s timed out, dropping the link reference",
                    self.mac_address,
                )
            except Exception as err:  # noqa: BLE001 - best effort
                _LOGGER.debug("[CONN] Disconnect from %s failed: %s", self.mac_address, err)
        finally:
            # Identity-guarded so a teardown that outlived its session (only
            # possible if we were cancelled twice) cannot clear a newer link.
            if self._client is client:
                self._client = None
            self._last_disconnect_time = self._clock()

    async def _async_close(self, client: Any, notify_started: bool) -> None:
        """Run the teardown so that it finishes even if we are cancelled."""
        task = asyncio.ensure_future(self._async_teardown(client, notify_started))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.wait({task})
            raise
        except Exception as err:  # noqa: BLE001 - teardown swallows its own errors
            _LOGGER.debug("[CONN] Teardown error for %s: %s", self.mac_address, err)

    async def _async_session(
        self,
        body: Callable[[Any], Awaitable[Any]],
        *,
        notify: bool = False,
        gate: str = _GATE_NONE,
        timeout: float | None = None,
        raise_errors: bool = False,
        outcome_fn: Callable[[], bool] | None = None,
    ) -> _SessionResult:
        """Run ``body(client)`` as the only BLE activity on this device.

        Serialised by ``self._lock``; the link created here is always closed
        here, even on cancellation or a blown deadline.
        """
        failure: Exception | None = None
        cancelled = False

        async with self._lock:
            admitted = True
            reason = "closed"
            if gate != _GATE_NONE:
                admitted, reason = self._circuit_breaker.can_attempt()
                if not admitted and gate == _GATE_CHECK:
                    _LOGGER.debug("[CIRCUIT] %s blocked: %s", self._device_id(), reason)
                    return _SessionResult(admitted=False, rejected_reason=reason)

            value: Any = None
            client: Any = None
            notify_started = False

            try:
                try:
                    try:
                        async with asyncio.timeout(
                            const.BLE_SESSION_TIMEOUT if timeout is None else timeout
                        ):
                            await self._async_wait_reconnect_guard()
                            client = await self._async_connect()
                            self._client = client

                            if notify:
                                try:
                                    async with asyncio.timeout(const.BLE_START_NOTIFY_TIMEOUT):
                                        await client.start_notify(
                                            EM1003_NOTIFY_CHAR_UUID, self._notification_handler
                                        )
                                except TimeoutError as err:
                                    raise BleakError(
                                        f"start_notify on {self.mac_address} timed out"
                                    ) from err
                                notify_started = True

                            value = await body(client)
                    finally:
                        # The link this session created is always closed here,
                        # whatever happened above - cancellation included.
                        if client is not None:
                            await self._async_close(client, notify_started)
                        else:
                            self._client = None
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except Exception as err:  # noqa: BLE001 - reported, not propagated
                    failure = err
                    _LOGGER.error(
                        "[CONN] %s: BLE session failed: %s",
                        self._device_id(),
                        _describe_error(err),
                    )
            finally:
                # Exactly one outcome per admitted call, cancellation included,
                # so a HALF_OPEN probe slot can never stay stuck.
                if outcome_fn is not None:
                    success = bool(outcome_fn())
                else:
                    success = failure is None and not cancelled

                if gate == _GATE_CHECK or (gate == _GATE_BYPASS and admitted):
                    if success:
                        self._circuit_breaker.record_success()
                    else:
                        self._circuit_breaker.record_failure()
                elif gate == _GATE_BYPASS and success:
                    # A bypassed call can only help the breaker, never extend
                    # an outage.
                    self._circuit_breaker.record_success()

            result = _SessionResult(
                value=value,
                admitted=admitted,
                rejected_reason=None if admitted else reason,
                error=None if failure is None else _describe_error(failure),
            )

        if failure is not None and raise_errors:
            raise failure
        return result

    # -- request / response -------------------------------------------------

    async def _async_request(
        self,
        client: Any,
        payload: bytes,
        key: int,
        *,
        timeout: float | None = None,
    ) -> bytes | None:
        """Write one frame and wait for its answer.

        Returns the raw response frame, or ``None`` when the device did not
        answer in time. Raises :class:`BleakError` when the write fails or the
        link drops while waiting.
        """
        seq_id = payload[0]
        request_key = (seq_id, key)
        loop = asyncio.get_running_loop()
        request = PendingRequest(
            seq_id=seq_id,
            sensor_id=key,
            future=loop.create_future(),
            timestamp=self._clock(),
        )
        self._pending_requests[request_key] = request

        _LOGGER.debug("[TX] %s", payload.hex(" "))

        try:
            async with asyncio.timeout(const.BLE_WRITE_TIMEOUT):
                await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, payload, response=False)
        except BaseException as err:
            self._discard_pending(request_key)
            if isinstance(err, BleakError):
                raise
            if isinstance(err, TimeoutError):
                raise BleakError(f"write timed out for {self.mac_address}") from err
            if isinstance(err, Exception):
                raise BleakError(f"write failed for {self.mac_address}: {err}") from err
            raise

        try:
            return await asyncio.wait_for(
                request.future,
                const.BLE_RESPONSE_TIMEOUT if timeout is None else timeout,
            )
        except TimeoutError:
            self._discard_pending(request_key)
            _LOGGER.debug(
                "[REQ] No response for key 0x%02x (seq=0x%02x)", key, seq_id
            )
            return None
        except BaseException:
            self._discard_pending(request_key)
            raise

    # -- operation bodies ---------------------------------------------------

    async def _async_read_sensors(
        self, client: Any, sensors: dict[int, float | int | None]
    ) -> None:
        """Query every sensor once on an established link, in place."""
        sensor_ids = list(SENSOR_TYPES)
        last_index = len(sensor_ids) - 1

        for index, sensor_id in enumerate(sensor_ids):
            if not client.is_connected:
                _LOGGER.warning(
                    "[REQ] Link to %s lost, %d sensors not read",
                    self.mac_address,
                    len(sensor_ids) - index,
                )
                return

            try:
                seq_id = self._alloc_seq_id()
                frame = await self._async_request(
                    client, build_sensor_request(seq_id, sensor_id), sensor_id
                )
            except BleakError as err:
                _LOGGER.error(
                    "[REQ] Sensor 0x%02x read failed on %s: %s",
                    sensor_id,
                    self.mac_address,
                    err,
                )
                return

            if frame is not None:
                value = apply_negative_filter(
                    sensor_id, parse_sensor_value(sensor_id, frame[3:])
                )
                if value is None:
                    _LOGGER.debug(
                        "[FILTER] Dropped reading for sensor 0x%02x", sensor_id
                    )
                else:
                    self.sensor_data[sensor_id] = value
                sensors[sensor_id] = value

            if index < last_index:
                await asyncio.sleep(const.BLE_INTER_REQUEST_DELAY)

    async def _async_query_buzzer(self, client: Any) -> bool | None:
        """Best-effort buzzer state query on an established link."""
        try:
            seq_id = self._alloc_seq_id()
            frame = await self._async_request(client, build_buzzer_query(seq_id), BUZZER_QUERY_KEY)
        except BleakError as err:
            _LOGGER.debug("[BUZZER] State query failed on %s: %s", self.mac_address, err)
            return None
        if frame is None:
            _LOGGER.debug("[BUZZER] No state answer from %s", self.mac_address)
            return None
        return self.buzzer_state

    # -- public API ---------------------------------------------------------

    async def async_poll(self) -> EM1003PollResult:
        """Read every sensor and the buzzer state over one single link."""
        sensors: dict[int, float | int | None] = {sensor_id: None for sensor_id in SENSOR_TYPES}
        buzzer: bool | None = None

        def _any_sensor() -> bool:
            return any(value is not None for value in sensors.values())

        async def _body(client: Any) -> None:
            nonlocal buzzer
            await self._async_read_sensors(client, sensors)
            buzzer = await self._async_query_buzzer(client)

        result = await self._async_session(
            _body, notify=True, gate=_GATE_CHECK, outcome_fn=_any_sensor
        )

        if not result.admitted:
            return EM1003PollResult.failed(f"Circuit open for {self.mac_address}: {result.rejected_reason}")
        if _any_sensor():
            return EM1003PollResult(sensors=sensors, buzzer=buzzer, ok=True)
        return EM1003PollResult.failed(
            result.error or f"No sensor data from {self.mac_address}"
        )

    async def read_all_sensors(self) -> dict[int, float | int | None]:
        """Read every sensor over one link. Never raises."""
        sensors: dict[int, float | int | None] = {sensor_id: None for sensor_id in SENSOR_TYPES}

        def _any_sensor() -> bool:
            return any(value is not None for value in sensors.values())

        async def _body(client: Any) -> None:
            await self._async_read_sensors(client, sensors)

        await self._async_session(_body, notify=True, gate=_GATE_CHECK, outcome_fn=_any_sensor)
        return sensors

    async def read_buzzer_state(self) -> bool | None:
        """Query the buzzer state over its own link. Never raises."""
        answered = False
        state: bool | None = None

        async def _body(client: Any) -> None:
            nonlocal answered, state
            seq_id = self._alloc_seq_id()
            frame = await self._async_request(client, build_buzzer_query(seq_id), BUZZER_QUERY_KEY)
            if frame is not None:
                answered = True
                state = self.buzzer_state

        await self._async_session(
            _body, notify=True, gate=_GATE_CHECK, outcome_fn=lambda: answered
        )
        return state if answered else None

    async def set_buzzer_state(self, turn_on: bool) -> bool:
        """Turn the buzzer on or off and verify the new state. Never raises.

        A user action, so it runs even while the breaker is open; a bypassed
        attempt can only close the breaker, never extend an outage.
        """
        verified = False

        async def _body(client: Any) -> None:
            nonlocal verified
            seq_id = self._alloc_seq_id()
            ack = await self._async_request(
                client, build_buzzer_set(seq_id, turn_on), BUZZER_SET_KEY
            )
            if ack is None:
                _LOGGER.error(
                    "[BUZZER] %s did not acknowledge the set command", self.mac_address
                )
                return

            await asyncio.sleep(const.BLE_BUZZER_SETTLE_DELAY)

            query_seq_id = self._alloc_seq_id()
            frame = await self._async_request(
                client, build_buzzer_query(query_seq_id), BUZZER_QUERY_KEY
            )
            if frame is None:
                _LOGGER.error(
                    "[BUZZER] %s did not confirm the new state", self.mac_address
                )
                return

            verified = self.buzzer_state == turn_on
            if not verified:
                _LOGGER.error(
                    "[BUZZER] %s reports %s after being told %s",
                    self.mac_address,
                    self.buzzer_state,
                    turn_on,
                )

        await self._async_session(
            _body, notify=True, gate=_GATE_BYPASS, outcome_fn=lambda: verified
        )
        return verified

    async def async_read_device_name(self) -> str | None:
        """Read the GATT device name. Never raises, never touches the breaker."""
        name: str | None = None

        async def _body(client: Any) -> None:
            nonlocal name
            async with asyncio.timeout(const.BLE_READ_CHAR_TIMEOUT):
                raw = await client.read_gatt_char(DEVICE_NAME_UUID)
            text = bytes(raw).decode("utf-8", errors="replace").replace("\x00", "").strip()
            name = text or None

        await self._async_session(_body, notify=False, gate=_GATE_NONE)
        return name

    async def async_with_client(
        self,
        func: Callable[[Any], Awaitable[Any]],
        *,
        notify: bool = False,
        timeout: float | None = None,
        use_breaker: bool = False,
    ) -> Any:
        """Run ``func(client)`` on a link of this device, then close it.

        Every BLE connection of the integration goes through here, so the
        debug services share this device's lock and its single patch point.
        Raises :class:`BleakError` when the link cannot be made, and re-raises
        whatever ``func`` raised once the link has been closed.
        """
        result = await self._async_session(
            func,
            notify=notify,
            timeout=timeout,
            gate=_GATE_CHECK if use_breaker else _GATE_NONE,
            raise_errors=True,
        )
        if not result.admitted:
            raise BleakError(
                f"Circuit open for {self.mac_address}: {result.rejected_reason}"
            )
        return result.value

    async def disconnect(self) -> None:
        """Close any lingering link. Never raises.

        Waits for an in-flight session, so it must never be called from inside
        one.
        """
        async with self._lock:
            client = self._client
            if client is None:
                self._client = None
                _LOGGER.debug("[CONN] %s already disconnected", self.mac_address)
                return
            await self._async_close(client, True)
