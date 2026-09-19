"""DataUpdateCoordinator for the EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .device import EM1003Device, EM1003PollResult

_LOGGER = logging.getLogger(__name__)


class EM1003DataUpdateCoordinator(DataUpdateCoordinator[dict[int, float | int | None]]):
    """Coordinate a single BLE session per interval that reads sensors and buzzer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: EM1003Device,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"EM1003 {device.mac_address}",
            update_interval=update_interval,
        )
        self.device = device
        # Kept for backward compatibility with anything referencing the old
        # sensor-module coordinator's attribute name.
        self.em1003_device = device
        self.mac_address = device.mac_address
        self.buzzer_state: bool | None = None
        self.last_poll: EM1003PollResult | None = None

    async def _async_update_data(self) -> dict[int, float | int | None]:
        """Fetch new data from the device using a single BLE session.

        ``EM1003Device.async_poll`` never raises: exactly one ``UpdateFailed``
        path below is the only way this coordinator reports a failure.
        """
        result = await self.device.async_poll()
        self.last_poll = result
        if result.buzzer is not None:
            # A failed buzzer query inside an otherwise-good poll must never
            # wipe out the last known buzzer state.
            self.buzzer_state = result.buzzer
        if not result.ok:
            raise UpdateFailed(result.error or f"no sensor data from {self.mac_address}")
        return result.sensors
