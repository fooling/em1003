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
10. 从下拉框中选择已扫描到的环境宝设备（系统会自动扫描附近的蓝牙设备）

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

### 3. 高级用法：ESP32 蓝牙代理扩展范围

#### 应用场景
当 Home Assistant 主机蓝牙信号覆盖范围不足时，可使用 ESP32 设备作为蓝牙代理，扩展蓝牙覆盖范围，连接更远距离的环境宝设备。

#### 工作原理
```
ESP32 (Bluetooth Proxy)
  ├─ 扫描附近的 BLE 设备（包括 EM1003）
  ├─ 透明转发蓝牙数据
  └─ 通过 WiFi 传输到 Home Assistant
```

#### 极简安装步骤

1. **准备硬件**
   - ESP32 开发板（推荐 ESP32-C3、ESP32 Supermini 等）
   - USB 数据线

2. **一键刷入固件并配置 WiFi**
   - 访问 ESPHome 官方项目页面：**https://esphome.io/projects/**
   - 找到 **"Bluetooth Proxy"** 项目
   - 选择你的 ESP32 型号，点击 **"Install"** 按钮
   - 按照网页提示连接 USB 并刷入固件
   - **刷入过程中会自动弹出 WiFi 配置界面**，直接输入你的家庭 WiFi 信息即可

3. **自动集成**
   - Home Assistant 会自动发现蓝牙代理设备
   - 在 **设置 > 设备与服务** 中确认添加
   - 所有在 ESP32 范围内的 EM1003 设备将自动被发现

#### 优势
- ✅ **无需编写代码**：官方预编译固件，点击即刷
- ✅ **即插即用**：自动发现，零配置
- ✅ **扩展范围**：将蓝牙覆盖扩展到 ESP32 周围 10 米范围
- ✅ **多设备支持**：一个代理可扫描多个 BLE 设备

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
10. Select your EM1003 device from the dropdown list (system will auto-scan nearby Bluetooth devices)

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

### 3. Advanced Usage: ESP32 Bluetooth Proxy Range Extender

#### Use Case
When your Home Assistant host's Bluetooth range is insufficient, use an ESP32 device as a Bluetooth proxy to extend coverage and reach distant EM1003 sensors.

#### How It Works
```
ESP32 (Bluetooth Proxy)
  ├─ Scans nearby BLE devices (including EM1003)
  ├─ Transparently forwards Bluetooth data
  └─ Transmits to Home Assistant via WiFi
```

#### Simple Installation Steps

1. **Prepare Hardware**
   - ESP32 development board (ESP32-C3, ESP32 Supermini, etc.)
   - USB cable

2. **One-Click Firmware Flash & WiFi Setup**
   - Visit ESPHome official projects page: **https://esphome.io/projects/**
   - Find the **"Bluetooth Proxy"** project
   - Select your ESP32 model, click **"Install"** button
   - Follow on-screen prompts to connect USB and flash firmware
   - **WiFi configuration interface will automatically appear during flashing** - simply enter your home WiFi credentials

3. **Automatic Integration**
   - Home Assistant will auto-discover the Bluetooth proxy
   - Confirm addition in **Settings > Devices & Services**
   - All EM1003 devices within ESP32 range will be automatically discovered

#### Benefits
- ✅ **No Coding Required**: Official pre-compiled firmware, click to flash
- ✅ **Plug & Play**: Auto-discovery, zero configuration
- ✅ **Extended Range**: Expands Bluetooth coverage to ~10 meters around ESP32
- ✅ **Multi-Device Support**: One proxy can scan multiple BLE devices

---

### License

MIT License - See [LICENSE](LICENSE)

### Contributing

This is a reverse-engineering project. Protocol discoveries and improvements are welcome via pull requests.
