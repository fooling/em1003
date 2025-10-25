"""Sensor platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

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

from .const import DOMAIN, CONF_MAC_ADDRESS, SENSOR_TYPES

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)  # Poll every 30 seconds


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

    # Create coordinator for updating sensor data
    coordinator = EM1003DataUpdateCoordinator(hass, em1003_device, mac_address)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

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
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"EM1003 {mac_address}",
            update_interval=SCAN_INTERVAL,
        )
        self.em1003_device = em1003_device
        self.mac_address = mac_address

    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            _LOGGER.debug("Updating sensor data for %s", self.mac_address)
            data = await self.em1003_device.read_all_sensors()
            _LOGGER.debug("Sensor data updated: %s", data)
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

        # Set device class if available
        if sensor_info.get("device_class"):
            self._attr_device_class = getattr(
                SensorDeviceClass, sensor_info["device_class"].upper(), None
            )

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
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._sensor_id)

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        return {
            "sensor_id": f"0x{self._sensor_id:02x}",
            "note": self._sensor_info.get("note", "Unknown sensor type"),
            "mac_address": self._mac_address,
        }
