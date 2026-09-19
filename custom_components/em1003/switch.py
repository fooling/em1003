"""Switch platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_MAC_ADDRESS
from .coordinator import EM1003DataUpdateCoordinator
from .device import EM1003Device

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
    coordinator = data["coordinator"]

    _LOGGER.info("Setting up EM1003 buzzer switch for device: %s (%s)", device_name, mac_address)

    async_add_entities([
        EM1003BuzzerSwitch(
            coordinator,
            config_entry,
            mac_address,
            device_name,
            em1003_device,
        )
    ])


class EM1003BuzzerSwitch(CoordinatorEntity[EM1003DataUpdateCoordinator], SwitchEntity):
    """Representation of an EM1003 buzzer switch.

    Never opens a BLE link of its own: it only reads/writes through the
    coordinator's shared ``EM1003Device`` (and thus its shared lock/session),
    so it can never race the coordinator's own poll.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EM1003DataUpdateCoordinator,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        device: EM1003Device,
    ) -> None:
        """Initialize the buzzer switch."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._device = device

        # Set entity attributes
        self._attr_unique_id = f"{mac_address}_buzzer"
        self._attr_name = "Buzzer"
        self._attr_icon = "mdi:volume-high"

        # State tracking: unknown until the first successful poll.
        self._attr_is_on = device.buzzer_state
        self._attr_available = True

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

        Deliberately does not follow ``coordinator.last_update_success``: it
        is set False only by a failed set command and restored True by the
        next coordinator update that carries a known buzzer state.
        """
        return self._attr_available

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass.

        Opens no BLE link: the initial state is whatever the device object
        already knows (usually ``None`` -> HA state ``unknown``) until the
        first coordinator poll completes.
        """
        await super().async_added_to_hass()
        self._attr_is_on = self._device.buzzer_state

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.buzzer_state is not None:
            self._attr_is_on = self.coordinator.buzzer_state
            self._attr_available = True
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the buzzer on."""
        await self._async_set_buzzer(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the buzzer off."""
        await self._async_set_buzzer(False)

    async def _async_set_buzzer(self, turn_on: bool) -> None:
        """Set the buzzer state through the shared device session."""
        ok = await self._device.set_buzzer_state(turn_on)

        if ok:
            self._attr_is_on = turn_on
            self._attr_available = True
            self.async_write_ha_state()
        else:
            self._attr_available = False
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Failed to turn {'on' if turn_on else 'off'} EM1003 buzzer {self._mac_address}"
            )

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        return {
            "mac_address": self._mac_address,
        }
