# EM1003 BLE Protocol Reverse Engineering

This document describes the Bluetooth Low Energy (BLE) communication protocol used by the EM1003 air quality sensor device.

## Quick Reference

**Communication Protocol**:
- **Service**: `09de2880-1415-4e2c-a48a-3938e3288537`
- **Write Char**: `0xFFF1` - Send commands
- **Notify Char**: `0xFFF4` - Receive responses

**Request Format** (3 bytes):
```
[Sequence ID] [0x06] [Sensor ID]
Example: AC 06 08
```

**Response Format** (5 bytes):
```
[Sequence ID] [0x06] [Sensor ID] [Value High] [Value Low]
Example: AC 06 08 33 00
```

**Value Parsing** (Little Endian):
```
Value = (Low Byte) + (High Byte × 256)
Example: 33 00 → 0x33 + (0x00 × 256) = 51 decimal
```

**8 Sensor IDs**: `0x01`, `0x06`, `0x08` (Noise), `0x09` (PM2.5), `0x0A`, `0x11` (PM10), `0x12` (TVOC), `0x13` (eCO2)

---

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

The device responds via the notify characteristic (`0xFFF4`) with a 5-byte message:

```
[Sequence ID] [Command Type] [Sensor ID] [Value High Byte] [Value Low Byte]
```

**Example Response**: `AC 06 08 33 00`

- **Byte 0 - Sequence ID** (`0xAC`): Matches the request sequence ID
- **Byte 1 - Command Type** (`0x06`): Echoes the command type
- **Byte 2 - Sensor ID** (`0x08`): Echoes the sensor ID
- **Bytes 3-4 - Value** (`33 00`): Sensor reading as 16-bit Big Endian unsigned integer
  - High byte first, then low byte
  - Example: `33 00` = `0x3300` = 13056 in hex notation, but interpreted as Big Endian = `0x0033` = **51 decimal**

### Sensor ID Mapping

The following sensor IDs have been observed:

| Sensor ID | Sensor Type | Unit | Notes |
|-----------|------------|------|-------|
| `0x01` | Unknown | TBD | Not yet identified |
| `0x06` | Unknown | TBD | Not yet identified |
| `0x08` | Noise Level | dB | ✓ Confirmed: 31-00 = 49 dB |
| `0x09` | PM2.5 | µg/m³ | ✓ Confirmed: 1C-00 = 28 µg/m³ |
| `0x0A` | Unknown | TBD | Not yet identified |
| `0x11` | PM10 | µg/m³ | ✓ Confirmed |
| `0x12` | TVOC | mg/m³ | ✓ Confirmed: 01-00 = 0.001 (special encoding) |
| `0x13` | eCO2 | ppm | ✓ Confirmed: 9B-01 = 411 ppm |

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

### Value Format (CONFIRMED: Little Endian)

The value bytes in the response are **2 bytes in Little Endian format**.

#### Little Endian Byte Order Explanation

In Little Endian format, the **least significant byte** (LSB, low byte) comes first, followed by the **most significant byte** (MSB, high byte). This is the common format used in Intel x86 processors.

**Why Little Endian?**
Many embedded systems and IoT devices use Little Endian format because it allows for easy byte-by-byte reading and incremental value building.

**Comparison Table**:

| Byte Order | Byte Sequence | Interpretation | Decimal Value |
|------------|---------------|----------------|---------------|
| Little Endian (EM1003) | `33 00` | `0x33 + (0x00 × 256)` | **51** |
| Little Endian (EM1003) | `9B 01` | `0x9B + (0x01 × 256)` | **411** |
| Big Endian | `33 00` | `(0x33 × 256) + 0x00` | 13,056 |

**IMPORTANT**: The EM1003 device uses **Little Endian**, so `33 00` = **51**, and `9B 01` = **411**!

#### Parsing Examples

**Example 1**: Response `AC 06 08 31 00` (Noise sensor)
- Sensor ID: `0x08` = Noise
- Value bytes: `31 00` (Little Endian)
- Byte breakdown:
  - Byte 3 (LSB/Low): `0x31` = 49 decimal
  - Byte 4 (MSB/High): `0x00` = 0 decimal
- **Calculation**: `0x31 + (0x00 × 256) = 49 + 0` = **49 dB**
- **✓ Confirmed real-world value**

**Example 2**: Response `AC 06 09 1C 00` (PM2.5 sensor)
- Sensor ID: `0x09` = PM2.5
- Value bytes: `1C 00` (Little Endian)
- Byte breakdown:
  - Low byte: `0x1C` = 28 decimal
  - High byte: `0x00` = 0 decimal
