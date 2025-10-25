# EM1003 BLE Sensor Integration

## Status: Development/Debugging Version 0.0.1

This integration is currently in the **initial development stage** designed for **protocol reverse engineering**.

### What This Version Does:

- Provides BLE scanning and discovery services
- Allows reading and writing BLE characteristics
- Outputs comprehensive logs for protocol analysis
- Supports multiple device configurations

### What This Version Does NOT Do:

- Does not provide sensor entities (temperature, humidity, etc.)
- Does not automatically decode sensor data
- Does not have a finalized protocol implementation

### For Users:

If you have an EM1003 (720环境宝3) device and want to help with development:

1. Install this integration
2. Add your device using its MAC address
3. Run the `discover_all` service
4. Share the logs with the project

### For Developers:

This integration provides debugging services to help understand the BLE protocol:

- `scan_device` - Find and display device information
- `discover_all` - Complete GATT service/characteristic dump
- `list_services` - List available services
- `read_characteristic` - Read specific characteristics
- `write_characteristic` - Write to characteristics

All operations output detailed logs for analysis.

## Roadmap

- **v0.0.1** (Current): BLE debugging and protocol exploration
- **v0.1.0** (Planned): Basic sensor data decoding
- **v1.0.0** (Future): Full sensor support with automatic updates

## Contributing

Help us decode the EM1003 protocol! Share your findings through issues or pull requests.
