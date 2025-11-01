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

**8 Sensor IDs** (ALL CONFIRMED):
- `0x01` Temperature (°C)
- `0x06` Humidity (%)
- `0x08` Noise (dB)
- `0x09` PM2.5 (µg/m³)
- `0x0A` Formaldehyde (mg/m³)
- `0x11` PM10 (µg/m³)
- `0x12` TVOC (mg/m³)
- `0x13` eCO2 (ppm)

**Buzzer Control** (CONFIRMED):
- Command: `0x50`
- Query state: `[seq_id][0x50][0x00]` → Response: `[seq_id][0x50][0x00][state]`
- Turn on: `[seq_id][0x50][0x01][0x01]` → Response: `[seq_id][0x05][0x01]`
- Turn off: `[seq_id][0x50][0x01][0x00]` → Response: `[seq_id][0x05][0x01]`

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

### Buzzer Control Protocol

The device supports buzzer control through command `0x50`.

#### Query Buzzer State

**Request Format** (3 bytes):
```
[Sequence ID] [0x50] [0x00]
```

**Response Format** (4 bytes):
```
[Sequence ID] [0x50] [0x00] [State]
```

- **State**: `0x01` = Buzzer ON, `0x00` = Buzzer OFF

**Example**:
```
Request:  00 50 00
Response: 00 50 00 01    # Buzzer is ON
Response: 00 50 00 00    # Buzzer is OFF
```

#### Set Buzzer State

**Request Format** (4 bytes):
```
[Sequence ID] [0x50] [0x01] [State]
```

**Response Format** (3 bytes):
```
[Sequence ID] [0x05] [0x01]
```

**Turn ON**:
```
Request:  00 50 01 01
Response: 00 05 01       # Successfully turned ON
```

**Turn OFF**:
```
Request:  00 50 01 00
Response: 00 05 01       # Successfully turned OFF
```

### Sensor ID Mapping

The following sensor IDs have been observed:

| Sensor ID | Sensor Type | Unit | Formula | Example |
|-----------|------------|------|---------|---------|
| `0x01` | **Temperature** | °C | `(raw - 4000) / 100` | 2C-1A (6700) = 27.00°C |
| `0x06` | **Humidity** | % | `raw / 100` | DB-11 (4571) = 45.71% |
| `0x08` | **Noise Level** | dB | `raw` (no conversion) | 31-00 (49) = 49 dB |
| `0x09` | **PM2.5** | µg/m³ | `raw` (no conversion) | 1C-00 (28) = 28 µg/m³ |
| `0x0A` | **Formaldehyde** | mg/m³ | `(raw - 16384) / 1000` | 00-40 (16384) = 0.000 mg/m³ |
| `0x11` | **PM10** | µg/m³ | `raw` (no conversion) | 16-00 (22) = 22 µg/m³ |
| `0x12` | **TVOC** | mg/m³ | `raw × 0.001` | 01-00 (1) = 0.001 mg/m³ |
| `0x13` | **eCO2** | ppm | `raw` (no conversion) | 9B-01 (411) = 411 ppm |

**All 8 sensor types have been confirmed through testing with real environmental data!**

#### Special Encoding Notes

1. **Temperature (0x01)**: Uses offset of 4000 to support negative temperatures
   - Range: -40°C to 615.35°C (raw 0 to 65535)
   - Negative example: -10°C = raw 3900

2. **Humidity (0x06)**: Scaled by 100 for precision
   - Range: 0% to 655.35% (raw 0 to 65535)
   - Typical indoor range: 30-70%

3. **Formaldehyde (0x0A)**: Uses offset of 16384 (0x4000)
   - Baseline value: 16384 = 0.000 mg/m³
   - Can measure positive and negative deviations from baseline

4. **TVOC (0x12)**: Multiplies raw value by 0.001
   - Different from other sensors which divide by scale factor

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

**Example 1**: Temperature - Response `CD 06 01 2C 1A`
- Sensor ID: `0x01` = Temperature
- Value bytes: `2C 1A` (Little Endian)
- **Raw value**: `0x2C + (0x1A × 256) = 44 + 6656` = **6700**
- **Formula**: `(6700 - 4000) / 100` = **27.00°C**
- **✓ Confirmed with real measurement: 27.0°C**

**Example 2**: Humidity - Response `CB 06 06 DB 11`
- Sensor ID: `0x06` = Humidity
- Value bytes: `DB 11` (Little Endian)
- **Raw value**: `0xDB + (0x11 × 256) = 219 + 4352` = **4571**
- **Formula**: `4571 / 100` = **45.71%**
- **✓ Confirmed with real measurement: 45%**

**Example 3**: Noise - Response `AC 06 08 31 00`
- Sensor ID: `0x08` = Noise
- Value bytes: `31 00` (Little Endian)
- **Raw value**: `0x31 + (0x00 × 256)` = **49**
- **Formula**: `raw` (no conversion) = **49 dB**
- **✓ Confirmed with real measurement: 49 dB**

