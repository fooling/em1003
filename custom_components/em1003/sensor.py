"""Sensor platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_MAC_ADDRESS

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

    _LOGGER.info("Setting up EM1003 sensor for device: %s (%s)", device_name, mac_address)

    # Create sensor entities
    entities = [
        EM1003StatusSensor(config_entry, mac_address, device_name),
    ]

    async_add_entities(entities)


class EM1003StatusSensor(SensorEntity):
    """Representation of an EM1003 BLE sensor."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:bluetooth"

    def __init__(
        self,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
    ) -> None:
        """Initialize the sensor."""
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._attr_unique_id = f"{mac_address}_status"
        self._attr_name = "Status"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="EM1003",
            model="BLE Sensor",
            connections={("mac", self._mac_address)},
        )

    @property
    def native_value(self) -> str:
        """Return the state of the sensor."""
        return "Connected"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True
