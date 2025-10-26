"""Sensor platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, CONF_MAC_ADDRESS, SENSOR_TYPES, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EM1003 sensor based on a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    mac_address = data[CONF_MAC_ADDRESS]
    device_name = data.get("device_name", config_entry.title)
    em1003_device = data["device"]

    _LOGGER.info("Setting up EM1003 sensors for device: %s (%s)", device_name, mac_address)

    # Get scan interval from options or use default
    scan_interval_seconds = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    scan_interval = timedelta(seconds=scan_interval_seconds)
    _LOGGER.info("Using scan interval: %d seconds", scan_interval_seconds)

    # Create coordinator for updating sensor data
    coordinator = EM1003DataUpdateCoordinator(hass, em1003_device, mac_address, scan_interval)

    # Add a small delay before first refresh to allow device to be ready
    _LOGGER.debug("Waiting for device to be ready before initial refresh...")
    await asyncio.sleep(2.0)

    # Fetch initial data with better error handling
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "Initial data fetch failed for %s, sensors will retry automatically: %s",
            mac_address,
            err
        )
        # Don't fail setup - let sensors show unavailable and retry later

    # Create sensor entities for each sensor type
    entities = []
    for sensor_id, sensor_info in SENSOR_TYPES.items():
        entities.append(
            EM1003Sensor(
                coordinator,
                config_entry,
                mac_address,
                device_name,
                sensor_id,
                sensor_info,
            )
        )

    async_add_entities(entities)


class EM1003DataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching EM1003 data from the device."""

    def __init__(
        self,
        hass: HomeAssistant,
        em1003_device,
        mac_address: str,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"EM1003 {mac_address}",
            update_interval=update_interval,
        )
        self.em1003_device = em1003_device
        self.mac_address = mac_address

    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            _LOGGER.debug("Updating sensor data for %s", self.mac_address)
            data = await self.em1003_device.read_all_sensors()

            # Check if we actually received any valid data
            valid_count = sum(1 for v in data.values() if v is not None)
            if valid_count == 0:
                raise UpdateFailed(
                    f"Failed to read any sensor data from {self.mac_address} - "
                    "connection or device issue"
                )

            _LOGGER.debug("Sensor data updated: %s (valid: %d/%d)", data, valid_count, len(data))

            # Log if problematic sensors have no data
            if data:
                for sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                    value = data.get(sensor_id)
                    if value is None:
                        from .const import SENSOR_TYPES
                        sensor_info = SENSOR_TYPES.get(sensor_id, {})
                        sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                        _LOGGER.info("[%s] No data received", sensor_name)

            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err


