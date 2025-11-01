"""BLE device client for EM1003 air quality sensor."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    CMD_READ_SENSOR,
    CMD_BUZZER,
    BUZZER_ON,
    BUZZER_OFF,
    EM1003_NOTIFY_CHAR_UUID,
    EM1003_WRITE_CHAR_UUID,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingRequest:
    """Represents a pending sensor read request."""

    seq_id: int
    sensor_id: int
    future: asyncio.Future
    timestamp: float


class CircuitBreaker:
    """Circuit breaker pattern to prevent request pile-up during connection failures.

    States:
    - CLOSED: Normal operation, requests allowed
    - OPEN: Too many failures, requests blocked for 60 seconds
    - HALF_OPEN: After timeout, allow one test request
    """

    def __init__(self, failure_threshold: int = 3, open_duration: float = 60.0, max_backoff: float = 3600.0):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            open_duration: Base seconds to wait before entering half-open state
            max_backoff: Maximum backoff duration in seconds (default: 1 hour)
        """
        self.state = "CLOSED"
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.open_time: float | None = None
        self.base_open_duration = open_duration
        self.max_backoff = max_backoff

    def record_success(self) -> None:
        """Record successful operation - reset to CLOSED state."""
        self.failure_count = 0
        self.state = "CLOSED"
        self.open_time = None
        _LOGGER.debug("[CIRCUIT] ✓ Success recorded, circuit CLOSED")

    def record_failure(self) -> None:
        """Record failed operation - may open circuit."""
        self.failure_count += 1
        _LOGGER.debug(
            "[CIRCUIT] ✗ Failure recorded (%d/%d)",
            self.failure_count,
            self.failure_threshold
        )

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            self.open_time = time.time()

            # Calculate exponential backoff: base_duration * 2^(failures - threshold)
            # Example: 3 failures → 60s, 4 → 120s, 5 → 240s, 6 → 480s, etc.
            backoff_multiplier = 2 ** (self.failure_count - self.failure_threshold)
            open_duration = min(self.base_open_duration * backoff_multiplier, self.max_backoff)

            _LOGGER.warning(
                "[CIRCUIT] Circuit OPEN due to %d consecutive failures. "
                "Blocking requests for %.0f seconds (exponential backoff)",
                self.failure_count,
                open_duration
            )

    def can_attempt(self) -> tuple[bool, str]:
        """Check if request can proceed.

        Returns:
            Tuple of (can_proceed, reason)
        """
        if self.state == "CLOSED":
            return True, "Circuit closed"

        elif self.state == "OPEN":
            if self.open_time is None:
                # Shouldn't happen, but handle gracefully
                self.state = "CLOSED"
                return True, "Circuit reset"

            # Calculate current open duration with exponential backoff
            backoff_multiplier = 2 ** (self.failure_count - self.failure_threshold)
            open_duration = min(self.base_open_duration * backoff_multiplier, self.max_backoff)

            elapsed = time.time() - self.open_time
            if elapsed >= open_duration:
                # CRITICAL FIX: Reset failure_count when entering HALF_OPEN
                # This prevents infinite accumulation of failures
                previous_failures = self.failure_count
                self.failure_count = 0
                self.state = "HALF_OPEN"
                _LOGGER.info(
                    "[CIRCUIT] Circuit entering HALF_OPEN state after %.0f seconds "
                    "(was %d failures, now testing recovery)",
                    elapsed,
                    previous_failures
                )
                return True, "Circuit half-open (testing)"

            remaining = open_duration - elapsed
            return False, f"Circuit open ({remaining:.0f}s remaining, {self.failure_count} failures)"

        else:  # HALF_OPEN
            return True, "Circuit half-open (testing)"

    def get_state_info(self) -> str:
        """Get human-readable state information."""
        if self.state == "CLOSED":
            return f"CLOSED (failures: {self.failure_count})"
        elif self.state == "OPEN" and self.open_time:
            # Calculate current open duration with exponential backoff
            backoff_multiplier = 2 ** (self.failure_count - self.failure_threshold)
            open_duration = min(self.base_open_duration * backoff_multiplier, self.max_backoff)

            elapsed = time.time() - self.open_time
            remaining = max(0, open_duration - elapsed)
            return f"OPEN (blocking for {remaining:.0f}s, {self.failure_count} failures)"
        else:
            return f"HALF_OPEN (testing, {self.failure_count} failures)"


