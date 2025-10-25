# EM1003 BLE Sensor for Home Assistant

[English](#english) | [中文](#中文)

---

## English

### Overview

This is a Home Assistant custom component for the **EM1003 BLE Sensor** (also known as **720环境宝3**).

**Version 0.0.1** is a debugging/development version designed to help reverse-engineer and understand the BLE protocol of the EM1003 device. It provides services to scan, discover, read, and write BLE characteristics through the Home Assistant interface.

### Features

- ✅ Add multiple EM1003 devices via MAC address
- ✅ BLE device scanning and discovery
- ✅ Full GATT service and characteristic enumeration
- ✅ Read BLE characteristics
- ✅ Write to BLE characteristics
- ✅ Comprehensive logging for protocol analysis
- ✅ Chinese and English language support

### Installation

#### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner and select "Custom repositories"
4. Add this repository URL: `https://github.com/yourusername/em1003`
5. Select category: "Integration"
6. Click "Add"
7. Find "EM1003 BLE Sensor" in the integration list and click "Download"
8. Restart Home Assistant

#### Manual Installation

1. Copy the `custom_components/em1003` directory to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

### Configuration

1. Go to **Settings** > **Devices & Services**
2. Click **Add Integration**
3. Search for "EM1003"
4. Enter the MAC address of your EM1003 BLE sensor (e.g., `AA:BB:CC:DD:EE:FF`)
5. Click **Submit**

You can add multiple EM1003 devices by repeating this process.

### Usage (Debugging Services)

This version provides the following services for BLE protocol exploration:

#### 1. `em1003.scan_device`

Scan for a specific BLE device and display its information.

**Parameters:**
- `mac_address`: MAC address of the device

**Example:**
```yaml
service: em1003.scan_device
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
```

#### 2. `em1003.discover_all`

Perform a complete discovery of all services and characteristics on the device. This is the most useful service for initial protocol analysis.

**Parameters:**
- `mac_address`: MAC address of the device

**Example:**
```yaml
service: em1003.discover_all
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
```

**Output:** Check Home Assistant logs for detailed information about all services, characteristics, their properties, and current values.

#### 3. `em1003.list_services`

List all GATT services available on the device.

**Parameters:**
- `mac_address`: MAC address of the device

**Example:**
```yaml
service: em1003.list_services
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
```

#### 4. `em1003.read_characteristic`

Read a specific BLE characteristic.

**Parameters:**
- `mac_address`: MAC address of the device
- `characteristic_uuid`: UUID of the characteristic to read

**Example:**
```yaml
service: em1003.read_characteristic
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
  characteristic_uuid: "00002a00-0000-1000-8000-00805f9b34fb"
```

#### 5. `em1003.write_characteristic`

Write data to a specific BLE characteristic.

**Parameters:**
- `mac_address`: MAC address of the device
- `characteristic_uuid`: UUID of the characteristic to write to
- `data`: Data to write (hex string or byte array)

**Example:**
```yaml
service: em1003.write_characteristic
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
  characteristic_uuid: "00002a00-0000-1000-8000-00805f9b34fb"
  data: "0102ff"
```

### Viewing Logs

All service calls output detailed information to the Home Assistant logs. To view:

1. Go to **Settings** > **System** > **Logs**
2. Or check the `home-assistant.log` file
3. Search for "EM1003" or "em1003" to filter relevant logs

**Tip:** Set the logging level to `debug` for more detailed output:

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.em1003: debug
```

### Workflow for Protocol Analysis

1. **Add the device** using its MAC address
2. **Run `discover_all`** service to get a complete dump of all services and characteristics
3. **Analyze the logs** to identify interesting characteristics
4. **Use `read_characteristic`** to read specific values
5. **Use `write_characteristic`** to test writing commands
6. **Document your findings** for future firmware development

### Requirements

- Home Assistant 2023.1 or later
- Bluetooth adapter on the Home Assistant host
- Python 3.11 or later

### Troubleshooting

**Device not found:**
- Make sure the EM1003 is powered on and within range
- Check if Bluetooth is enabled on your Home Assistant host
- Try running `em1003.scan_device` to see if it's discoverable

**Connection errors:**
- The device might be connected to another service
- Try restarting the EM1003 device
- Check the logs for detailed error messages

### License

MIT License - See [LICENSE](LICENSE) file for details

### Contributing

This is a reverse-engineering project. If you discover any protocol details about the EM1003, please contribute:

1. Fork this repository
2. Document your findings
3. Submit a pull request

---

## 中文

### 概述

这是一个用于 **EM1003 蓝牙传感器**（也称为 **720环境宝3**）的 Home Assistant 自定义组件。

**版本 0.0.1** 是一个调试/开发版本，旨在帮助逆向工程和理解 EM1003 设备的 BLE 协议。它提供了通过 Home Assistant 界面扫描、发现、读取和写入 BLE 特征的服务。

### 功能特性

- ✅ 通过 MAC 地址添加多个 EM1003 设备
- ✅ BLE 设备扫描和发现
- ✅ 完整的 GATT 服务和特征枚举
- ✅ 读取 BLE 特征
- ✅ 写入 BLE 特征
- ✅ 用于协议分析的全面日志记录
- ✅ 中英文语言支持

### 安装

#### HACS（推荐）

1. 在 Home Assistant 中打开 HACS
2. 点击"集成"
3. 点击右上角的三个点并选择"自定义存储库"
4. 添加此仓库 URL：`https://github.com/yourusername/em1003`
5. 选择类别："Integration"
6. 点击"添加"
7. 在集成列表中找到"EM1003 BLE Sensor"并点击"下载"
8. 重启 Home Assistant

#### 手动安装

1. 将 `custom_components/em1003` 目录复制到你的 Home Assistant 的 `custom_components` 目录
2. 重启 Home Assistant

### 配置

1. 进入 **设置** > **设备与服务**
2. 点击 **添加集成**
3. 搜索 "EM1003"
4. 输入你的 EM1003 蓝牙传感器的 MAC 地址（例如：`AA:BB:CC:DD:EE:FF`）
5. 点击 **提交**

你可以通过重复此过程添加多个 EM1003 设备。

### 使用方法（调试服务）

此版本提供以下用于 BLE 协议探索的服务：

#### 1. `em1003.scan_device`

扫描特定的 BLE 设备并显示其信息。

**参数：**
- `mac_address`：设备的 MAC 地址

**示例：**
```yaml
service: em1003.scan_device
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
```

#### 2. `em1003.discover_all`

对设备上的所有服务和特征执行完整发现。这是初始协议分析最有用的服务。

**参数：**
- `mac_address`：设备的 MAC 地址

**示例：**
```yaml
service: em1003.discover_all
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
```

**输出：** 查看 Home Assistant 日志以获取有关所有服务、特征、其属性和当前值的详细信息。

#### 3. `em1003.list_services`

列出设备上所有可用的 GATT 服务。

**参数：**
- `mac_address`：设备的 MAC 地址

**示例：**
```yaml
service: em1003.list_services
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
```

#### 4. `em1003.read_characteristic`

读取特定的 BLE 特征。

**参数：**
- `mac_address`：设备的 MAC 地址
- `characteristic_uuid`：要读取的特征的 UUID

**示例：**
```yaml
service: em1003.read_characteristic
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
  characteristic_uuid: "00002a00-0000-1000-8000-00805f9b34fb"
```

#### 5. `em1003.write_characteristic`

向特定的 BLE 特征写入数据。

**参数：**
- `mac_address`：设备的 MAC 地址
- `characteristic_uuid`：要写入的特征的 UUID
- `data`：要写入的数据（十六进制字符串或字节数组）

**示例：**
```yaml
service: em1003.write_characteristic
data:
  mac_address: "AA:BB:CC:DD:EE:FF"
  characteristic_uuid: "00002a00-0000-1000-8000-00805f9b34fb"
  data: "0102ff"
```

### 查看日志

所有服务调用都会向 Home Assistant 日志输出详细信息。要查看：

1. 进入 **设置** > **系统** > **日志**
2. 或查看 `home-assistant.log` 文件
3. 搜索 "EM1003" 或 "em1003" 以过滤相关日志

**提示：** 将日志级别设置为 `debug` 以获得更详细的输出：

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.em1003: debug
```

### 协议分析工作流程

1. **使用 MAC 地址添加设备**
2. **运行 `discover_all` 服务**以获取所有服务和特征的完整转储
3. **分析日志**以识别感兴趣的特征
4. **使用 `read_characteristic`** 读取特定值
5. **使用 `write_characteristic`** 测试写入命令
6. **记录你的发现**以供未来固件开发

### 系统要求

- Home Assistant 2023.1 或更高版本
- Home Assistant 主机上的蓝牙适配器
- Python 3.11 或更高版本

### 故障排除

**找不到设备：**
- 确保 EM1003 已开机并在范围内
- 检查 Home Assistant 主机上是否启用了蓝牙
- 尝试运行 `em1003.scan_device` 查看是否可发现

**连接错误：**
- 设备可能已连接到其他服务
- 尝试重启 EM1003 设备
- 查看日志以获取详细错误消息

### 许可证

MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

### 贡献

这是一个逆向工程项目。如果你发现了 EM1003 的任何协议细节，欢迎贡献：

1. Fork 此仓库
2. 记录你的发现
3. 提交 pull request
