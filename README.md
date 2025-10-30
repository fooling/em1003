# EM1003 环境宝3 蓝牙传感器 | BLE Sensor Integration

[English](#english) | [中文](#中文)

---

## 中文

### 概述

这是一个用于 **EM1003 蓝牙传感器**（720环境宝3）的 Home Assistant 自定义组件。支持8种传感器：温度、湿度、噪音、PM2.5、PM10、甲醛、TVOC、eCO2。

### 1. 安装方式

#### 通过 HACS 安装（推荐）

> **注意**：此项目尚未加入HACS官方存储库，需要手动添加自定义存储库。

1. 在 Home Assistant 中打开 **HACS**
2. 点击右上角 **⋮** 菜单，选择 **自定义存储库**
3. 在弹出窗口中填写：
   - **存储库地址**：`https://github.com/fooling/em1003`
   - **类别**：选择 `Integration`（集成）
   - 点击 **添加**
4. 关闭对话框后，点击右下角 **+ 浏览并下载存储库** 按钮
5. 搜索 **"EM1003"**，找到并点击进入
6. 点击右下角 **下载** 按钮，下载最新版本
7. **重启 Home Assistant**（必须重启才能加载集成）
8. 重启后，进入 **设置 > 设备与服务 > 添加集成**
9. 搜索 **"EM1003"**，点击添加
10. 输入你的环境宝设备 MAC 地址（格式：`AA:BB:CC:DD:EE:FF`）

#### 手动安装

复制 `custom_components/em1003` 到 Home Assistant 的 `custom_components` 目录，重启即可。

### 2. 协议逆向工程核心原理

#### BLE 通信架构
```
服务 UUID: 09de2880-1415-4e2c-a48a-3938e3288537
  ├─ 写特征 0xFFF1: 发送命令
  └─ 通知特征 0xFFF4: 接收数据
```

#### 请求格式（3字节）
```
[序列ID] [命令0x06] [传感器ID]
示例: AC 06 08  → 读取噪音传感器
```

#### 响应格式（5字节 Little Endian）
```
[序列ID] [0x06] [传感器ID] [低字节] [高字节]
示例: AC 06 08 31 00  → 噪音值 = 0x31 + (0x00 × 256) = 49 dB
```

#### 8种传感器映射（已全部确认）

| ID | 类型 | 单位 | 解析公式 | 示例 |
|----|------|------|---------|------|
| 0x01 | 温度 | °C | `(raw - 4000) / 100` | 6700 → 27.00°C |
| 0x06 | 湿度 | % | `raw / 100` | 4571 → 45.71% |
| 0x08 | 噪音 | dB | `raw` | 49 → 49 dB |
| 0x09 | PM2.5 | µg/m³ | `raw` | 28 → 28 µg/m³ |
| 0x0A | 甲醛 | mg/m³ | `(raw - 16384) / 1000` | 16384 → 0.000 |
| 0x11 | PM10 | µg/m³ | `raw` | 22 → 22 µg/m³ |
| 0x12 | TVOC | mg/m³ | `raw × 0.001` | 1 → 0.001 |
| 0x13 | eCO2 | ppm | `raw` | 411 → 411 ppm |

**关键点**：数据采用 **Little Endian** 编码，值 = 低字节 + (高字节 × 256)

完整协议文档：[docs/protocol_reverse_engineering.md](docs/protocol_reverse_engineering.md)

### 3. 高级用法：ESP32 蓝牙网关

#### 应用场景
Home Assistant 主机蓝牙信号覆盖不足时，使用 ESP32 Supermini 作为远程蓝牙网关，通过 ESPHome 连接多个环境宝。

#### 核心逻辑
```
ESP32 Supermini (ESPHome)
  ├─ 蓝牙扫描多个 EM1003 设备
  ├─ 建立 BLE 连接（最多支持3个同时连接）
  ├─ 轮询读取各设备传感器数据
  └─ 通过 WiFi 上报到 Home Assistant
```

#### 快速步骤

1. **准备硬件**：ESP32 Supermini + USB 数据线

2. **安装 ESPHome**：在 Home Assistant 中安装 ESPHome 集成

3. **创建设备配置** `em1003-gateway.yaml`：
```yaml
esphome:
  name: em1003-gateway

esp32:
  board: esp32dev

wifi:
  ssid: "你的WiFi"
  password: "WiFi密码"

api:
  encryption:
    key: "自动生成的密钥"

ota:

esp32_ble_tracker:
  scan_parameters:
    active: true

# 配置多个 EM1003 设备（替换为你的 MAC 地址）
sensor:
  - platform: ble_client
    ble_client_id: em1003_1
    # 温度、湿度等传感器配置
    # 具体配置参考 ESPHome BLE Client 文档
```

4. **烧录固件**：ESPHome > 编译并上传到 ESP32

5. **自动发现**：Home Assistant 会自动发现网关设备及所有传感器

#### 注意事项
- ESP32 建议同时连接 ≤3 个设备，避免连接槽耗尽
- 设备需在 ESP32 蓝牙范围内（约10米）
- 传感器更新间隔建议 ≥30秒，减少电池消耗

---

## English

### Overview

A Home Assistant custom integration for the **EM1003 BLE Sensor** (720 Air Quality Monitor). Supports 8 sensors: Temperature, Humidity, Noise, PM2.5, PM10, Formaldehyde, TVOC, and eCO2.

### 1. Installation

#### Install via HACS (Recommended)

> **Note**: This project is not yet in the official HACS repository. You need to manually add it as a custom repository.

1. Open **HACS** in Home Assistant
2. Click the **⋮** menu (top right), select **Custom repositories**
3. In the popup dialog, fill in:
   - **Repository URL**: `https://github.com/fooling/em1003`
   - **Category**: Select `Integration`
   - Click **Add**
4. Close the dialog, then click **+ Explore & Download Repositories** button (bottom right)
5. Search for **"EM1003"** and select it
6. Click **Download** button (bottom right) to download the latest version
7. **Restart Home Assistant** (required to load the integration)
8. After restart, go to **Settings > Devices & Services > Add Integration**
9. Search for **"EM1003"** and add it
10. Enter your device's MAC address (format: `AA:BB:CC:DD:EE:FF`)

#### Manual Installation

Copy `custom_components/em1003` to your Home Assistant `custom_components` directory and restart.

### 2. Reverse Engineering Protocol Essentials

#### BLE Communication Structure
```
Service UUID: 09de2880-1415-4e2c-a48a-3938e3288537
  ├─ Write Char 0xFFF1: Send commands
  └─ Notify Char 0xFFF4: Receive responses
```

#### Request Format (3 bytes)
```
[Sequence ID] [Command 0x06] [Sensor ID]
Example: AC 06 08  → Read noise sensor
```

#### Response Format (5 bytes, Little Endian)
```
[Sequence ID] [0x06] [Sensor ID] [Low Byte] [High Byte]
Example: AC 06 08 31 00  → Noise = 0x31 + (0x00 × 256) = 49 dB
```

#### 8 Sensor ID Mappings (All Confirmed)

| ID | Type | Unit | Formula | Example |
|----|------|------|---------|---------|
| 0x01 | Temperature | °C | `(raw - 4000) / 100` | 6700 → 27.00°C |
| 0x06 | Humidity | % | `raw / 100` | 4571 → 45.71% |
| 0x08 | Noise | dB | `raw` | 49 → 49 dB |
| 0x09 | PM2.5 | µg/m³ | `raw` | 28 → 28 µg/m³ |
| 0x0A | Formaldehyde | mg/m³ | `(raw - 16384) / 1000` | 16384 → 0.000 |
| 0x11 | PM10 | µg/m³ | `raw` | 22 → 22 µg/m³ |
| 0x12 | TVOC | mg/m³ | `raw × 0.001` | 1 → 0.001 |
| 0x13 | eCO2 | ppm | `raw` | 411 → 411 ppm |

**Key Point**: Data uses **Little Endian** encoding: Value = Low Byte + (High Byte × 256)

Full protocol documentation: [docs/protocol_reverse_engineering.md](docs/protocol_reverse_engineering.md)

### 3. Advanced Usage: ESP32 Bluetooth Gateway

#### Use Case
When Home Assistant's Bluetooth range is insufficient, use an ESP32 Supermini as a remote BLE gateway to connect multiple EM1003 devices via ESPHome.

#### Core Logic
```
ESP32 Supermini (ESPHome)
  ├─ Scan multiple EM1003 devices
  ├─ Establish BLE connections (max 3 concurrent)
  ├─ Poll sensor data from each device
  └─ Report to Home Assistant via WiFi
```

#### Quick Setup

1. **Hardware**: ESP32 Supermini + USB cable

2. **Install ESPHome**: Add ESPHome integration in Home Assistant

3. **Create device config** `em1003-gateway.yaml`:
```yaml
esphome:
  name: em1003-gateway

esp32:
  board: esp32dev

wifi:
  ssid: "YourWiFi"
  password: "WiFiPassword"

api:
  encryption:
    key: "auto-generated-key"

ota:

esp32_ble_tracker:
  scan_parameters:
    active: true

# Configure multiple EM1003 devices (replace with your MAC addresses)
sensor:
  - platform: ble_client
    ble_client_id: em1003_1
    # Temperature, humidity sensor configs
    # Refer to ESPHome BLE Client documentation
```

4. **Flash firmware**: ESPHome > Compile and upload to ESP32

5. **Auto-discovery**: Home Assistant will automatically discover the gateway and all sensors

#### Important Notes
- Recommend ≤3 concurrent connections to avoid ESP32 connection slot exhaustion
- Devices must be within ESP32 BLE range (~10 meters)
- Sensor update interval ≥30 seconds recommended to reduce battery drain

---

### License

MIT License - See [LICENSE](LICENSE)

### Contributing

This is a reverse-engineering project. Protocol discoveries and improvements are welcome via pull requests.