class EM1003Sensor(CoordinatorEntity, SensorEntity):
    """Representation of an EM1003 BLE sensor."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: EM1003DataUpdateCoordinator,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        sensor_id: int,
        sensor_info: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._sensor_id = sensor_id
        self._sensor_info = sensor_info

        # Set entity attributes
        self._attr_unique_id = f"{mac_address}_{sensor_info['key']}"
        self._attr_name = sensor_info["name"]
        self._attr_icon = sensor_info["icon"]
        self._attr_native_unit_of_measurement = sensor_info.get("unit")

        # Set device class if available - use explicit mapping with getattr for compatibility
        device_class_str = sensor_info.get("device_class")
        if device_class_str:
            # Map string device classes to SensorDeviceClass enum
            # Use getattr to handle older HA versions that may not have all device classes
            device_class_map = {
                "temperature": getattr(SensorDeviceClass, "TEMPERATURE", None),
                "humidity": getattr(SensorDeviceClass, "HUMIDITY", None),
                "pm25": getattr(SensorDeviceClass, "PM25", None),
                "pm10": getattr(SensorDeviceClass, "PM10", None),
                "carbon_dioxide": getattr(SensorDeviceClass, "CARBON_DIOXIDE", None),
                "volatile_organic_compounds": getattr(SensorDeviceClass, "VOLATILE_ORGANIC_COMPOUNDS", None),
            }
            self._attr_device_class = device_class_map.get(device_class_str)
            if self._attr_device_class:
                _LOGGER.debug(
                    "Set device_class for sensor %s (0x%02x): %s",
                    sensor_info["name"],
                    sensor_id,
                    self._attr_device_class
                )
            elif device_class_str:
                _LOGGER.debug(
                    "Device class '%s' not available in this Home Assistant version for sensor %s (0x%02x)",
                    device_class_str,
                    sensor_info["name"],
                    sensor_id
                )

        # Set precision based on sensor type
        if sensor_id == 0x01:  # Temperature
            self._attr_suggested_display_precision = 2
        elif sensor_id == 0x06:  # Humidity
            self._attr_suggested_display_precision = 1
        elif sensor_id == 0x0A:  # Formaldehyde
            self._attr_suggested_display_precision = 3
        else:  # Other sensors (no decimals): PM2.5, PM10, Noise, TVOC, eCO2
            self._attr_suggested_display_precision = 0

        # Cache for last valid value and timestamp
        self._last_valid_value: float | None = None
        self._last_update_time: datetime | None = None
        self._stale_threshold = timedelta(minutes=20)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="EM1003",
            model="BLE Air Quality Sensor",
            connections={("mac", self._mac_address)},
        )

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Consider sensor available if:
        1. Coordinator has fresh data, OR
        2. We have cached data that's less than 20 minutes old
        """
        # Check if we have fresh data from coordinator
        if self.coordinator.last_update_success:
            if self.coordinator.data is not None:
                current_value = self.coordinator.data.get(self._sensor_id)
                if current_value is not None:
                    return True

        # Check if we have recent cached data
        if self._last_valid_value is not None and self._last_update_time is not None:
            time_since_update = datetime.now() - self._last_update_time
            if time_since_update < self._stale_threshold:
                # Cached data is fresh enough
                return True

        # No fresh or cached data available
        return False

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        # Get current value from coordinator
        current_value = None
        if self.coordinator.data is not None:
            current_value = self.coordinator.data.get(self._sensor_id)

        # If we have a valid new value, update cache
        if current_value is not None:
            self._last_valid_value = current_value
            self._last_update_time = datetime.now()

            _LOGGER.debug(
                "[VALUE] %s (0x%02x): Fresh value = %s",
                self._sensor_info["name"],
                self._sensor_id,
                current_value
            )
            return current_value

        # No new data available, check if we should use cached value
        if self._last_valid_value is not None and self._last_update_time is not None:
            time_since_update = datetime.now() - self._last_update_time

            # If less than 20 minutes, return cached value
            if time_since_update < self._stale_threshold:
                _LOGGER.debug(
                    "[VALUE] %s (0x%02x): Using cached value %s (age: %d seconds)",
                    self._sensor_info["name"],
                    self._sensor_id,
                    self._last_valid_value,
                    int(time_since_update.total_seconds())
                )
                return self._last_valid_value
            else:
                # Data is too old, mark as unavailable
                _LOGGER.warning(
                    "[VALUE] %s (0x%02x): Data stale for %d minutes, returning None",
                    self._sensor_info["name"],
                    self._sensor_id,
                    int(time_since_update.total_seconds() / 60)
                )

        # No valid data available
        _LOGGER.debug(
            "[VALUE] %s (0x%02x): No data available (coordinator_data=%s, cached=%s)",
            self._sensor_info["name"],
            self._sensor_id,
            self.coordinator.data is not None,
            self._last_valid_value is not None
        )
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        attrs = {
            "sensor_id": f"0x{self._sensor_id:02x}",
            "note": self._sensor_info.get("note", "Unknown sensor type"),
            "mac_address": self._mac_address,
        }

        # Add last update information if available
        if self._last_update_time is not None:
            attrs["last_update"] = self._last_update_time.isoformat()
            time_since_update = datetime.now() - self._last_update_time
            attrs["data_age_seconds"] = int(time_since_update.total_seconds())

        return attrs
