"""Constants for the EM1003 BLE Sensor integration."""

DOMAIN = "em1003"
CONF_MAC_ADDRESS = "mac_address"

# Service names
SERVICE_SCAN_DEVICE = "scan_device"
SERVICE_READ_CHARACTERISTIC = "read_characteristic"
SERVICE_WRITE_CHARACTERISTIC = "write_characteristic"
SERVICE_LIST_SERVICES = "list_services"
SERVICE_DISCOVER_ALL = "discover_all"

# Attribute names
ATTR_MAC_ADDRESS = "mac_address"
ATTR_SERVICE_UUID = "service_uuid"
ATTR_CHARACTERISTIC_UUID = "characteristic_uuid"
ATTR_DATA = "data"

# Default values
DEFAULT_NAME = "EM1003"
DEVICE_TIMEOUT = 30.0

# Version
VERSION = "0.0.1"