**Example 4**: PM2.5 - Response `AD 06 09 1C 00`
- Sensor ID: `0x09` = PM2.5
- Value bytes: `1C 00` (Little Endian)
- **Raw value**: `0x1C + (0x00 × 256)` = **28**
- **Formula**: `raw` (no conversion) = **28 µg/m³**
- **✓ Confirmed with real measurement: 28 µg/m³**

**Example 5**: Formaldehyde - Response `CD 06 0A 00 40`
- Sensor ID: `0x0A` = Formaldehyde
- Value bytes: `00 40` (Little Endian)
- **Raw value**: `0x00 + (0x40 × 256) = 0 + 16384` = **16384**
- **Formula**: `(16384 - 16384) / 1000` = **0.000 mg/m³**
- **✓ Confirmed with real measurement: 0.00 mg/m³**

**Example 6**: PM10 - Response `AE 06 11 16 00`
- Sensor ID: `0x11` = PM10
- Value bytes: `16 00` (Little Endian)
- **Raw value**: `0x16 + (0x00 × 256)` = **22**
- **Formula**: `raw` (no conversion) = **22 µg/m³**
- **✓ Confirmed**

**Example 7**: TVOC - Response `AF 06 12 01 00`
- Sensor ID: `0x12` = TVOC
- Value bytes: `01 00` (Little Endian)
- **Raw value**: `0x01 + (0x00 × 256)` = **1**
- **Formula**: `1 × 0.001` = **0.001 mg/m³**
- **✓ Confirmed with real measurement: 0.001 mg/m³**

**Example 8**: eCO2 - Response `B0 06 13 9B 01`
- Sensor ID: `0x13` = eCO2
- Value bytes: `9B 01` (Little Endian)
- **Raw value**: `0x9B + (0x01 × 256) = 155 + 256` = **411**
- **Formula**: `raw` (no conversion) = **411 ppm**
- **✓ Confirmed with real measurement: 411 ppm**

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

### Confirmed ✅
- ✅ Device name reading via standard BLE characteristic
- ✅ Service UUID: `09de2880-1415-4e2c-a48a-3938e3288537`
- ✅ Write characteristic: `0xFFF1`
- ✅ Notify characteristic: `0xFFF4`
- ✅ Request format: 3 bytes `[seq][06][sensor_id]`
- ✅ Response format: `[seq][06][sensor_id][value_bytes]`
- ✅ Sequence ID matching works
- ✅ Value format: 2 bytes, **Little Endian**, unsigned 16-bit integer
- ✅ **ALL 8 Sensor ID mappings confirmed**:
  - `0x01` = Temperature (°C) - formula: `(raw - 4000) / 100`
  - `0x06` = Humidity (%) - formula: `raw / 100`
  - `0x08` = Noise Level (dB) - formula: `raw`
  - `0x09` = PM2.5 (µg/m³) - formula: `raw`
  - `0x0A` = Formaldehyde (mg/m³) - formula: `(raw - 16384) / 1000`
  - `0x11` = PM10 (µg/m³) - formula: `raw`
  - `0x12` = TVOC (mg/m³) - formula: `raw × 0.001`
  - `0x13` = eCO2 (ppm) - formula: `raw`
- ✅ **Buzzer Control confirmed**:
  - Command `0x50` for buzzer control
  - Query state: 3-byte request `[seq][0x50][0x00]` → 4-byte response `[seq][0x50][0x00][state]`
  - Set state: 4-byte request `[seq][0x50][0x01][state]` → 3-byte response `[seq][0x05][0x01]`
  - State values: `0x00` = OFF, `0x01` = ON

### To Be Determined
- ❓ Other command types besides `0x06` (sensor read) and `0x50` (buzzer control)
- ❓ Error handling and timeout behavior
- ❓ Battery level reading (if supported)

## Example Communication Session

```
1. Connect to device via BLE
2. Subscribe to notifications on 0xFFF4

# Reading Sensor Data
3. Write to 0xFFF1: [0xAC, 0x06, 0x08]
   └─> Request sensor 0x08 (Noise) with sequence ID 0xAC
4. Receive from 0xFFF4: [0xAC, 0x06, 0x08, 0x31, 0x00]
   └─> Response: seq=0xAC, cmd=0x06, sensor=0x08, value_bytes=[0x31, 0x00]
5. Parse Little Endian value: 0x31 + (0x00 × 256) = 49 dB
6. Repeat for other sensor IDs (0x01, 0x06, 0x09, 0x0A, 0x11, 0x12, 0x13)

# Buzzer Control
7. Query buzzer state:
   Write to 0xFFF1: [0x00, 0x50, 0x00]
   Receive from 0xFFF4: [0x00, 0x50, 0x00, 0x00]
   └─> Buzzer is OFF

8. Turn on buzzer:
   Write to 0xFFF1: [0x00, 0x50, 0x01, 0x01]
   Receive from 0xFFF4: [0x00, 0x05, 0x01]
   └─> Buzzer turned ON successfully

9. Turn off buzzer:
   Write to 0xFFF1: [0x00, 0x50, 0x01, 0x00]
   Receive from 0xFFF4: [0x00, 0x05, 0x01]
   └─> Buzzer turned OFF successfully
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
