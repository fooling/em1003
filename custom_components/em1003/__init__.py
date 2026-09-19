"""The EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
import voluptuous as vol
from datetime import timedelta

from . import const
from .const import (
    DOMAIN,
    CONF_MAC_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    SERVICE_SCAN_DEVICE,
    SERVICE_READ_CHARACTERISTIC,
    SERVICE_WRITE_CHARACTERISTIC,
    SERVICE_LIST_SERVICES,
    SERVICE_DISCOVER_ALL,
    SERVICE_READ_DEVICE_NAME,
    ATTR_MAC_ADDRESS,
    ATTR_SERVICE_UUID,  # noqa: F401  (kept for anything importing it from here)
    ATTR_CHARACTERISTIC_UUID,
    ATTR_DATA,
)
from .coordinator import EM1003DataUpdateCoordinator
from .device import EM1003Device

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


def _async_device_for_mac(hass: HomeAssistant, mac_address: str) -> EM1003Device:
    """Return the configured entry's device for this MAC, else a transient one.

    Returning the configured entry's own ``EM1003Device`` means a debug
    service call serialises (via that device's lock/breaker) with any poll
    already in flight for the same physical device. An unconfigured MAC gets
    its own throwaway device with its own lock, so it never disturbs a
    configured device's state.
    """
    for entry_data in hass.data.get(DOMAIN, {}).values():
        device: EM1003Device = entry_data["device"]
        if device.mac_address.upper() == mac_address.upper():
            return device
    return EM1003Device(hass, mac_address)


async def async_read_device_name(hass: HomeAssistant, mac_address: str) -> str | None:
    """Read the device name from a BLE device, through device.py's session machinery.

    Kept as a module-level function (imported lazily by config_flow.py) for
    backward compatibility with the pre-refactor import path.
    """
    return await _async_device_for_mac(hass, mac_address).async_read_device_name()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EM1003 BLE Sensor from a config entry."""
    mac_address = entry.data[CONF_MAC_ADDRESS]

    _LOGGER.info("Setting up EM1003 device with MAC: %s", mac_address)

    device = EM1003Device(hass, mac_address)

    # Device name resolution: a cached name costs zero BLE connections; an
    # uncached name is attempted once (max_attempts=1, inside
    # async_read_device_name) and cached additively on success. A failed
    # read falls back to the entry title without writing entry.data, so the
    # next start retries.
    cached_name = entry.data.get(CONF_DEVICE_NAME)
    if cached_name:
        name = cached_name
        _LOGGER.debug("Using cached device name for %s: %s", mac_address, name)
    else:
        name = await device.async_read_device_name()
        if name:
            _LOGGER.info("Read device name from %s: %s", mac_address, name)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICE_NAME: name}
            )
        else:
            _LOGGER.warning(
                "Could not read device name for %s, using entry title %s",
                mac_address,
                entry.title,
            )
            name = entry.title

    device.device_name = name

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, mac_address)},
        connections={("mac", mac_address)},
        name=name,
        manufacturer="EM1003",
        model="BLE Air Quality Sensor",
    )
    _LOGGER.info("Device registered in device registry: %s", name)

    interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    coordinator = EM1003DataUpdateCoordinator(hass, entry, device, interval)

    # Written here, before the platforms are forwarded, so sensor.py /
    # switch.py can rely on it already being present.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        CONF_MAC_ADDRESS: mac_address,
        "name": entry.title,
        "device_name": name,
        "device": device,
        "coordinator": coordinator,
    }

    # A failed first poll must never abort setup (BC-16): async_refresh (not
    # async_config_entry_first_refresh) just records last_update_success and
    # leaves entities to show unavailable / retry on the next interval.
    await coordinator.async_refresh()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (e.g. a new scan interval)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if data is not None:
            await data["coordinator"].async_shutdown()
            # Closes any lingering link; a safe no-op when the device is idle.
            #
            # Bounded, because `disconnect()` waits on the device lock: a poll
            # that is still in flight (device out of range) holds it for its
            # whole session budget, and an options-triggered reload would then
            # leave the 9 entities gone for that long. Giving up the wait is
            # safe - the coordinator is already shut down, so no new poll
            # starts, and the in-flight session always tears its own link down.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(const.BLE_UNLOAD_DISCONNECT_TIMEOUT):
                    await data["device"].disconnect()

        # The 6 debug services are registered once (guarded by `has_service`)
        # and deliberately never removed: v1.0.3 kept them for the life of HA,
        # and a single-entry install is unloaded in the middle of every
        # options-triggered reload, so removing them here would make an
        # automation calling `em1003.scan_device` fail with `ServiceNotFound`.
        # Their handlers resolve a device by MAC and work with no entry loaded.
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)

    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the debug services once, regardless of how many entries exist."""
    if hass.services.has_service(DOMAIN, SERVICE_SCAN_DEVICE):
        return

    async def handle_scan_device(call: ServiceCall) -> None:
        """Handle the scan_device service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Starting BLE scan for device: %s ===", mac_address)

        try:
            ble_device = bluetooth.async_ble_device_from_address(
                hass, mac_address, connectable=True
            )

            if ble_device:
                service_info = bluetooth.async_last_service_info(
                    hass, mac_address, connectable=True
                )
                rssi = service_info.rssi if service_info is not None else "N/A"
                _LOGGER.info("✓ Device found!")
                _LOGGER.info("  Name: %s", ble_device.name)
                _LOGGER.info("  Address: %s", ble_device.address)
                _LOGGER.info("  RSSI: %s", rssi)
            else:
                _LOGGER.warning("✗ Device not found. Make sure it's powered on and nearby.")
                _LOGGER.info("Listing nearby discovered BLE devices...")
                discovered = list(bluetooth.async_discovered_service_info(hass))
                for info in discovered:
                    _LOGGER.info("  - %s (%s) RSSI: %s", info.name or "Unknown", info.address, info.rssi)
                _LOGGER.info("Found %d BLE devices", len(discovered))

        except Exception as err:  # noqa: BLE001 - a service call must never raise
            _LOGGER.error("Error scanning for device: %s", err, exc_info=True)

        _LOGGER.info("=== Scan complete ===")

    async def handle_discover_all(call: ServiceCall) -> None:
        """Handle the discover_all service call - discover all services and characteristics."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Starting full BLE discovery for device: %s ===", mac_address)

        device = _async_device_for_mac(hass, mac_address)

        async def _discover(client):
            # Real bleak hands back a BleakGATTServiceCollection: iterable and
            # indexable, but with no __len__ - materialise it before counting.
            services = list(client.services)
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

                    if "read" in char.properties:
                        try:
                            async with asyncio.timeout(const.BLE_READ_CHAR_TIMEOUT):
                                value = await client.read_gatt_char(char.uuid)
                            _LOGGER.info("      Value (hex): %s", value.hex())
                            _LOGGER.info("      Value (bytes): %s", list(value))
                            try:
                                _LOGGER.info("      Value (utf-8): %s", value.decode("utf-8"))
                            except UnicodeDecodeError:
                                pass
                        except Exception as read_err:  # noqa: BLE001
                            _LOGGER.warning("      Could not read: %s", read_err)

                    if char.descriptors:
                        _LOGGER.info("      Descriptors:")
                        for desc in char.descriptors:
                            _LOGGER.info("        - UUID: %s, Handle: %s", desc.uuid, desc.handle)

        try:
            await device.async_with_client(_discover)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error during discovery: %s", err, exc_info=True)

        _LOGGER.info("=== Discovery complete ===")

    async def handle_list_services(call: ServiceCall) -> None:
        """Handle the list_services service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Listing services for device: %s ===", mac_address)

        device = _async_device_for_mac(hass, mac_address)

        async def _list(client):
            services = list(client.services)
            _LOGGER.info("Services available:")
            for service in services:
                _LOGGER.info("  UUID: %s", service.uuid)
                _LOGGER.info("  Description: %s", service.description)
                _LOGGER.info("  Characteristics count: %d", len(service.characteristics))

        try:
            await device.async_with_client(_list)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error listing services: %s", err, exc_info=True)

        _LOGGER.info("=== Service list complete ===")

    async def handle_read_characteristic(call: ServiceCall) -> None:
        """Handle the read_characteristic service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)
        char_uuid = call.data.get(ATTR_CHARACTERISTIC_UUID)

        _LOGGER.info("=== Reading characteristic %s from device: %s ===", char_uuid, mac_address)

        device = _async_device_for_mac(hass, mac_address)

        async def _read(client):
            async with asyncio.timeout(const.BLE_READ_CHAR_TIMEOUT):
                value = await client.read_gatt_char(char_uuid)

            _LOGGER.info("✓ Read successful!")
            _LOGGER.info("  Characteristic: %s", char_uuid)
            _LOGGER.info("  Value (hex): %s", value.hex())
            _LOGGER.info("  Value (bytes): %s", list(value))
            _LOGGER.info("  Length: %d bytes", len(value))
            try:
                _LOGGER.info("  Value (utf-8): %s", value.decode("utf-8"))
            except UnicodeDecodeError:
                pass

        try:
            await device.async_with_client(_read)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error reading characteristic: %s", err, exc_info=True)

        _LOGGER.info("=== Read complete ===")

    async def handle_write_characteristic(call: ServiceCall) -> None:
        """Handle the write_characteristic service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)
        char_uuid = call.data.get(ATTR_CHARACTERISTIC_UUID)
        data = call.data.get(ATTR_DATA)

        _LOGGER.info("=== Writing to characteristic %s on device: %s ===", char_uuid, mac_address)
        _LOGGER.info("Data to write: %s", data)

        # Parsed before connecting, so a bad value never opens a link.
        try:
            if isinstance(data, str):
                hex_candidate = data.replace(" ", "")
                if hex_candidate and all(c in "0123456789abcdefABCDEF" for c in hex_candidate):
                    data_bytes = bytes.fromhex(hex_candidate)
                else:
                    data_bytes = data.encode("utf-8")
            elif isinstance(data, list):
                data_bytes = bytes(data)
            else:
                data_bytes = bytes(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Could not parse data to write (%r): %s", data, err)
            return

        device = _async_device_for_mac(hass, mac_address)

        async def _write(client):
            async with asyncio.timeout(const.BLE_WRITE_TIMEOUT):
                await client.write_gatt_char(char_uuid, data_bytes)
            _LOGGER.info("✓ Write successful!")
            _LOGGER.info("  Characteristic: %s", char_uuid)
            _LOGGER.info("  Data written (hex): %s", data_bytes.hex())
            _LOGGER.info("  Data written (bytes): %s", list(data_bytes))

        try:
            await device.async_with_client(_write)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error writing characteristic: %s", err, exc_info=True)

        _LOGGER.info("=== Write complete ===")

    async def handle_read_device_name(call: ServiceCall) -> None:
        """Handle the read_device_name service call."""
        mac_address = call.data.get(ATTR_MAC_ADDRESS)

        _LOGGER.info("=== Reading device name from: %s ===", mac_address)

        try:
            device_name = await async_read_device_name(hass, mac_address)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error reading device name: %s", err, exc_info=True)
            device_name = None

        if device_name:
            _LOGGER.info("✓ Device name: %s", device_name)
        else:
            _LOGGER.warning("✗ Failed to read device name")

        _LOGGER.info("=== Read device name complete ===")

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
