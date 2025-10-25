"""Config flow for EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_MAC
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_MAC_ADDRESS, DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)


class EM1003ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EM1003 BLE Sensor."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle bluetooth discovery."""
        _LOGGER.debug("Discovered BLE device: %s", discovery_info)
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_devices[discovery_info.address] = discovery_info

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        if user_input is not None:
            return self.async_create_entry(
                title=DEFAULT_NAME,
                data={CONF_MAC_ADDRESS: self.unique_id},
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": DEFAULT_NAME},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mac_address = user_input[CONF_MAC_ADDRESS].upper().replace("-", ":")

            # Set unique ID to prevent duplicate entries
            await self.async_set_unique_id(mac_address)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"{DEFAULT_NAME} ({mac_address})",
                data={CONF_MAC_ADDRESS: mac_address},
            )

        # Get list of discovered Bluetooth devices
        discovered_devices = async_discovered_service_info(self.hass)

        # Build dropdown options from discovered devices
        device_options = {}
        if discovered_devices:
            for device in discovered_devices:
                # Create a friendly display name with device name and MAC address
                device_name = device.name or "Unknown Device"
                mac_address = device.address
                display_name = f"{device_name} ({mac_address})"
                device_options[mac_address] = display_name

        # Create schema with dropdown if devices found, otherwise text input
        if device_options:
            schema = vol.Schema(
                {
                    vol.Required(CONF_MAC_ADDRESS): vol.In(device_options),
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_MAC_ADDRESS): str,
                }
            )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "discovered": str(len(discovered_devices)) if discovered_devices else "0"
            },
        )
