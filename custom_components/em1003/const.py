"""Constants for the EM1003 BLE Sensor integration."""

DOMAIN = "em1003"
CONF_MAC_ADDRESS = "mac_address"

# Service names
SERVICE_SCAN_DEVICE = "scan_device"
SERVICE_READ_CHARACTERISTIC = "read_characteristic"
SERVICE_WRITE_CHARACTERISTIC = "write_characteristic"
SERVICE_LIST_SERVICES = "list_services"
SERVICE_DISCOVER_ALL = "discover_all"
SERVICE_READ_DEVICE_NAME = "read_device_name"

# Attribute names
ATTR_MAC_ADDRESS = "mac_address"
ATTR_SERVICE_UUID = "service_uuid"
ATTR_CHARACTERISTIC_UUID = "characteristic_uuid"
ATTR_DATA = "data"

# BLE UUIDs
# Standard BLE GATT Characteristics
DEVICE_NAME_UUID = "00002A00-0000-1000-8000-00805F9B34FB"  # Device Name characteristic

# EM1003 Custom Service and Characteristics
EM1003_SERVICE_UUID = "09de2880-1415-4e2c-a48a-3938e3288537"  # Unknown service for sensor data
EM1003_WRITE_CHAR_UUID = "0000FFF1-0000-1000-8000-00805F9B34FB"  # Write characteristic (0xFFF1)
EM1003_NOTIFY_CHAR_UUID = "0000FFF4-0000-1000-8000-00805F9B34FB"  # Notify characteristic (0xFFF4)

# Protocol Commands
CMD_READ_SENSOR = 0x06  # Command to read sensor data

# Sensor IDs (mapping to be confirmed through testing)
SENSOR_ID_01 = 0x01  # Unknown - possibly Formaldehyde (HCHO)
SENSOR_ID_06 = 0x06  # Unknown - possibly PM2.5
SENSOR_ID_08 = 0x08  # Unknown - possibly PM10
SENSOR_ID_09 = 0x09  # Unknown - possibly TVOC
SENSOR_ID_0A = 0x0A  # Unknown - possibly eCO2
SENSOR_ID_11 = 0x11  # Unknown - possibly Noise
SENSOR_ID_12 = 0x12  # Unknown - possibly Temperature
SENSOR_ID_13 = 0x13  # Unknown - possibly Humidity

# Sensor definitions
SENSOR_TYPES = {
    SENSOR_ID_01: {
        "id": SENSOR_ID_01,
        "name": "Sensor 01",
        "key": "sensor_01",
        "icon": "mdi:chemical-weapon",
        "device_class": None,
        "unit": None,  # TBD
        "note": "Possibly Formaldehyde (HCHO)",
    },
    SENSOR_ID_06: {
        "id": SENSOR_ID_06,
        "name": "Sensor 06",
        "key": "sensor_06",
        "icon": "mdi:air-filter",
        "device_class": None,
        "unit": "µg/m³",  # Assuming PM2.5
        "note": "Possibly PM2.5",
    },
    SENSOR_ID_08: {
        "id": SENSOR_ID_08,
        "name": "Sensor 08",
        "key": "sensor_08",
        "icon": "mdi:air-filter",
        "device_class": None,
        "unit": "µg/m³",  # Assuming PM10
        "note": "Possibly PM10",
    },
    SENSOR_ID_09: {
        "id": SENSOR_ID_09,
        "name": "Sensor 09",
        "key": "sensor_09",
        "icon": "mdi:molecule",
        "device_class": None,
        "unit": "ppb",  # Assuming TVOC
        "note": "Possibly TVOC",
    },
    SENSOR_ID_0A: {
        "id": SENSOR_ID_0A,
        "name": "Sensor 0A",
        "key": "sensor_0a",
        "icon": "mdi:molecule-co2",
        "device_class": None,
        "unit": "ppm",  # Assuming eCO2
        "note": "Possibly eCO2",
    },
    SENSOR_ID_11: {
        "id": SENSOR_ID_11,
        "name": "Sensor 11",
        "key": "sensor_11",
        "icon": "mdi:volume-high",
        "device_class": None,
        "unit": "dB",  # Assuming Noise
        "note": "Possibly Noise",
    },
    SENSOR_ID_12: {
        "id": SENSOR_ID_12,
        "name": "Sensor 12",
        "key": "sensor_12",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "unit": "°C",  # Assuming Temperature
        "note": "Possibly Temperature",
    },
    SENSOR_ID_13: {
        "id": SENSOR_ID_13,
        "name": "Sensor 13",
        "key": "sensor_13",
        "icon": "mdi:water-percent",
        "device_class": "humidity",
        "unit": "%",  # Assuming Humidity
        "note": "Possibly Humidity",
    },
}

# Default values
DEFAULT_NAME = "EM1003"
DEVICE_TIMEOUT = 30.0

# Version
VERSION = "0.0.1"
