"""Switch platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up EM1003 switch based on a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    mac_address = data[CONF_MAC_ADDRESS]
    device_name = data.get("device_name", config_entry.title)
    em1003_device = data["device"]
    coordinator = data.get("coordinator")

    _LOGGER.info("Setting up EM1003 buzzer switch for device: %s (%s)", device_name, mac_address)

    # Create buzzer switch entity
    async_add_entities([
        EM1003BuzzerSwitch(
            config_entry,
            mac_address,
            device_name,
            em1003_device,
            coordinator,
        )
    ])


class EM1003BuzzerSwitch(SwitchEntity):
    """Representation of an EM1003 buzzer switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
        coordinator=None,
    ) -> None:
        """Initialize the buzzer switch."""
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device
        self._coordinator = coordinator

        # Set entity attributes
        self._attr_unique_id = f"{mac_address}_buzzer"
        self._attr_name = "Buzzer"
        self._attr_icon = "mdi:volume-high"

        # State tracking
        self._attr_is_on = None
        self._attr_available = True

        # Subscribe to coordinator updates if available
        if self._coordinator:
            self._coordinator_listener = None

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

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Subscribe to coordinator updates
        if self._coordinator:
            self._coordinator_listener = self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )

        # Try to read initial buzzer state
        try:
            _LOGGER.debug("Reading initial buzzer state for %s", self._mac_address)
            state = await self._em1003_device.read_buzzer_state()
            if state is not None:
                self._attr_is_on = state
                _LOGGER.info(
                    "Initial buzzer state for %s: %s",
                    self._mac_address,
                    "ON" if state else "OFF"
                )
            else:
                _LOGGER.warning(
                    "Could not read initial buzzer state for %s, will retry on first interaction",
                    self._mac_address
                )
                self._attr_is_on = None
        except Exception as err:
            _LOGGER.warning(
                "Error reading initial buzzer state for %s: %s",
                self._mac_address,
                err
            )
            self._attr_is_on = None

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self._coordinator_listener:
            self._coordinator_listener()
            self._coordinator_listener = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Sync state from device object (updated by coordinator's buzzer query)
        if self._em1003_device.buzzer_state is not None:
            if self._attr_is_on != self._em1003_device.buzzer_state:
                _LOGGER.debug(
                    "Syncing buzzer state from coordinator for %s: %s -> %s",
                    self._mac_address,
                    "ON" if self._attr_is_on else "OFF" if self._attr_is_on is not None else "Unknown",
                    "ON" if self._em1003_device.buzzer_state else "OFF"
                )
                self._attr_is_on = self._em1003_device.buzzer_state
                self._attr_available = True
                self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the buzzer on."""
        _LOGGER.debug("Turning on buzzer for %s", self._mac_address)

        try:
            success = await self._em1003_device.set_buzzer_state(True)

            if success:
                self._attr_is_on = True
                self._attr_available = True
                _LOGGER.info("Successfully turned on buzzer for %s", self._mac_address)
            else:
                _LOGGER.error("Failed to turn on buzzer for %s", self._mac_address)
                self._attr_available = False

            # Trigger state update
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Error turning on buzzer for %s: %s", self._mac_address, err)
            self._attr_available = False
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the buzzer off."""
        _LOGGER.debug("Turning off buzzer for %s", self._mac_address)

        try:
            success = await self._em1003_device.set_buzzer_state(False)

            if success:
                self._attr_is_on = False
                self._attr_available = True
                _LOGGER.info("Successfully turned off buzzer for %s", self._mac_address)
            else:
                _LOGGER.error("Failed to turn off buzzer for %s", self._mac_address)
                self._attr_available = False

            # Trigger state update
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Error turning off buzzer for %s: %s", self._mac_address, err)
            self._attr_available = False
            self.async_write_ha_state()
            raise

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        return {
            "mac_address": self._mac_address,
        }
