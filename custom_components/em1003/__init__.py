"""The EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
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
    DEVICE_NAME_UUID,
)
from .device import EM1003Device

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


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


# EM1003Device class has been moved to device.py for better code organization


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
