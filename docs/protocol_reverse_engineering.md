# EM1003 BLE Protocol Reverse Engineering

This document describes the Bluetooth Low Energy (BLE) communication protocol used by the EM1003 air quality sensor device.

## Device Information

- **Device Name**: Can be read from standard BLE Device Name characteristic
  - Example: `3GCG300ZY4`
- **Device Name UUID**: `00002A00-0000-1000-8000-00805F9B34FB` (Standard BLE GATT)

## BLE Service Structure

### Unknown Service (Data Communication)
- **Service UUID**: `09de2880-1415-4e2c-a48a-3938e3288537`
- **Position**: Third service block when connected via BLE
- **Purpose**: Sensor data communication

#### Characteristics

1. **Write Characteristic** (Command)
   - **UUID**: `0000FFF1-0000-1000-8000-00805F9B34FB` (Short: `0xFFF1`)
   - **Properties**: WRITE
   - **Purpose**: Send commands to request sensor data

2. **Notify Characteristic** (Response)
   - **UUID**: `0000FFF4-0000-1000-8000-00805F9B34FB` (Short: `0xFFF4`)
   - **Properties**: NOTIFY
   - **Purpose**: Receive sensor data responses

## Communication Protocol

### Request Format

The device uses a simple request-response protocol. To read sensor data, write a 3-byte command to the write characteristic (`0xFFF1`):

```
[Sequence ID] [Command Type] [Sensor ID]
```

**Example Request**: `AC 06 08`

- **Byte 0 - Sequence ID** (`0xAC`): Unique identifier for this request
  - Used to match responses to requests
  - Can be any value (e.g., `0xAC`, `0xAB`, `0x01`, etc.)
  - The response will echo back this same sequence ID

- **Byte 1 - Command Type** (`0x06`): Command identifier
  - `0x06`: Read sensor data (confirmed)
  - Other commands may exist but are not yet documented

- **Byte 2 - Sensor ID** (`0x08`): Which sensor to read
  - See sensor ID table below

### Response Format

The device responds via the notify characteristic (`0xFFF4`) with a variable-length message:

```
[Sequence ID] [Command Type] [Sensor ID] [Value Bytes...]
```

**Example Response**: `AC 06 08 33 00`

- **Byte 0 - Sequence ID** (`0xAC`): Matches the request sequence ID
- **Byte 1 - Command Type** (`0x06`): Echoes the command type
- **Byte 2 - Sensor ID** (`0x08`): Echoes the sensor ID
- **Bytes 3+ - Value** (`33 00`): Sensor reading (format varies by sensor type)

### Sensor ID Mapping

The following sensor IDs have been observed:

| Sensor ID | Likely Sensor Type | Notes |
|-----------|-------------------|-------|
| `0x01` | Unknown | Possibly Formaldehyde (HCHO) |
| `0x06` | Unknown | Possibly PM2.5 |
| `0x08` | Unknown | Possibly PM10 |
| `0x09` | Unknown | Possibly TVOC |
| `0x0A` | Unknown | Possibly eCO2 |
| `0x11` | Unknown | Possibly Noise |
| `0x12` | Unknown | Possibly Temperature |
| `0x13` | Unknown | Possibly Humidity |

**Possible sensor types** (order not yet confirmed):
- Formaldehyde (HCHO)
- PM2.5 (Particulate Matter 2.5µm)
- PM10 (Particulate Matter 10µm)
- TVOC (Total Volatile Organic Compounds)
- eCO2 (Equivalent CO2)
- Noise Level
- Temperature
- Humidity

## Data Parsing

### Value Format (CONFIRMED)

The value bytes in the response are **2 bytes in Big Endian format**.

**Example**: Response `AC 06 08 33 00`
- Sensor ID: `0x08`
- Value bytes: `33 00`
- Interpretation: `0x3300` = `00 33` (Big Endian) = **51 decimal**

**Format**: 16-bit unsigned integer, Big Endian

**Analysis needed**:
- Determine which sensor ID corresponds to which physical sensor
- Collect multiple readings with known reference values
- Compare with other air quality sensors
- Test different environmental conditions
- Check for scaling factors (e.g., divide by 10, 100, etc.)

### Recommended Testing Approach

1. **Temperature/Humidity** (if available): Easiest to verify
   - Compare with known thermometer/hygrometer
   - Common formats:
     - Temperature: Usually in 0.1°C units (e.g., 235 = 23.5°C)
     - Humidity: Usually in 0.1% or 1% units

2. **PM2.5/PM10**:
   - Usually in µg/m³
   - Typical indoor range: 0-100 µg/m³
   - Compare with reference PM sensor

3. **TVOC/eCO2**:
   - TVOC: Usually in ppb (parts per billion)
   - eCO2: Usually in ppm (parts per million)

4. **Formaldehyde**:
   - Usually in µg/m³ or mg/m³
   - Typical indoor range: 0-100 µg/m³

## Implementation Status

### Confirmed
- ✅ Device name reading via standard BLE characteristic
- ✅ Service UUID: `09de2880-1415-4e2c-a48a-3938e3288537`
- ✅ Write characteristic: `0xFFF1`
- ✅ Notify characteristic: `0xFFF4`
- ✅ Request format: 3 bytes `[seq][06][sensor_id]`
- ✅ Response format: `[seq][06][sensor_id][value_bytes]`
- ✅ Sequence ID matching works
- ✅ Value format: 2 bytes, Big Endian, unsigned 16-bit integer
- ✅ Example: `33 00` = 51 decimal

### To Be Determined
- ❓ Exact mapping of sensor IDs to sensor types
- ❓ Scaling factors and units for each sensor
- ❓ Whether all sensors use 2-byte values or if some use different lengths
- ❓ Other command types besides `0x06`
- ❓ Error handling and timeout behavior

## Example Communication Session

```
1. Connect to device via BLE
2. Subscribe to notifications on 0xFFF4
3. Write to 0xFFF1: [0x01, 0x06, 0x08]
4. Receive from 0xFFF4: [0x01, 0x06, 0x08, 0x33, 0x00]
5. Parse value: 0x3300 or 0x0033 (TBD)
6. Repeat for other sensor IDs
```

## Next Steps

1. Implement basic read functionality for all sensor IDs
2. Log raw values for analysis
3. Test with known environmental conditions
4. Determine correct parsing for each sensor type
5. Add proper units and scaling
6. Implement periodic polling or continuous monitoring

## Notes

- The device may require time between readings
- Some sensors may need warm-up time after power-on
- Check if multiple sensors can be read in sequence without delay
- Consider implementing retry logic for failed reads
- May need to handle notifications that arrive out of order

---

**Document Version**: 1.0
**Last Updated**: 2025-10-25
**Status**: Initial reverse engineering in progress