class EM1003Device:
    """Representation of an EM1003 BLE device."""

    def __init__(self, hass: HomeAssistant, mac_address: str) -> None:
        """Initialize the device."""
        self.hass = hass
        self.mac_address = mac_address
        self._client: BleakClient | None = None
        self.sensor_data: dict[int, float | None] = {}
        self.buzzer_state: bool | None = None  # Buzzer state (True=on, False=off, None=unknown)
        self._last_disconnect_time: float | None = None

        # Request cache for matching responses to requests
        # Key: (seq_id, sensor_id), Value: PendingRequest
        self._pending_requests: dict[tuple[int, int], PendingRequest] = {}
        self._used_seq_ids: set[int] = set()  # Track used sequence IDs

        # Circuit breaker to prevent request pile-up
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            open_duration=60.0
        )

        # Connection abort tracking for adaptive backoff
        self._connection_abort_count = 0
        self._last_connection_abort_time: float | None = None

        # Track last connection failure for fast-fail behavior
        self._last_connection_failure_time: float | None = None
        self._fast_fail_window = 30.0  # Seconds to fast-fail after connection failure

    def _get_random_sequence_id(self) -> int:
        """Get a random unused sequence ID.

        Uses random IDs to avoid collisions when multiple requests are in flight.
        Cache is limited to 256 entries (max possible sequence IDs).
        """
        # Clean up old sequence IDs if cache is getting full
        if len(self._used_seq_ids) >= 250:
            _LOGGER.debug(
                "[SEQ] Cache nearly full (%d), clearing old sequence IDs",
                len(self._used_seq_ids)
            )
            self._used_seq_ids.clear()

        # Try to find an unused random sequence ID
        for attempt in range(100):
            seq_id = random.randint(0, 255)
            if seq_id not in self._used_seq_ids:
                self._used_seq_ids.add(seq_id)
                return seq_id

        # Fallback: sequential search
        for seq_id in range(256):
            if seq_id not in self._used_seq_ids:
                self._used_seq_ids.add(seq_id)
                _LOGGER.debug("[SEQ] Used sequential fallback, seq_id=%02x", seq_id)
                return seq_id

        # Last resort: clear and start over
        _LOGGER.warning("[SEQ] All 256 sequence IDs exhausted, clearing cache")
        self._used_seq_ids.clear()
        seq_id = random.randint(0, 255)
        self._used_seq_ids.add(seq_id)
        return seq_id

    def _cleanup_expired_requests(self, max_age: float = 10.0) -> None:
        """Clean up expired pending requests.

        Args:
            max_age: Maximum age in seconds for a pending request
        """
        now = time.time()
        expired_keys = [
            key for key, req in self._pending_requests.items()
            if now - req.timestamp > max_age
        ]

        for key in expired_keys:
            seq_id, sensor_id = key
            req = self._pending_requests.pop(key)
            self._used_seq_ids.discard(seq_id)
            if not req.future.done():
                req.future.cancel()
            _LOGGER.debug(
                "[CACHE] Cleaned up expired request: seq=%02x, sensor=%02x (age=%.1fs)",
                seq_id, sensor_id, now - req.timestamp
            )

    async def _ensure_connection_delay(self) -> None:
        """Ensure sufficient delay since last disconnect to avoid connection issues.

        Uses adaptive backoff based on recent connection abort errors:
        - Base delay: 2 seconds (increased from 1 to give Bluetooth stack more time)
        - After connection abort: adds exponential backoff
        - Resets abort count after 5 minutes of no abort errors
        """
        current_time = time.time()

        # Reset connection abort counter if it's been a while since last abort
        if self._last_connection_abort_time is not None:
            time_since_abort = current_time - self._last_connection_abort_time
            if time_since_abort > 300:  # 5 minutes
                self._connection_abort_count = 0
                self._last_connection_abort_time = None

        # Calculate base delay with adaptive backoff for connection aborts
        base_delay = 2.0  # Increased from 1.0 to 2.0 seconds

        # Add exponential backoff for repeated connection aborts
        # abort_count: 0 → 0s, 1 → 2s, 2 → 4s, 3 → 8s, 4 → 16s, max 30s
        abort_backoff = 0.0
        if self._connection_abort_count > 0:
            abort_backoff = min(2.0 ** self._connection_abort_count, 30.0)

        min_delay = base_delay + abort_backoff

        # Wait if we recently disconnected
        if self._last_disconnect_time is not None:
            time_since_disconnect = current_time - self._last_disconnect_time
            if time_since_disconnect < min_delay:
                delay = min_delay - time_since_disconnect
                if abort_backoff > 0:
                    _LOGGER.info("Waiting %.1fs before retry (connection abort backoff)", delay)
                await asyncio.sleep(delay)
        elif abort_backoff > 0:
            # No recent disconnect but we have abort history, add safety delay
            _LOGGER.info("Waiting %.1fs before retry (connection abort backoff)", abort_backoff)
            await asyncio.sleep(abort_backoff)

    async def _establish_connection(self) -> BleakClient:
        """Establish a connection to the device with proper error handling.

        Returns:
            Connected BleakClient instance

        Raises:
            BleakError: If connection fails after retries
        """
        _LOGGER.debug(
            "[DIAG] Starting connection attempt to %s",
            self.mac_address
        )

        # Wait if we recently disconnected to avoid connection abort errors
        await self._ensure_connection_delay()

        # Get device from Home Assistant's Bluetooth integration
        _LOGGER.info(
            "[DIAG] Getting device %s from Home Assistant Bluetooth integration...",
            self.mac_address
        )

        device = bluetooth.async_ble_device_from_address(
            self.hass,
            self.mac_address,
            connectable=True
        )

        if not device:
            _LOGGER.error(
                "[DIAG] ✗ Device %s not found",
                self.mac_address
            )
            raise BleakError(
                f"Device not found: {self.mac_address}. "
                "Make sure device is powered on and nearby."
            )

        device_rssi = getattr(device, 'rssi', None)
        _LOGGER.info(
            "[DIAG] ✓ Found device %s (Name: %s, RSSI: %s dBm)",
            self.mac_address,
            device.name or "Unknown",
            device_rssi if device_rssi is not None else "N/A"
        )

        # Establish connection with timeout and retry logic
        # CRITICAL: Use max_attempts=1 to prevent slot exhaustion
        # Each failed attempt can occupy a BLE slot that doesn't get released immediately
        # Multiple retries can exhaust all available slots, preventing new connections
        _LOGGER.debug(
            "[DIAG] Attempting to establish connection to %s using bleak-retry-connector...",
            self.mac_address
        )

        connection_start_time = time.time()
        client = None
        try:
            _LOGGER.info(
                "[CONN_ROOT_CAUSE] Starting connection attempt to %s (RSSI: %s)",
                self.mac_address, getattr(device, 'rssi', 'N/A')
            )

            client = await establish_connection(
                BleakClient,
                device,
                self.mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=1,  # CRITICAL: Reduced to 1 to prevent slot exhaustion
                timeout=30.0,  # 30 second timeout per attempt
            )

            connection_duration = time.time() - connection_start_time
            _LOGGER.info(
                "[CONN_ROOT_CAUSE] ✓ Connection successful to %s (took %.2fs)",
                self.mac_address, connection_duration
            )

            # Reset connection abort tracking on successful connection
            if self._connection_abort_count > 0:
                self._connection_abort_count = 0
                self._last_connection_abort_time = None

            return client

        except Exception as conn_err:
            # CRITICAL: Clean up any partially established connection
            # Failed connection attempts can leave slots occupied
            if client is not None:
                try:
                    _LOGGER.debug(
                        "[CONN_ROOT_CAUSE] Cleaning up failed connection attempt to %s",
                        self.mac_address
                    )
                    await client.disconnect()
                except Exception as cleanup_err:
                    _LOGGER.debug(
                        "[CONN_ROOT_CAUSE] Error during connection cleanup: %s",
                        cleanup_err
                    )
            connection_duration = time.time() - connection_start_time

            # Analyze the error to determine root cause
            error_message = str(conn_err).lower()
            error_type = type(conn_err).__name__

            # Categorize the error
            is_connection_abort = (
                "connection abort" in error_message or
                "software caused connection abort" in error_message
            )
            is_timeout = "timeout" in error_message or isinstance(conn_err, asyncio.TimeoutError)
            is_device_unreachable = (
                "device unreachable" in error_message or
                "no route to host" in error_message or
                "host is down" in error_message
            )
            is_auth_failed = (
                "authentication" in error_message or
                "pairing" in error_message
            )
            is_resource_busy = (
                "resource busy" in error_message or
                "device busy" in error_message
            )

            # Build diagnostic context
            device_rssi = getattr(device, 'rssi', None)
            device_name = getattr(device, 'name', 'Unknown')

            # Log detailed root cause analysis
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] ✗ Connection FAILED to %s after %.2fs",
                self.mac_address, connection_duration
            )
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Error type: %s", error_type
            )
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Error message: %s", conn_err
            )
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Device info: name=%s, RSSI=%s",
                device_name, device_rssi
            )

            # Determine and log the root cause
            if is_connection_abort:
                self._connection_abort_count += 1
                self._last_connection_abort_time = time.time()
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: CONNECTION ABORT - "
                    "Bluetooth stack aborted connection (count: %d). "
                    "Possible reasons: device too far, interference, device busy, or Bluetooth stack overload",
                    self._connection_abort_count
                )
            elif is_timeout:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: TIMEOUT - "
                    "Device did not respond within 30s. "
                    "Possible reasons: device out of range (RSSI: %s), device sleeping, "
                    "or device not advertising",
                    device_rssi
                )
            elif is_device_unreachable:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: DEVICE UNREACHABLE - "
                    "Device cannot be reached. "
                    "Possible reasons: device powered off, out of range (RSSI: %s), "
                    "or Bluetooth adapter issue",
                    device_rssi
                )
            elif is_auth_failed:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: AUTHENTICATION/PAIRING FAILED - "
                    "Device rejected pairing or authentication. "
                    "Possible reasons: device requires pairing, incorrect PIN, or bonding issue"
                )
            elif is_resource_busy:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: RESOURCE BUSY - "
                    "Bluetooth resource is busy. "
                    "Possible reasons: another process connected to device, "
                    "Bluetooth adapter busy, or previous connection not fully closed"
                )
            else:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: UNKNOWN ERROR - "
                    "Unexpected error during connection. "
                    "Check device status, Bluetooth adapter, and system logs",
                    exc_info=True
                )

            # Log environmental factors
            if device_rssi is not None:
                if device_rssi < -90:
                    _LOGGER.warning(
                        "[CONN_ROOT_CAUSE] ⚠ Signal strength VERY WEAK (RSSI: %s dBm). "
                        "Device is likely too far away or obstructed",
                        device_rssi
                    )
                elif device_rssi < -80:
                    _LOGGER.warning(
                        "[CONN_ROOT_CAUSE] ⚠ Signal strength WEAK (RSSI: %s dBm). "
                        "Connection may be unreliable",
                        device_rssi
                    )
                elif device_rssi < -70:
                    _LOGGER.info(
                        "[CONN_ROOT_CAUSE] Signal strength FAIR (RSSI: %s dBm)",
                        device_rssi
                    )
                else:
                    _LOGGER.info(
                        "[CONN_ROOT_CAUSE] Signal strength GOOD (RSSI: %s dBm)",
                        device_rssi
                    )

            # Log recommended actions
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Recommended actions: "
                "1) Check device is powered on and nearby, "
                "2) Check for Bluetooth interference, "
                "3) Restart Bluetooth adapter if issues persist, "
                "4) Check Home Assistant Bluetooth integration logs"
            )

            raise

    async def _ensure_connected(self) -> BleakClient:
        """Ensure we have an active connection, reusing existing if possible.

        This method prioritizes reusing existing connections:
        1. If self._client exists and is connected, use it immediately
        2. Otherwise, establish a new connection via active BLE scanning

        Returns:
            Connected BleakClient instance

        Raises:
            BleakError: If connection fails or fast-fail is active
        """
        # PRIORITY 1: Check if we already have a valid connection
        if self._client and self._client.is_connected:
            _LOGGER.info(
                "[CONN] ✓ Reusing existing active connection to %s",
                self.mac_address
            )
            return self._client

        _LOGGER.debug(
            "[CONN] No active connection to %s, need to establish new connection",
            self.mac_address
        )

        # Fast-fail if we recently failed to connect (unless circuit breaker is testing)
        if self._last_connection_failure_time is not None:
            time_since_failure = time.time() - self._last_connection_failure_time

            # Only fast-fail if we're not in HALF_OPEN state (testing phase)
            if time_since_failure < self._fast_fail_window and self._circuit_breaker.state != "HALF_OPEN":
                remaining = self._fast_fail_window - time_since_failure
                _LOGGER.debug(
                    "[CONN] Fast-fail: Recent connection failure (%.0fs ago), "
                    "skipping connection attempt for %.0fs more",
                    time_since_failure, remaining
                )
                raise BleakError(
                    f"Fast-fail: Connection failed {time_since_failure:.0f}s ago, "
                    f"will retry after {remaining:.0f}s"
                )

        # PRIORITY 2: Need to establish a new connection via active BLE scanning
        _LOGGER.info(
            "[CONN] Establishing new connection to %s via active BLE scan",
            self.mac_address
        )

        try:
            self._client = await self._establish_connection()

            # Subscribe to notifications (only need to do this once per connection)
            try:
                await self._client.start_notify(EM1003_NOTIFY_CHAR_UUID, self._notification_handler)
                _LOGGER.debug("[CONN] ✓ Connected and subscribed to %s", self.mac_address)

                # Connection successful - clear failure timestamp
                self._last_connection_failure_time = None

            except Exception as err:
                # Failed to subscribe, disconnect and re-raise
                _LOGGER.error("[CONN] Failed to subscribe to notifications: %s", err)
                # CRITICAL: Ensure connection is properly cleaned up to free slot
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                        _LOGGER.debug("[CONN] Disconnected after subscription failure to free slot")
                    except Exception as disconnect_err:
                        _LOGGER.debug("[CONN] Error during cleanup disconnect: %s", disconnect_err)
                self._client = None
                # Record failure timestamp
                self._last_connection_failure_time = time.time()
                raise

            return self._client

        except Exception as err:
            # Record failure timestamp for fast-fail
            self._last_connection_failure_time = time.time()
            # CRITICAL: Ensure client is cleared so it doesn't hold a stale connection
            if self._client is not None:
                try:
                    await self._client.disconnect()
                    _LOGGER.debug("[CONN] Disconnected after connection error to free slot")
                except Exception:
                    pass
                self._client = None
            raise

    async def disconnect(self) -> None:
        """Explicitly disconnect from the device."""
        if self._client and self._client.is_connected:
            try:
                # Stop notifications first
                try:
                    await self._client.stop_notify(EM1003_NOTIFY_CHAR_UUID)
                    _LOGGER.debug("[CONN] Stopped notifications for %s", self.mac_address)
                except Exception as err:
                    _LOGGER.debug("[CONN] Could not stop notifications: %s", err)

                # Disconnect
                await self._client.disconnect()
                _LOGGER.debug("[CONN] Disconnected from %s", self.mac_address)
            except Exception as err:
                _LOGGER.debug("[CONN] Error during disconnect: %s", err)
            finally:
                self._client = None
                self._last_disconnect_time = time.time()
        else:
            _LOGGER.debug("[CONN] Already disconnected from %s", self.mac_address)
            self._client = None

    def _notification_handler(self, sender, data: bytearray) -> None:
        """Handle notification from device.

        Validates response matches pending request by checking (seq_id, sensor_id).
        This prevents accepting wrong responses due to timing issues.
        """
        try:
            if len(data) < 3:
                _LOGGER.warning("[RX] Notification too short: %d bytes, raw: %s", len(data), data.hex())
                return

            seq_id = data[0]
            cmd_type = data[1]
            sensor_id = data[2]
            value_bytes = data[3:]

            # Handle buzzer command response
            if cmd_type == CMD_BUZZER:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug(
                    "[RX] (蜂鸣器响应)[0x %s]",
                    hex_parts
                )

                # Find matching pending request
                request_key = (seq_id, sensor_id)
                pending_request = self._pending_requests.get(request_key)

                if not pending_request:
                    _LOGGER.warning(
                        "[RX] ✗ Buzzer: Unexpected response (seq=0x%02x, no matching request)",
                        seq_id
                    )
                    return

                # Parse buzzer state from response
                if len(value_bytes) >= 1:
                    buzzer_value = value_bytes[0]
                    self.buzzer_state = (buzzer_value == BUZZER_ON)
                    _LOGGER.info(
                        "[RESP] ✓ Buzzer state = %s (0x%02x)",
                        "ON" if self.buzzer_state else "OFF",
                        buzzer_value
                    )
                else:
                    _LOGGER.warning("[RX] ✗ Buzzer: Response too short")

                # Complete the future
                if not pending_request.future.done():
                    pending_request.future.set_result(data)

                del self._pending_requests[request_key]
                self._used_seq_ids.discard(seq_id)
                return

            # Get sensor name for logging
            sensor_info = SENSOR_TYPES.get(sensor_id, {})
            sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")

            # Format: (设备响应)[0x seq-cmd-sensor-value...] 实体XX 传感器名
            hex_parts = ' '.join([f'{b:02x}' for b in data])
            _LOGGER.debug(
                "[RX] (设备响应)[0x %s] 实体%02x %s",
                hex_parts, sensor_id, sensor_name
            )


            # Find matching pending request using (seq_id, sensor_id) key
            request_key = (seq_id, sensor_id)
            pending_request = self._pending_requests.get(request_key)

            if not pending_request:
                _LOGGER.warning(
                    "[RX] ✗ %s: Unexpected response (seq=0x%02x, no matching request)",
                    sensor_name, seq_id
                )
                return

            # Parse value based on sensor type
            # Value is in little-endian format (e.g., 0x31 0x00 = 49)
            if len(value_bytes) >= 2:
                raw_value = int.from_bytes(value_bytes[:2], byteorder='little')

                # Apply sensor-specific scaling and offsets
                formula_desc = ""
                if sensor_id == 0x01:
                    # Temperature: (raw - 4000) / 100
                    self.sensor_data[sensor_id] = (raw_value - 4000) / 100.0
                    formula_desc = f"({raw_value} - 4000) / 100"
                elif sensor_id == 0x06:
                    # Humidity: raw / 100
                    self.sensor_data[sensor_id] = raw_value / 100.0
                    formula_desc = f"{raw_value} / 100"
                elif sensor_id == 0x0A:
                    # Formaldehyde: (raw - 16384) / 1000
                    self.sensor_data[sensor_id] = (raw_value - 16384) / 1000.0
                    formula_desc = f"({raw_value} - 16384) / 1000"
                elif sensor_id in [0x08, 0x09, 0x11, 0x12, 0x13]:
                    # Noise, PM2.5, PM10, TVOC, eCO2: raw value directly
                    self.sensor_data[sensor_id] = raw_value
                    formula_desc = f"{raw_value} (direct)"
                else:
                    # Other sensors use raw value directly (Noise, etc.)
                    self.sensor_data[sensor_id] = raw_value
                    formula_desc = f"{raw_value} (direct)"

                unit = sensor_info.get("unit", "")
                final_value = self.sensor_data.get(sensor_id)

                # Format: 解析: 字节XX → 原始值N → 公式[...] → 最终值V 单位
                value_hex = ' '.join([f'{b:02x}' for b in value_bytes[:2]])
                _LOGGER.debug(
                    "[RX] 解析: 字节[%s] → 原始值%d → 公式[%s] → 最终值%s %s",
                    value_hex, raw_value, formula_desc, final_value, unit
                )

                _LOGGER.info(
                    "[RESP] ✓ %s (0x%02x) = %s %s",
                    sensor_name, sensor_id, final_value, unit
                )
            else:
                _LOGGER.warning(
                    "[RX] ✗ %s: Value too short (got %d bytes, need at least 2)",
                    sensor_name, len(value_bytes)
                )

            # Complete the future and remove from pending requests
            if not pending_request.future.done():
                pending_request.future.set_result(data)

            del self._pending_requests[request_key]
            self._used_seq_ids.discard(seq_id)
            _LOGGER.debug(
                "[CACHE] Removed completed request (seq=%02x, sensor=%02x). "
                "Pending: %d, Used seq_ids: %d",
                seq_id, sensor_id, len(self._pending_requests), len(self._used_seq_ids)
            )

        except Exception as err:
            _LOGGER.error("Error handling notification: %s", err, exc_info=True)

    async def read_sensor(self, sensor_id: int) -> float | None:
        """Read a specific sensor value.

        Args:
            sensor_id: Sensor ID to read

        Returns:
            Sensor value or None if reading fails
        """
        # Check circuit breaker
        can_attempt, reason = self._circuit_breaker.can_attempt()
        if not can_attempt:
            _LOGGER.warning(
                "[CIRCUIT] Blocked read_sensor for 0x%02x: %s",
                sensor_id, reason
            )
            return None

        # Clean up expired requests
        self._cleanup_expired_requests()

        try:
            # Ensure connection (will reuse existing or create new)
            client = await self._ensure_connected()

            # Prepare request with random sequence ID
            seq_id = self._get_random_sequence_id()
            request = bytes([seq_id, CMD_READ_SENSOR, sensor_id])

            # Get sensor name for logging
            sensor_info = SENSOR_TYPES.get(sensor_id, {})
            sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")

            # Format: (请求传感器数据)[0x seq-cmd-sensor] 实体XX 传感器名
            hex_parts = ' '.join([f'{b:02x}' for b in request])
            _LOGGER.debug(
                "[TX] (请求传感器数据)[0x %s] 实体%02x %s",
                hex_parts, sensor_id, sensor_name
            )

            # Create pending request and add to cache
            pending_request = PendingRequest(
                seq_id=seq_id,
                sensor_id=sensor_id,
                future=asyncio.Future(),
                timestamp=time.time()
            )
            request_key = (seq_id, sensor_id)
            self._pending_requests[request_key] = pending_request

            # Send request
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

            # Wait for response with timeout
            try:
                await asyncio.wait_for(pending_request.future, timeout=2.0)
                value = self.sensor_data.get(sensor_id)
                self._circuit_breaker.record_success()
                return value
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timeout waiting for sensor 0x%02x response (seq=%02x)",
                    sensor_id, seq_id
                )
                # Clean up pending request
                self._pending_requests.pop(request_key, None)
                self._used_seq_ids.discard(seq_id)
                self._circuit_breaker.record_failure()
                return None

        except BleakError as err:
            _LOGGER.error("Bleak error reading sensor %02x: %s", sensor_id, err)
            self._circuit_breaker.record_failure()
            # Clear client so next attempt will create new connection
            self._client = None
            return None
        except Exception as err:
            _LOGGER.error("Error reading sensor %02x: %s", sensor_id, err, exc_info=True)
            self._circuit_breaker.record_failure()
            # Clear client so next attempt will create new connection
            self._client = None
            return None

    async def read_all_sensors(self) -> dict[int, float | None]:
        """Read all sensors using persistent connection.

        Uses circuit breaker pattern to prevent request pile-up during failures.
        Uses request cache to match responses to requests by (seq_id, sensor_id).
        Maintains a persistent BLE connection that is reused across multiple reads.

        Returns:
            Dictionary mapping sensor IDs to their values
        """
        _LOGGER.debug("[DIAG] read_all_sensors called for %s", self.mac_address)
        results = {}

        # Check circuit breaker before attempting connection
        can_attempt, reason = self._circuit_breaker.can_attempt()
        if not can_attempt:
            _LOGGER.warning(
                "[CIRCUIT] Blocked read_all_sensors: %s. State: %s",
                reason,
                self._circuit_breaker.get_state_info()
            )
            # Return None for all sensors when circuit is open
            return {sensor_id: None for sensor_id in SENSOR_TYPES.keys()}

        _LOGGER.debug(
            "[CIRCUIT] Attempt allowed: %s. State: %s",
            reason,
            self._circuit_breaker.get_state_info()
        )

        # Clean up any expired requests before starting
        self._cleanup_expired_requests()

        try:
            # Ensure connection (will reuse existing or create new)
            _LOGGER.debug("[CONN] Ensuring connection to %s", self.mac_address)
            client = await self._ensure_connected()

            _LOGGER.info(
                "[DIAG] Using connection to %s, starting sensor reads",
                self.mac_address
            )

            # Read all sensors using the persistent connection
            sensor_count = len(SENSOR_TYPES)
            _LOGGER.debug(
                "[DIAG] Reading %d sensors: %s",
                sensor_count,
                [f"0x{sid:02x}" for sid in SENSOR_TYPES.keys()]
            )

            for idx, sensor_id in enumerate(SENSOR_TYPES.keys(), 1):
                # Check if connection is still valid before each read
                if not client.is_connected:
                    _LOGGER.warning(
                        "[REQ] [%d/%d] Connection lost, aborting remaining sensor reads",
                        idx, sensor_count
                    )
                    # Mark remaining sensors as None
                    for remaining_id in list(SENSOR_TYPES.keys())[idx-1:]:
                        results[remaining_id] = None
                    break

                try:
                    # Get random sequence ID to avoid collisions
                    seq_id = self._get_random_sequence_id()
                    request = bytes([seq_id, CMD_READ_SENSOR, sensor_id])

                    # Get sensor name for logging
                    sensor_info = SENSOR_TYPES.get(sensor_id, {})
                    sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")

                    if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        _LOGGER.info(
                            "[%s] Requesting sensor 0x%02x (seq=%02x)",
                            sensor_name, sensor_id, seq_id
                        )

                    # Format: (请求传感器数据)[0x seq-cmd-sensor] 实体XX 传感器名
                    hex_parts = ' '.join([f'{b:02x}' for b in request])
                    _LOGGER.debug(
                        "[TX] [%d/%d] (请求传感器数据)[0x %s] 实体%02x %s",
                        idx, sensor_count, hex_parts, sensor_id, sensor_name
                    )

                    # Create pending request and add to cache
                    pending_request = PendingRequest(
                        seq_id=seq_id,
                        sensor_id=sensor_id,
                        future=asyncio.Future(),
                        timestamp=time.time()
                    )
                    request_key = (seq_id, sensor_id)
                    self._pending_requests[request_key] = pending_request


                    # Send request
                    await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)


                    # Wait for response with timeout
                    try:
                        await asyncio.wait_for(pending_request.future, timeout=2.0)
                        # Get parsed value from sensor_data (set by notification handler)
                        value = self.sensor_data.get(sensor_id)
                        results[sensor_id] = value

                        if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                            _LOGGER.info(
                                "[%s] ✓ Got value: %s",
                                sensor_name, value
                            )

                        _LOGGER.debug(
                            "[REQ] [%d/%d] ✓ Sensor 0x%02x = %s",
                            idx, sensor_count, sensor_id, value
                        )
                    except asyncio.TimeoutError:
                        if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                            _LOGGER.warning(
                                "[%s] ✗ TIMEOUT (2s) - sensor 0x%02x not responding",
                                sensor_name, sensor_id
                            )
                        else:
                            _LOGGER.warning(
                                "[REQ] [%d/%d] ✗ Timeout waiting for sensor 0x%02x response (seq=%02x)",
                                idx, sensor_count, sensor_id, seq_id
                            )
                        results[sensor_id] = None
                        # Clean up pending request on timeout
                        self._pending_requests.pop(request_key, None)
                        self._used_seq_ids.discard(seq_id)

                    # Small delay between sensor reads
                    await asyncio.sleep(0.3)

                except BleakError as err:
                    if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        _LOGGER.error(
                            "[%s] ✗ BLE error: %s",
                            sensor_name, err
                        )
                    else:
                        _LOGGER.error(
                            "[REQ] [%d/%d] ✗ BLE error reading sensor 0x%02x: %s",
                            idx, sensor_count, sensor_id, err
                        )
                    results[sensor_id] = None
                    # Clean up on error
                    request_key = (seq_id, sensor_id)
                    self._pending_requests.pop(request_key, None)
                    self._used_seq_ids.discard(seq_id)

                    # If we get a BLE error, connection might be broken
                    # Check and abort if disconnected
                    if not client.is_connected:
                        _LOGGER.warning(
                            "[REQ] Connection lost after BLE error, aborting remaining reads"
                        )
                        # Mark remaining sensors as None
                        for remaining_id in list(SENSOR_TYPES.keys())[idx:]:
                            results[remaining_id] = None
                        break
                except Exception as err:
                    if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        _LOGGER.error(
                            "[%s] ✗ Error: %s",
                            sensor_name, err
                        )
                    else:
                        _LOGGER.error(
                            "[REQ] [%d/%d] ✗ Error reading sensor 0x%02x: %s",
                            idx, sensor_count, sensor_id, err
                        )
                    results[sensor_id] = None
                    # Clean up on error
                    request_key = (seq_id, sensor_id)
                    self._pending_requests.pop(request_key, None)
                    self._used_seq_ids.discard(seq_id)

            # Calculate success rate
            success_count = sum(1 for v in results.values() if v is not None)

            _LOGGER.info(
                "[DIAG] Completed reading all sensors. Success: %d/%d",
                success_count,
                len(results)
            )

            # Record success or failure to circuit breaker
            if success_count >= len(results) * 0.5:  # At least 50% success
                self._circuit_breaker.record_success()
            else:
                _LOGGER.warning(
                    "[CIRCUIT] Low success rate (%d/%d), recording failure",
                    success_count, len(results)
                )
                self._circuit_breaker.record_failure()

            # CRITICAL: Disconnect immediately after reading to free up connection slot
            # EM1003 device responds very fast (<2s), no need to keep connection open
            # This prevents "No backend with an available connection slot" errors
            if self._client and self._client.is_connected:
                try:
                    _LOGGER.info(
                        "[CONN] Disconnecting after successful read to free connection slot"
                    )
                    await self.disconnect()
                except Exception as disconnect_err:
                    _LOGGER.debug(
                        "[CONN] Error during post-read disconnect: %s",
                        disconnect_err
                    )

            return results

        except BleakError as err:
            # Connection or BLE error - clean up client and record failure
            _LOGGER.error(
                "[DIAG] ✗ BLE error while reading sensors from %s: %s",
                self.mac_address, err
            )
            self._circuit_breaker.record_failure()
            # CRITICAL: Disconnect to free connection slot even on error
            if self._client is not None:
                try:
                    await self.disconnect()
                    _LOGGER.debug("[CONN] Disconnected after BLE error to free slot")
                except Exception as disconnect_err:
                    _LOGGER.debug("[CONN] Error during error-path disconnect: %s", disconnect_err)
                self._client = None
            raise
        except Exception as err:
            # Unexpected error - clean up and record failure
            _LOGGER.error(
                "[DIAG] ✗ Error while reading sensors from %s: %s",
                self.mac_address, err, exc_info=True
            )
            self._circuit_breaker.record_failure()
            # CRITICAL: Disconnect to free connection slot even on error
            if self._client is not None:
                try:
                    await self.disconnect()
                    _LOGGER.debug("[CONN] Disconnected after error to free slot")
                except Exception as disconnect_err:
                    _LOGGER.debug("[CONN] Error during error-path disconnect: %s", disconnect_err)
                self._client = None
            raise

    async def read_buzzer_state(self) -> bool | None:
        """Read current buzzer state.

        Returns:
            True if buzzer is on, False if off, None if reading fails
        """
        # Check circuit breaker
        can_attempt, reason = self._circuit_breaker.can_attempt()
        if not can_attempt:
            _LOGGER.warning(
                "[CIRCUIT] Blocked read_buzzer_state: %s",
                reason
            )
            return None

        # Clean up expired requests
        self._cleanup_expired_requests()

        try:
            # Ensure connection
            client = await self._ensure_connected()

            # Prepare request with random sequence ID
            # Query buzzer state: [seq_id][0x50][0x00]
            seq_id = self._get_random_sequence_id()
            request = bytes([seq_id, CMD_BUZZER, 0x00])

            hex_parts = ' '.join([f'{b:02x}' for b in request])
            _LOGGER.debug(
                "[TX] (查询蜂鸣器状态)[0x %s]",
                hex_parts
            )

            # Create pending request and add to cache
            pending_request = PendingRequest(
                seq_id=seq_id,
                sensor_id=0x00,  # Use 0x00 as placeholder for buzzer
                future=asyncio.Future(),
                timestamp=time.time()
            )
            request_key = (seq_id, 0x00)
            self._pending_requests[request_key] = pending_request

            # Send request
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

            # Wait for response with timeout
            try:
                await asyncio.wait_for(pending_request.future, timeout=2.0)
                self._circuit_breaker.record_success()
                return self.buzzer_state
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timeout waiting for buzzer state response (seq=%02x)",
                    seq_id
                )
                # Clean up pending request
                self._pending_requests.pop(request_key, None)
                self._used_seq_ids.discard(seq_id)
                self._circuit_breaker.record_failure()
                return None

        except BleakError as err:
            _LOGGER.error("Bleak error reading buzzer state: %s", err)
            self._circuit_breaker.record_failure()
            self._client = None
            return None
        except Exception as err:
            _LOGGER.error("Error reading buzzer state: %s", err, exc_info=True)
            self._circuit_breaker.record_failure()
            self._client = None
            return None

    async def set_buzzer_state(self, turn_on: bool) -> bool:
        """Set buzzer state.

        Args:
            turn_on: True to turn on buzzer, False to turn off

        Returns:
            True if successful, False otherwise
        """
        # Check circuit breaker
        can_attempt, reason = self._circuit_breaker.can_attempt()
        if not can_attempt:
            _LOGGER.warning(
                "[CIRCUIT] Blocked set_buzzer_state: %s",
                reason
            )
            return False

        # Clean up expired requests
        self._cleanup_expired_requests()

        try:
            # Ensure connection
            client = await self._ensure_connected()

            # Prepare request with random sequence ID
            # Set buzzer state: [seq_id][0x50][0x00][state]
            seq_id = self._get_random_sequence_id()
            state_byte = BUZZER_ON if turn_on else BUZZER_OFF
            request = bytes([seq_id, CMD_BUZZER, 0x00, state_byte])

            hex_parts = ' '.join([f'{b:02x}' for b in request])
            _LOGGER.debug(
                "[TX] (设置蜂鸣器状态)[0x %s] %s",
                hex_parts,
                "开启" if turn_on else "关闭"
            )

            # Create pending request and add to cache
            pending_request = PendingRequest(
                seq_id=seq_id,
                sensor_id=0x00,  # Use 0x00 as placeholder for buzzer
                future=asyncio.Future(),
                timestamp=time.time()
            )
            request_key = (seq_id, 0x00)
            self._pending_requests[request_key] = pending_request

            # Send request
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

            # Wait for response with timeout
            try:
                await asyncio.wait_for(pending_request.future, timeout=2.0)

                # Verify the state was set correctly
                if self.buzzer_state == turn_on:
                    _LOGGER.info(
                        "[BUZZER] ✓ Successfully %s buzzer",
                        "turned on" if turn_on else "turned off"
                    )
                    self._circuit_breaker.record_success()
                    return True
                else:
                    _LOGGER.warning(
                        "[BUZZER] ✗ State mismatch: expected %s, got %s",
                        turn_on, self.buzzer_state
                    )
                    self._circuit_breaker.record_failure()
                    return False

            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timeout waiting for buzzer set response (seq=%02x)",
                    seq_id
                )
                # Clean up pending request
                self._pending_requests.pop(request_key, None)
                self._used_seq_ids.discard(seq_id)
                self._circuit_breaker.record_failure()
                return False

        except BleakError as err:
            _LOGGER.error("Bleak error setting buzzer state: %s", err)
            self._circuit_breaker.record_failure()
            self._client = None
            return False
        except Exception as err:
            _LOGGER.error("Error setting buzzer state: %s", err, exc_info=True)
            self._circuit_breaker.record_failure()
            self._client = None
            return False