- **Calculation**: `0x1C + (0x00 × 256) = 28` = **28 µg/m³**
- **✓ Confirmed real-world value**

**Example 3**: Response `AC 06 13 9B 01` (eCO2 sensor)
- Sensor ID: `0x13` = eCO2
- Value bytes: `9B 01` (Little Endian)
- Byte breakdown:
  - Low byte: `0x9B` = 155 decimal
  - High byte: `0x01` = 1 decimal
- **Calculation**: `0x9B + (0x01 × 256) = 155 + 256` = **411 ppm**
- **✓ Confirmed real-world value**

**Example 4**: Response `AC 06 12 01 00` (TVOC sensor - special encoding)
- Sensor ID: `0x12` = TVOC
- Value bytes: `01 00` (Little Endian)
- **Raw value**: `0x01 + (0x00 × 256)` = **1**
- **Actual value**: **0.001 mg/m³** (special scaling factor)
- **✓ Confirmed: This sensor uses a different encoding**

#### Format Summary
- **Data Type**: 16-bit unsigned integer
- **Byte Order**: Little Endian (LSB first)
- **Range**: 0 to 65535
- **Parsing Formula**: `value = low_byte + (high_byte × 256)`
- **Python**: `int.from_bytes(value_bytes[:2], byteorder='little')`

#### Analysis Needed
- Determine which sensor ID corresponds to which physical sensor
- Collect multiple readings with known reference values
- Compare with other air quality sensors
- Test different environmental conditions
- Determine scaling factors and units:
  - Temperature might be raw value / 10 (e.g., 235 = 23.5°C)
  - Humidity might be percentage (e.g., 51 = 51%)
  - PM values might be in µg/m³ (e.g., 25 = 25 µg/m³)

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
- ✅ Value format: 2 bytes, **Little Endian**, unsigned 16-bit integer
- ✅ **Sensor ID mappings (confirmed)**:
  - `0x08` = Noise Level (dB)
  - `0x09` = PM2.5 (µg/m³)
  - `0x11` = PM10 (µg/m³)
  - `0x12` = TVOC (mg/m³, special encoding)
  - `0x13` = eCO2 (ppm)

### To Be Determined
- ❓ Sensor IDs `0x01`, `0x06`, `0x0A` - not yet identified
- ❓ Scaling factor for TVOC (0x12) - appears to use special encoding
- ❓ Whether all sensors use 2-byte values or if some use different lengths
- ❓ Other command types besides `0x06`
- ❓ Error handling and timeout behavior

## Example Communication Session

```
1. Connect to device via BLE
2. Subscribe to notifications on 0xFFF4
3. Write to 0xFFF1: [0xAC, 0x06, 0x08]
   └─> Request sensor 0x08 (Noise) with sequence ID 0xAC
4. Receive from 0xFFF4: [0xAC, 0x06, 0x08, 0x31, 0x00]
   └─> Response: seq=0xAC, cmd=0x06, sensor=0x08, value_bytes=[0x31, 0x00]
5. Parse Little Endian value: 0x31 + (0x00 × 256) = 49 dB
6. Repeat for other sensor IDs (0x01, 0x06, 0x09, 0x0A, 0x11, 0x12, 0x13)
```

### Detailed Example with Multiple Sensors

```python
# Reading Noise (confirmed sensor 0x08)
Send:    AC 06 08
Receive: AC 06 08 31 00    # Little Endian: 0x31 + (0x00 × 256) = 49 dB

# Reading PM2.5 (confirmed sensor 0x09)
Send:    AD 06 09
Receive: AD 06 09 1C 00    # Little Endian: 0x1C + (0x00 × 256) = 28 µg/m³

# Reading PM10 (confirmed sensor 0x11)
Send:    AE 06 11
Receive: AE 06 11 16 00    # Little Endian: 0x16 + (0x00 × 256) = 22 µg/m³

# Reading TVOC (confirmed sensor 0x12 - special encoding)
Send:    AF 06 12
Receive: AF 06 12 01 00    # Little Endian: 0x01 + (0x00 × 256) = 1 → 0.001 mg/m³

# Reading eCO2 (confirmed sensor 0x13)
Send:    B0 06 13
Receive: B0 06 13 9B 01    # Little Endian: 0x9B + (0x01 × 256) = 155 + 256 = 411 ppm
```

**Note**: Each request uses a different sequence ID (AC, AD, AE, AF) to track responses.

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
