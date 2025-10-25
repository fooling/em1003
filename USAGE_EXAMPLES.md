# EM1003 使用示例 / Usage Examples

## 中文示例

### 1. 添加设备

1. 进入 Home Assistant 设置
2. 点击"设备与服务"
3. 点击右下角的"+ 添加集成"
4. 搜索 "EM1003"
5. 输入设备的 MAC 地址（例如：`A4:C1:38:12:34:56`）

### 2. 完整协议发现（推荐首次使用）

打开开发者工具 > 服务，执行：

```yaml
service: em1003.discover_all
data:
  mac_address: "A4:C1:38:12:34:56"
```

然后查看日志（设置 > 系统 > 日志），你会看到类似这样的输出：

```
Service: 00001800-0000-1000-8000-00805f9b34fb
  Description: Generic Access
  Characteristics:
    - UUID: 00002a00-0000-1000-8000-00805f9b34fb
      Description: Device Name
      Properties: ['read']
      Value (hex): 454d31303033
      Value (utf-8): EM1003
```

### 3. 扫描设备

查看设备是否在线并获取基本信息：

```yaml
service: em1003.scan_device
data:
  mac_address: "A4:C1:38:12:34:56"
```

### 4. 读取特定特征

假设你从发现中找到了一个有趣的特征：

```yaml
service: em1003.read_characteristic
data:
  mac_address: "A4:C1:38:12:34:56"
  characteristic_uuid: "0000fff1-0000-1000-8000-00805f9b34fb"
```

### 5. 写入数据到设备

测试向设备发送命令：

```yaml
service: em1003.write_characteristic
data:
  mac_address: "A4:C1:38:12:34:56"
  characteristic_uuid: "0000fff2-0000-1000-8000-00805f9b34fb"
  data: "01"
```

或者发送多个字节：

```yaml
service: em1003.write_characteristic
data:
  mac_address: "A4:C1:38:12:34:56"
  characteristic_uuid: "0000fff2-0000-1000-8000-00805f9b34fb"
  data: "0102030405"
```

### 6. 启用调试日志

在 `configuration.yaml` 中添加：

```yaml
logger:
  default: warning
  logs:
    custom_components.em1003: debug
```

重启 Home Assistant 后，所有 EM1003 相关的操作都会有详细的日志输出。

---

## English Examples

### 1. Add Device

1. Go to Home Assistant Settings
2. Click "Devices & Services"
3. Click "+ Add Integration" in the bottom right
4. Search for "EM1003"
5. Enter the device MAC address (e.g., `A4:C1:38:12:34:56`)

### 2. Full Protocol Discovery (Recommended for First Use)

Open Developer Tools > Services, and execute:

```yaml
service: em1003.discover_all
data:
  mac_address: "A4:C1:38:12:34:56"
```

Then check the logs (Settings > System > Logs), you will see output like:

```
Service: 00001800-0000-1000-8000-00805f9b34fb
  Description: Generic Access
  Characteristics:
    - UUID: 00002a00-0000-1000-8000-00805f9b34fb
      Description: Device Name
      Properties: ['read']
      Value (hex): 454d31303033
      Value (utf-8): EM1003
```

### 3. Scan Device

Check if the device is online and get basic information:

```yaml
service: em1003.scan_device
data:
  mac_address: "A4:C1:38:12:34:56"
```

### 4. Read Specific Characteristic

Assuming you found an interesting characteristic from discovery:

```yaml
service: em1003.read_characteristic
data:
  mac_address: "A4:C1:38:12:34:56"
  characteristic_uuid: "0000fff1-0000-1000-8000-00805f9b34fb"
```

### 5. Write Data to Device

Test sending commands to the device:

```yaml
service: em1003.write_characteristic
data:
  mac_address: "A4:C1:38:12:34:56"
  characteristic_uuid: "0000fff2-0000-1000-8000-00805f9b34fb"
  data: "01"
```

Or send multiple bytes:

```yaml
service: em1003.write_characteristic
data:
  mac_address: "A4:C1:38:12:34:56"
  characteristic_uuid: "0000fff2-0000-1000-8000-00805f9b34fb"
  data: "0102030405"
```

### 6. Enable Debug Logging

Add to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.em1003: debug
```

After restarting Home Assistant, all EM1003 related operations will have detailed log output.

---

## Protocol Analysis Workflow / 协议分析工作流程

### Step 1: Discovery / 步骤 1：发现
Run `discover_all` to get a complete map of all services and characteristics.

运行 `discover_all` 获取所有服务和特征的完整映射。

### Step 2: Identify / 步骤 2：识别
Look for custom/vendor-specific UUIDs (usually starting with `0000fff` or other non-standard prefixes).

查找自定义/供应商特定的 UUID（通常以 `0000fff` 或其他非标准前缀开头）。

### Step 3: Read / 步骤 3：读取
Use `read_characteristic` to read values from characteristics with 'read' property.

使用 `read_characteristic` 从具有 'read' 属性的特征中读取值。

### Step 4: Experiment / 步骤 4：实验
Use `write_characteristic` to send test data and observe device behavior.

使用 `write_characteristic` 发送测试数据并观察设备行为。

### Step 5: Document / 步骤 5：记录
Document your findings and share with the community.

记录你的发现并与社区分享。

---

## Common BLE UUIDs / 常见 BLE UUID

These standard UUIDs might appear in your device:

这些标准 UUID 可能会出现在你的设备中：

- `00001800`: Generic Access
- `00001801`: Generic Attribute
- `0000180a`: Device Information
- `00002a00`: Device Name
- `00002a01`: Appearance
- `00002a24`: Model Number String
- `00002a25`: Serial Number String
- `00002a26`: Firmware Revision String
- `00002a27`: Hardware Revision String
- `00002a29`: Manufacturer Name String

Custom/vendor-specific services usually have UUIDs like:
- `0000fff0-...`
- `0000ffe0-...`
- Or completely custom UUIDs

自定义/供应商特定服务通常具有如下 UUID：
- `0000fff0-...`
- `0000ffe0-...`
- 或完全自定义的 UUID
