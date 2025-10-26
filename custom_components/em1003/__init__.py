"""The EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import (
    DOMAIN,
    CONF_MAC_ADDRESS,
    SERVICE_SCAN_DEVICE,
    SERVICE_READ_CHARACTERISTIC,
    SERVICE_WRITE_CHARACTERISTIC,
    SERVICE_LIST_SERVICES,
    SERVICE_DISCOVER_ALL,
    SERVICE_READ_DEVICE_NAME,
    ATTR_MAC_ADDRESS,
    ATTR_SERVICE_UUID,
    ATTR_CHARACTERISTIC_UUID,
    ATTR_DATA,
    DEVICE_TIMEOUT,
    DEVICE_NAME_UUID,
    EM1003_SERVICE_UUID,
    EM1003_WRITE_CHAR_UUID,
    EM1003_NOTIFY_CHAR_UUID,
    CMD_READ_SENSOR,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


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


async def async_read_device_name(hass: HomeAssistant, mac_address: str) -> str | None:
    """Read device name from BLE device using Device Name characteristic.

    Args:
        hass: Home Assistant instance
        mac_address: MAC address of the BLE device

    Returns:
        Device name as string, or None if reading fails
    """
    try:
        device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)

        if not device:
            _LOGGER.warning("Device not found when reading name: %s", mac_address)
            return None

        client = await establish_connection(
            BleakClient,
            device,
            mac_address,
            disconnected_callback=lambda _: None,
            max_attempts=3,  # Reduced to avoid overwhelming Bluetooth stack
            timeout=30.0,
        )

        try:
            _LOGGER.debug("Connected to device %s to read name", mac_address)

            # Read the Device Name characteristic (0x2A00)
            value = await client.read_gatt_char(DEVICE_NAME_UUID)
            device_name = value.decode('utf-8').strip()

            _LOGGER.info("Read device name from %s: %s", mac_address, device_name)
            return device_name
        finally:
            await client.disconnect()

    except BleakError as err:
        _LOGGER.error("Bleak error reading device name from %s: %s", mac_address, err)
        return None
    except Exception as err:
        _LOGGER.error("Error reading device name from %s: %s", mac_address, err)
        return None


class EM1003Device:
    """Representation of an EM1003 BLE device."""

    def __init__(self, hass: HomeAssistant, mac_address: str) -> None:
        """Initialize the device."""
        self.hass = hass
        self.mac_address = mac_address
        self._client: BleakClient | None = None
        self.sensor_data: dict[int, float | None] = {}
        self._connection_lock = asyncio.Lock()  # Prevent concurrent connections
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
                if self._connection_abort_count > 0:
                    _LOGGER.info(
                        "[BACKOFF] Resetting connection abort count (%d) after %.0f seconds of stability",
                        self._connection_abort_count,
                        time_since_abort
                    )
                self._connection_abort_count = 0
                self._last_connection_abort_time = None

        # Calculate base delay with adaptive backoff for connection aborts
        base_delay = 2.0  # Increased from 1.0 to 2.0 seconds

        # Add exponential backoff for repeated connection aborts
        # abort_count: 0 → 0s, 1 → 2s, 2 → 4s, 3 → 8s, 4 → 16s, max 30s
        abort_backoff = 0.0
        if self._connection_abort_count > 0:
            abort_backoff = min(2.0 ** self._connection_abort_count, 30.0)
            _LOGGER.info(
                "[BACKOFF] Adding %.1fs backoff for %d recent connection abort(s)",
                abort_backoff,
                self._connection_abort_count
            )

        min_delay = base_delay + abort_backoff

        # Wait if we recently disconnected
        if self._last_disconnect_time is not None:
            time_since_disconnect = current_time - self._last_disconnect_time
            if time_since_disconnect < min_delay:
                delay = min_delay - time_since_disconnect
                _LOGGER.info(
                    "[BACKOFF] Waiting %.2fs before reconnecting (base=%.1fs + abort_backoff=%.1fs, "
                    "time_since_disconnect=%.2fs)",
                    delay, base_delay, abort_backoff, time_since_disconnect
                )
                await asyncio.sleep(delay)
            else:
                _LOGGER.debug(
                    "[BACKOFF] No wait needed, %.2fs elapsed since disconnect (min_delay=%.2fs)",
                    time_since_disconnect, min_delay
                )
        elif abort_backoff > 0:
            # No recent disconnect but we have abort history, add safety delay
            _LOGGER.info(
                "[BACKOFF] Adding %.2fs safety delay due to %d recent connection abort(s)",
                abort_backoff, self._connection_abort_count
            )
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

        # Get a fresh device reference to ensure we have the latest advertising data
        _LOGGER.debug(
            "[DIAG] Looking for device %s in Bluetooth cache...",
            self.mac_address
        )
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )

        if device:
            _LOGGER.debug(
                "[DIAG] Device %s found in cache (RSSI: %s)",
                self.mac_address,
                getattr(device, 'rssi', 'N/A')
            )
        else:
            _LOGGER.info(
                "[DIAG] Device %s not in cache, performing active BLE scan...",
                self.mac_address
            )

            try:
                # Perform active scan to discover the device
                scan_start = time.time()
                devices = await BleakScanner.discover(timeout=10.0)
                scan_duration = time.time() - scan_start

                _LOGGER.debug(
                    "[DIAG] BLE scan completed in %.2f seconds, found %d devices",
                    scan_duration,
                    len(devices)
                )

                found = False
                for scanned_device in devices:
                    _LOGGER.debug(
                        "[DIAG] Scanned device: %s (%s) RSSI: %s",
                        scanned_device.address,
                        scanned_device.name or "Unknown",
                        scanned_device.rssi
                    )
                    if scanned_device.address.upper() == self.mac_address.upper():
                        _LOGGER.info(
                            "[DIAG] ✓ Found target device %s during scan (Name: %s, RSSI: %s)",
                            self.mac_address,
                            scanned_device.name or "Unknown",
                            scanned_device.rssi
                        )
                        found = True
                        break

                if not found:
                    _LOGGER.warning(
                        "[DIAG] ✗ Device %s not found during scan. Scanned %d devices.",
                        self.mac_address,
                        len(devices)
                    )

                # Try to get device from cache again after scan
                # The scan should have populated the cache
                _LOGGER.debug(
                    "[DIAG] Waiting 0.5s for cache update, then checking cache again..."
                )
                await asyncio.sleep(0.5)  # Small delay for cache update
                device = bluetooth.async_ble_device_from_address(
                    self.hass, self.mac_address, connectable=True
                )

                if device:
                    _LOGGER.info(
                        "[DIAG] ✓ Device %s now available in cache after scan",
                        self.mac_address
                    )
                else:
                    _LOGGER.warning(
                        "[DIAG] ✗ Device %s still not in cache after scan",
                        self.mac_address
                    )

            except Exception as scan_err:
                _LOGGER.warning(
                    "[DIAG] Error during BLE scan for %s: %s",
                    self.mac_address,
                    scan_err,
                    exc_info=True
                )

        if not device:
            _LOGGER.error(
                "[DIAG] ✗ Device %s not found after all attempts",
                self.mac_address
            )
            raise BleakError(f"Device not found: {self.mac_address}")

        # Establish connection with timeout and retry logic
        # Use fewer attempts with longer timeout to avoid overwhelming Bluetooth stack
        _LOGGER.debug(
            "[DIAG] Attempting to establish connection to %s using bleak-retry-connector...",
            self.mac_address
        )

        try:
            client = await establish_connection(
                BleakClient,
                device,
                self.mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=3,  # Reduced from 5 to 3 to avoid stack exhaustion
                timeout=30.0,  # 30 second timeout per attempt
            )

            _LOGGER.info(
                "[DIAG] ✓ Successfully connected to %s",
                self.mac_address
            )

            # Reset connection abort tracking on successful connection
            if self._connection_abort_count > 0:
                _LOGGER.info(
                    "[BACKOFF] Connection successful, resetting abort count from %d to 0",
                    self._connection_abort_count
                )
                self._connection_abort_count = 0
                self._last_connection_abort_time = None

            return client

        except Exception as conn_err:
            # Check if this is a connection abort error
            error_message = str(conn_err).lower()
            is_connection_abort = (
                "connection abort" in error_message or
                "software caused connection abort" in error_message
            )

            if is_connection_abort:
                self._connection_abort_count += 1
                self._last_connection_abort_time = time.time()
                _LOGGER.warning(
                    "[DIAG] ✗ Connection abort error #%d for %s: %s",
                    self._connection_abort_count,
                    self.mac_address,
                    conn_err
                )
                _LOGGER.info(
                    "[BACKOFF] Next connection attempt will wait an additional %.1fs",
                    min(2.0 ** self._connection_abort_count, 30.0)
                )
            else:
                _LOGGER.error(
                    "[DIAG] ✗ Failed to connect to %s: %s",
                    self.mac_address,
                    conn_err,
                    exc_info=True
                )
            raise

    def _notification_handler(self, sender, data: bytearray) -> None:
        """Handle notification from device.

        Validates response matches pending request by checking (seq_id, sensor_id).
        This prevents accepting wrong responses due to timing issues.
        """
        try:
            _LOGGER.debug("Received notification from %s: %s", self.mac_address, data.hex())

            if len(data) < 3:
                _LOGGER.warning("Notification too short: %d bytes", len(data))
                return

            seq_id = data[0]
            cmd_type = data[1]
            sensor_id = data[2]
            value_bytes = data[3:]

            # Special logging for problematic sensors
            if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                sensor_info = SENSOR_TYPES.get(sensor_id, {})
                sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                _LOGGER.warning(
                    "[TRACK-%s] Step 1: Notification received - seq=%02x, cmd=%02x, raw_data=%s, value_bytes=%s",
                    sensor_name, seq_id, cmd_type, data.hex(), value_bytes.hex()
                )

            # Find matching pending request using (seq_id, sensor_id) key
            request_key = (seq_id, sensor_id)
            pending_request = self._pending_requests.get(request_key)

            if not pending_request:
                if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                    sensor_info = SENSOR_TYPES.get(sensor_id, {})
                    sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                    _LOGGER.warning(
                        "[TRACK-%s] Step 2: ✗ NO MATCHING REQUEST FOUND - seq=%02x, sensor=%02x, value=%s. "
                        "Current pending requests: %s",
                        sensor_name, seq_id, sensor_id, value_bytes.hex(),
                        {f"(seq={k[0]:02x},sensor={k[1]:02x})": f"age={time.time()-v.timestamp:.1f}s"
                         for k, v in self._pending_requests.items()}
                    )
                _LOGGER.warning(
                    "[RESP] ✗ Received unexpected response: seq=%02x, sensor=%02x, value=%s "
                    "(no matching pending request)",
                    seq_id, sensor_id, value_bytes.hex()
                )
                return

            if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                sensor_info = SENSOR_TYPES.get(sensor_id, {})
                sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                _LOGGER.warning(
                    "[TRACK-%s] Step 2: ✓ Matched pending request - seq=%02x, request_age=%.2fs",
                    sensor_name, seq_id, time.time() - pending_request.timestamp
                )

            _LOGGER.debug(
                "[RESP] ✓ Matched response: seq=%02x, cmd=%02x, sensor=%02x, value=%s",
                seq_id, cmd_type, sensor_id, value_bytes.hex()
            )

            # Parse value based on sensor type
            # Value is in little-endian format (e.g., 0x31 0x00 = 49)
            if len(value_bytes) >= 2:
                raw_value = int.from_bytes(value_bytes[:2], byteorder='little')

                # Get sensor name for better logging
                sensor_info = SENSOR_TYPES.get(sensor_id, {})
                sensor_name = sensor_info.get("name", f"Unknown(0x{sensor_id:02x})")

                if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                    _LOGGER.warning(
                        "[TRACK-%s] Step 3: Parsing value - value_bytes=%s, length=%d",
                        sensor_name, value_bytes.hex(), len(value_bytes)
                    )
                    _LOGGER.warning(
                        "[TRACK-%s] Step 3: Raw value parsed - byte0=0x%02x, byte1=0x%02x, raw_value=%d",
                        sensor_name, value_bytes[0], value_bytes[1] if len(value_bytes) > 1 else 0, raw_value
                    )

                _LOGGER.debug(
                    "[RESP] Sensor %s (0x%02x) raw value: %d (bytes: %s)",
                    sensor_name, sensor_id, raw_value, value_bytes[:2].hex()
                )

                # Apply sensor-specific scaling and offsets
                if sensor_id == 0x01:
                    # Temperature: (raw - 4000) / 100
                    self.sensor_data[sensor_id] = (raw_value - 4000) / 100.0
                    _LOGGER.debug("[RESP] Temperature formula: (%d - 4000) / 100 = %.2f°C", raw_value, self.sensor_data[sensor_id])
                elif sensor_id == 0x06:
                    # Humidity: raw / 100
                    self.sensor_data[sensor_id] = raw_value / 100.0
                    _LOGGER.debug("[RESP] Humidity formula: %d / 100 = %.2f%%", raw_value, self.sensor_data[sensor_id])
                elif sensor_id == 0x0A:
                    # Formaldehyde: (raw - 16384) / 1000
                    self.sensor_data[sensor_id] = (raw_value - 16384) / 1000.0
                    _LOGGER.debug("[RESP] Formaldehyde formula: (%d - 16384) / 1000 = %.3f mg/m³", raw_value, self.sensor_data[sensor_id])
                elif sensor_id == 0x11:
                    # PM10: raw value directly
                    self.sensor_data[sensor_id] = raw_value
                    _LOGGER.warning("[TRACK-PM10] Step 4: Conversion applied - raw_value=%d, final_value=%d µg/m³",
                                  raw_value, self.sensor_data[sensor_id])
                elif sensor_id == 0x12:
                    # TVOC: raw value is directly in µg/m³
                    # Device returns: raw * 0.001 mg/m³, which equals raw * 0.001 * 1000 = raw µg/m³
                    self.sensor_data[sensor_id] = raw_value
                    _LOGGER.warning("[TRACK-TVOC] Step 4: Conversion applied - raw_value=%d, final_value=%d µg/m³",
                                  raw_value, self.sensor_data[sensor_id])
                elif sensor_id == 0x13:
                    # eCO2: raw value directly
                    self.sensor_data[sensor_id] = raw_value
                    _LOGGER.warning("[TRACK-eCO2] Step 4: Conversion applied - raw_value=%d, final_value=%d ppm",
                                  raw_value, self.sensor_data[sensor_id])
                else:
                    # Other sensors use raw value directly (PM2.5, Noise)
                    self.sensor_data[sensor_id] = raw_value
                    _LOGGER.debug("[RESP] %s: raw value %d (no conversion)", sensor_name, raw_value)

                if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                    _LOGGER.warning(
                        "[TRACK-%s] Step 5: Stored in sensor_data - sensor_data[0x%02x] = %s",
                        sensor_name, sensor_id, self.sensor_data.get(sensor_id)
                    )

                _LOGGER.info(
                    "[RESP] ✓ %s (0x%02x) = %s %s",
                    sensor_name,
                    sensor_id,
                    self.sensor_data.get(sensor_id),
                    sensor_info.get("unit", "")
                )
            else:
                if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                    sensor_info = SENSOR_TYPES.get(sensor_id, {})
                    sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                    _LOGGER.warning(
                        "[TRACK-%s] Step 3: ✗ VALUE TOO SHORT - value_bytes=%s, length=%d (need at least 2)",
                        sensor_name, value_bytes.hex(), len(value_bytes)
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

        # Use lock to prevent concurrent connection attempts
        async with self._connection_lock:
            # Clean up expired requests
            self._cleanup_expired_requests()

            try:
                # Establish connection with improved error handling
                client = await self._establish_connection()

                try:
                    _LOGGER.debug("Connected to device %s", self.mac_address)

                    # Subscribe to notifications
                    await client.start_notify(EM1003_NOTIFY_CHAR_UUID, self._notification_handler)
                    _LOGGER.debug("Subscribed to notifications")

                    # Prepare request with random sequence ID
                    seq_id = self._get_random_sequence_id()
                    request = bytes([seq_id, CMD_READ_SENSOR, sensor_id])

                    _LOGGER.debug(
                        "Sending request to sensor 0x%02x: seq=%02x, data=%s",
                        sensor_id, seq_id, request.hex()
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
                        await asyncio.wait_for(pending_request.future, timeout=5.0)
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
                    finally:
                        # Stop notifications
                        await client.stop_notify(EM1003_NOTIFY_CHAR_UUID)

                finally:
                    await client.disconnect()
                    self._last_disconnect_time = time.time()

            except BleakError as err:
                _LOGGER.error("Bleak error reading sensor %02x: %s", sensor_id, err)
                self._circuit_breaker.record_failure()
                return None
            except Exception as err:
                _LOGGER.error("Error reading sensor %02x: %s", sensor_id, err, exc_info=True)
                self._circuit_breaker.record_failure()
                return None

    async def read_all_sensors(self) -> dict[int, float | None]:
        """Read all sensors and return their values.

        Uses circuit breaker pattern to prevent request pile-up during failures.
        Uses request cache to match responses to requests by (seq_id, sensor_id).

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

        # Use lock to prevent concurrent connection attempts
        _LOGGER.debug("[DIAG] Acquiring connection lock for %s", self.mac_address)
        async with self._connection_lock:
            _LOGGER.debug("[DIAG] Connection lock acquired for %s", self.mac_address)

            # Clean up any expired requests before starting
            self._cleanup_expired_requests()

            try:
                # Establish connection with improved error handling
                _LOGGER.debug("[DIAG] Calling _establish_connection for %s", self.mac_address)
                client = await self._establish_connection()

                try:
                    _LOGGER.info(
                        "[DIAG] Connected to device %s, starting sensor reads",
                        self.mac_address
                    )

                    # Subscribe to notifications once
                    _LOGGER.debug("[DIAG] Subscribing to notifications...")
                    await client.start_notify(EM1003_NOTIFY_CHAR_UUID, self._notification_handler)
                    _LOGGER.debug("[DIAG] ✓ Subscribed to notifications")

                    # Read all sensors using the same connection
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
                                _LOGGER.warning(
                                    "[TRACK-%s] Step 0: Preparing request - seq=%02x, sensor=0x%02x, request_data=%s",
                                    sensor_name, seq_id, sensor_id, request.hex()
                                )

                            _LOGGER.debug(
                                "[REQ] [%d/%d] Sending request: seq=%02x, sensor=0x%02x, data=%s",
                                idx, sensor_count, seq_id, sensor_id, request.hex()
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

                            if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                                _LOGGER.warning(
                                    "[TRACK-%s] Step 0.5: Pending request created - request_key=(seq=%02x,sensor=%02x), "
                                    "timestamp=%.2f, total_pending=%d",
                                    sensor_name, seq_id, sensor_id, pending_request.timestamp,
                                    len(self._pending_requests)
                                )

                            # Send request
                            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

                            if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                                _LOGGER.warning(
                                    "[TRACK-%s] Step 0.8: Request sent to device via write_gatt_char, now waiting for response...",
                                    sensor_name
                                )

                            # Wait for response with timeout
                            try:
                                await asyncio.wait_for(pending_request.future, timeout=5.0)
                                # Get parsed value from sensor_data (set by notification handler)
                                value = self.sensor_data.get(sensor_id)
                                results[sensor_id] = value

                                if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                                    _LOGGER.warning(
                                        "[TRACK-%s] Step 6: ✓ Response received and processed - value=%s, "
                                        "stored in results[0x%02x]",
                                        sensor_name, value, sensor_id
                                    )

                                _LOGGER.debug(
                                    "[REQ] [%d/%d] ✓ Sensor 0x%02x = %s",
                                    idx, sensor_count, sensor_id, value
                                )
                            except asyncio.TimeoutError:
                                if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                                    _LOGGER.warning(
                                        "[TRACK-%s] Step X: ✗ TIMEOUT after 5.0s - No response received from device. "
                                        "Request was: seq=%02x, sensor=0x%02x",
                                        sensor_name, seq_id, sensor_id
                                    )
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
                                _LOGGER.warning(
                                    "[TRACK-%s] Step X: ✗ BLEAK ERROR - %s. Request was: seq=%02x, sensor=0x%02x",
                                    sensor_name, err, seq_id, sensor_id
                                )
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
                                _LOGGER.warning(
                                    "[TRACK-%s] Step X: ✗ EXCEPTION - %s. Request was: seq=%02x, sensor=0x%02x",
                                    sensor_name, err, seq_id, sensor_id
                                )
                            _LOGGER.error(
                                "[REQ] [%d/%d] ✗ Error reading sensor 0x%02x: %s",
                                idx, sensor_count, sensor_id, err
                            )
                            results[sensor_id] = None
                            # Clean up on error
                            request_key = (seq_id, sensor_id)
                            self._pending_requests.pop(request_key, None)
                            self._used_seq_ids.discard(seq_id)

                    # Stop notifications (if still connected)
                    if client.is_connected:
                        try:
                            _LOGGER.debug("[DIAG] Stopping notifications...")
                            await client.stop_notify(EM1003_NOTIFY_CHAR_UUID)
                            _LOGGER.debug("[DIAG] ✓ Notifications stopped")
                        except Exception as err:
                            _LOGGER.debug("[DIAG] Could not stop notifications (connection may be lost): %s", err)
                    else:
                        _LOGGER.debug("[DIAG] Connection already lost, skipping notification stop")

                    success_count = sum(1 for v in results.values() if v is not None)

                    # Log final status of problematic sensors
                    for sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        sensor_info = SENSOR_TYPES.get(sensor_id, {})
                        sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                        value = results.get(sensor_id)
                        cached_value = self.sensor_data.get(sensor_id)
                        _LOGGER.warning(
                            "[TRACK-%s] FINAL: results[0x%02x]=%s, sensor_data[0x%02x]=%s",
                            sensor_name, sensor_id, value, sensor_id, cached_value
                        )

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

                finally:
                    # Disconnect if still connected
                    if client.is_connected:
                        try:
                            _LOGGER.debug("[DIAG] Disconnecting from %s...", self.mac_address)
                            await client.disconnect()
                            self._last_disconnect_time = time.time()
                            _LOGGER.debug("[DIAG] ✓ Disconnected from device %s", self.mac_address)
                        except Exception as err:
                            _LOGGER.debug("[DIAG] Error during disconnect: %s", err)
                            self._last_disconnect_time = time.time()
                    else:
                        _LOGGER.debug("[DIAG] Already disconnected from %s", self.mac_address)
                        self._last_disconnect_time = time.time()

            except BleakError as err:
                _LOGGER.error(
                    "[DIAG] ✗ Bleak error reading all sensors from %s: %s",
                    self.mac_address, err
                )
                self._circuit_breaker.record_failure()
            except Exception as err:
                _LOGGER.error(
                    "[DIAG] ✗ Error reading all sensors from %s: %s",
                    self.mac_address, err, exc_info=True
                )
                self._circuit_breaker.record_failure()

        _LOGGER.debug("[DIAG] Released connection lock for %s", self.mac_address)
        return results


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EM1003 BLE Sensor from a config entry."""
    mac_address = entry.data[CONF_MAC_ADDRESS]

    _LOGGER.info("Setting up EM1003 device with MAC: %s", mac_address)

    # Try to read device name from BLE device
    device_name = await async_read_device_name(hass, mac_address)

    if device_name:
        _LOGGER.info("Successfully read device name: %s", device_name)
    else:
        _LOGGER.warning("Could not read device name, using default: %s", entry.title)
        device_name = entry.title

    # Create EM1003 device instance
    em1003_device = EM1003Device(hass, mac_address)

    # Register device in device registry before creating entities
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, mac_address)},
        name=device_name,
        manufacturer="EM1003",
        model="BLE Air Quality Sensor",
        connections={("mac", mac_address)},
    )
    _LOGGER.info("Device registered in device registry: %s", device_name)

    # Store device info in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        CONF_MAC_ADDRESS: mac_address,
        "name": entry.title,
        "device_name": device_name,
        "device": em1003_device,
    }

    # Register services
    await async_setup_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for EM1003 debugging."""

    async def handle_scan_device(call: ServiceCall) -> None:
        """Handle the scan_device service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Starting BLE scan for device: %s ===", mac_address)

        try:
            # Use Home Assistant's Bluetooth integration
            scanner = bluetooth.async_get_scanner(hass)

            _LOGGER.info("Scanning for nearby BLE devices...")

            # Try to find the device
            device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)

            if device:
                _LOGGER.info("✓ Device found!")
                _LOGGER.info("  Name: %s", device.name)
                _LOGGER.info("  Address: %s", device.address)
                _LOGGER.info("  RSSI: %s", getattr(device, 'rssi', 'N/A'))
                _LOGGER.info("  Details: %s", device.details)
            else:
                _LOGGER.warning("✗ Device not found. Make sure it's powered on and nearby.")

                # Also try a general scan
                _LOGGER.info("Performing general BLE scan...")
                devices = await BleakScanner.discover(timeout=10.0)
                _LOGGER.info("Found %d BLE devices:", len(devices))
                for dev in devices:
                    _LOGGER.info("  - %s (%s) RSSI: %s", dev.name or "Unknown", dev.address, dev.rssi)

        except Exception as err:
            _LOGGER.error("Error scanning for device: %s", err, exc_info=True)

        _LOGGER.info("=== Scan complete ===")

    async def handle_discover_all(call: ServiceCall) -> None:
        """Handle the discover_all service call - discover all services and characteristics."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Starting full BLE discovery for device: %s ===", mac_address)

        try:
            device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)

            if not device:
                _LOGGER.error("Device not found: %s", mac_address)
                return

            client = await establish_connection(
                BleakClient,
                device,
                mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=3,  # Reduced to avoid overwhelming Bluetooth stack
                timeout=30.0,
            )

            try:
                _LOGGER.info("✓ Connected to device")

                # Get all services
                services = client.services

                _LOGGER.info("Found %d services:", len(services))

                for service in services:
                    _LOGGER.info("")
                    _LOGGER.info("Service: %s", service.uuid)
                    _LOGGER.info("  Description: %s", service.description)
                    _LOGGER.info("  Characteristics:")

                    for char in service.characteristics:
                        _LOGGER.info("    - UUID: %s", char.uuid)
                        _LOGGER.info("      Description: %s", char.description)
                        _LOGGER.info("      Properties: %s", char.properties)
                        _LOGGER.info("      Handle: %s", char.handle)

                        # Try to read if readable
                        if "read" in char.properties:
                            try:
                                value = await client.read_gatt_char(char.uuid)
                                _LOGGER.info("      Value (hex): %s", value.hex())
                                _LOGGER.info("      Value (bytes): %s", list(value))
                                try:
                                    _LOGGER.info("      Value (utf-8): %s", value.decode('utf-8'))
                                except:
                                    pass
                            except Exception as read_err:
                                _LOGGER.warning("      Could not read: %s", read_err)

                        # List descriptors
                        if char.descriptors:
                            _LOGGER.info("      Descriptors:")
                            for desc in char.descriptors:
                                _LOGGER.info("        - UUID: %s, Handle: %s", desc.uuid, desc.handle)
            finally:
                await client.disconnect()

        except BleakError as err:
            _LOGGER.error("Bleak error during discovery: %s", err, exc_info=True)
        except Exception as err:
            _LOGGER.error("Error during discovery: %s", err, exc_info=True)

        _LOGGER.info("=== Discovery complete ===")

    async def handle_list_services(call: ServiceCall) -> None:
        """Handle the list_services service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Listing services for device: %s ===", mac_address)

        try:
            device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)

            if not device:
                _LOGGER.error("Device not found: %s", mac_address)
                return

            client = await establish_connection(
                BleakClient,
                device,
                mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=3,  # Reduced to avoid overwhelming Bluetooth stack
                timeout=30.0,
            )

            try:
                _LOGGER.info("✓ Connected to device")

                services = client.services
                _LOGGER.info("Services available:")

                for service in services:
                    _LOGGER.info("  UUID: %s", service.uuid)
                    _LOGGER.info("  Description: %s", service.description)
                    _LOGGER.info("  Characteristics count: %d", len(service.characteristics))
            finally:
                await client.disconnect()

        except Exception as err:
            _LOGGER.error("Error listing services: %s", err, exc_info=True)

        _LOGGER.info("=== Service list complete ===")

    async def handle_read_characteristic(call: ServiceCall) -> None:
        """Handle the read_characteristic service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)
        char_uuid = call.data.get(ATTR_CHARACTERISTIC_UUID)

        _LOGGER.info("=== Reading characteristic %s from device: %s ===", char_uuid, mac_address)

        try:
            device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)

            if not device:
                _LOGGER.error("Device not found: %s", mac_address)
                return

            client = await establish_connection(
                BleakClient,
                device,
                mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=3,  # Reduced to avoid overwhelming Bluetooth stack
                timeout=30.0,
            )

            try:
                _LOGGER.info("✓ Connected to device")

                value = await client.read_gatt_char(char_uuid)

                _LOGGER.info("✓ Read successful!")
                _LOGGER.info("  Characteristic: %s", char_uuid)
                _LOGGER.info("  Value (hex): %s", value.hex())
                _LOGGER.info("  Value (bytes): %s", list(value))
                _LOGGER.info("  Length: %d bytes", len(value))

                try:
                    _LOGGER.info("  Value (utf-8): %s", value.decode('utf-8'))
                except:
                    pass
            finally:
                await client.disconnect()

        except Exception as err:
            _LOGGER.error("Error reading characteristic: %s", err, exc_info=True)

        _LOGGER.info("=== Read complete ===")

    async def handle_write_characteristic(call: ServiceCall) -> None:
        """Handle the write_characteristic service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)
        char_uuid = call.data.get(ATTR_CHARACTERISTIC_UUID)
        data = call.data.get(ATTR_DATA)

        _LOGGER.info("=== Writing to characteristic %s on device: %s ===", char_uuid, mac_address)
        _LOGGER.info("Data to write: %s", data)

        try:
            device = bluetooth.async_ble_device_from_address(hass, mac_address, connectable=True)

            if not device:
                _LOGGER.error("Device not found: %s", mac_address)
                return

            # Convert data string to bytes
            if isinstance(data, str):
                # Try to parse as hex
                if all(c in '0123456789abcdefABCDEF' for c in data.replace(' ', '')):
                    data_bytes = bytes.fromhex(data.replace(' ', ''))
                else:
                    # Treat as UTF-8 string
                    data_bytes = data.encode('utf-8')
            elif isinstance(data, list):
                data_bytes = bytes(data)
            else:
                data_bytes = data

            client = await establish_connection(
                BleakClient,
                device,
                mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=3,  # Reduced to avoid overwhelming Bluetooth stack
                timeout=30.0,
            )

            try:
                _LOGGER.info("✓ Connected to device")

                await client.write_gatt_char(char_uuid, data_bytes)

                _LOGGER.info("✓ Write successful!")
                _LOGGER.info("  Characteristic: %s", char_uuid)
                _LOGGER.info("  Data written (hex): %s", data_bytes.hex())
                _LOGGER.info("  Data written (bytes): %s", list(data_bytes))
            finally:
                await client.disconnect()

        except Exception as err:
            _LOGGER.error("Error writing characteristic: %s", err, exc_info=True)

        _LOGGER.info("=== Write complete ===")

    async def handle_read_device_name(call: ServiceCall) -> None:
        """Handle the read_device_name service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Reading device name from: %s ===", mac_address)

        device_name = await async_read_device_name(hass, mac_address)

        if device_name:
            _LOGGER.info("✓ Device name: %s", device_name)
        else:
            _LOGGER.warning("✗ Failed to read device name")

        _LOGGER.info("=== Read device name complete ===")

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_DEVICE,
        handle_scan_device,
        schema=vol.Schema({
            vol.Required(ATTR_MAC_ADDRESS): str,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER_ALL,
        handle_discover_all,
        schema=vol.Schema({
            vol.Required(ATTR_MAC_ADDRESS): str,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_SERVICES,
        handle_list_services,
        schema=vol.Schema({
            vol.Required(ATTR_MAC_ADDRESS): str,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_CHARACTERISTIC,
        handle_read_characteristic,
        schema=vol.Schema({
            vol.Required(ATTR_MAC_ADDRESS): str,
            vol.Required(ATTR_CHARACTERISTIC_UUID): str,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_CHARACTERISTIC,
        handle_write_characteristic,
        schema=vol.Schema({
            vol.Required(ATTR_MAC_ADDRESS): str,
            vol.Required(ATTR_CHARACTERISTIC_UUID): str,
            vol.Required(ATTR_DATA): vol.Any(str, list),
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_DEVICE_NAME,
        handle_read_device_name,
        schema=vol.Schema({
            vol.Required(ATTR_MAC_ADDRESS): str,
        }),
    )

    _LOGGER.info("EM1003 debugging services registered")
