# EM1003 连接逻辑重构任务

## 问题描述

### 问题 1: Sensor Update 返回空数据但显示 success: True

**现象**:
```
2025-10-26 20:38:27.131 ERROR  [DIAG] ✗ Failed to connect to 54:6C:0E:49:2B:55
2025-10-26 20:38:27.132 DEBUG  [CIRCUIT] ✗ Failure recorded (2/3)
2025-10-26 20:38:27.132 DEBUG  [DIAG] Released connection lock for 54:6C:0E:49:2B:55
2025-10-26 20:38:27.132 DEBUG  Sensor data updated: {}
2025-10-26 20:38:27.132 DEBUG  Finished fetching EM1003 data (success: True)  ❌
```

**根本原因**:
- `read_all_sensors()` 在连接失败时捕获异常，但不抛出，而是返回空字典 `{}`
- `_async_update_data()` 收到空字典，没有检查数据有效性就返回
- DataUpdateCoordinator 没有收到异常，误认为更新成功

**影响**:
- 传感器状态显示错误（should be unavailable but shows as available）
- 用户无法判断连接是否真的失败
- Circuit breaker 虽然记录了失败，但 coordinator 状态不匹配

**相关代码**:
- `custom_components/em1003/__init__.py:963-993` - 异常处理逻辑
- `custom_components/em1003/sensor.py:101-120` - Update data 逻辑

---

### 问题 2: 连接锁设计不合理

**现象**:
- 定义了 `self._client` 但从未使用（`__init__.py:224`）
- 每次 `read_all_sensors()` 都建立新的 BLE 连接
- 读取完成后立即断开连接
- 使用 `asyncio.Lock()` 来防止并发连接

**设计问题**:

| 当前设计 | 问题 |
|---------|------|
| 每次建立新连接 | 耗时长（日志显示 32+ 秒完成一次更新）|
| 频繁连接/断开 | 给蓝牙栈造成压力，容易触发 "connection abort" 错误 |
| 使用锁序列化访问 | 不必要 - BLE 本身就是单连接，只需检查连接状态 |
| 不复用连接 | 浪费资源，增加失败概率 |

**正确设计**:
- 维护一个**持久连接** `self._client`
- 在读取前检查: `if self._client and self._client.is_connected`
- 如果已连接，直接使用；如果未连接，才建立新连接
- **不需要锁** - 连接状态检查就足够了

**相关代码**:
- `custom_components/em1003/__init__.py:226` - `_connection_lock` 定义
- `custom_components/em1003/__init__.py:761-762` - Lock 使用
- `custom_components/em1003/__init__.py:729-993` - `read_all_sensors()` 方法

---

## 待完成任务

### 任务 1: 修复 Sensor Update Success 误报 ⚠️ 高优先级

**目标**: 确保连接失败时 coordinator 能正确识别失败状态

**修改文件**: `custom_components/em1003/sensor.py`

**实现方案**:
```python
async def _async_update_data(self) -> dict:
    """Fetch data from the device."""
    try:
        _LOGGER.debug("Updating sensor data for %s", self.mac_address)
        data = await self.em1003_device.read_all_sensors()

        # 检查是否真的获取到数据
        valid_count = sum(1 for v in data.values() if v is not None)
        if valid_count == 0:
            raise UpdateFailed("连接失败或设备无响应，未获取到任何传感器数据")

        _LOGGER.debug("Sensor data updated: %s", data)

        # Log if problematic sensors have no data
        if data:
            for sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                value = data.get(sensor_id)
                if value is None:
                    from .const import SENSOR_TYPES
                    sensor_info = SENSOR_TYPES.get(sensor_id, {})
                    sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                    _LOGGER.info("[%s] No data received", sensor_name)

        return data
    except Exception as err:
        raise UpdateFailed(f"Error communicating with device: {err}") from err
```

**验证标准**:
- 连接失败时，日志显示 `success: False`
- 传感器实体状态显示为 unavailable
- Circuit breaker 状态与 coordinator 状态一致

---

### 任务 2: 重构为持久连接架构 🔧 中优先级

**目标**: 维护持久 BLE 连接，避免频繁连接/断开

**修改文件**: `custom_components/em1003/__init__.py`

**实现方案**:

1. **新增连接管理方法**:
```python
async def _ensure_connected(self) -> BleakClient:
    """Ensure we have an active connection, reusing existing if possible."""
    # 检查现有连接
    if self._client and self._client.is_connected:
        _LOGGER.debug("[CONN] Reusing existing connection to %s", self.mac_address)
        return self._client

    # 需要建立新连接
    _LOGGER.debug("[CONN] Establishing new connection to %s", self.mac_address)
    self._client = await self._establish_connection()

    # 订阅通知（只需订阅一次）
    await self._client.start_notify(EM1003_NOTIFY_CHAR_UUID, self._notification_handler)
    _LOGGER.debug("[CONN] ✓ Connected and subscribed to %s", self.mac_address)

    return self._client
```

2. **修改 `read_all_sensors()` 方法**:
```python
async def read_all_sensors(self) -> dict[int, float | None]:
    """Read all sensors using a persistent connection."""
    results: dict[int, float | None] = {}

    # Check circuit breaker
    can_attempt, reason = self._circuit_breaker.can_attempt()
    if not can_attempt:
        _LOGGER.warning("[CIRCUIT] Blocked: %s", reason)
        return {sensor_id: None for sensor_id in SENSOR_TYPES.keys()}

    try:
        # 确保连接（复用现有或建立新的）
        client = await self._ensure_connected()

        # 读取所有传感器
        for sensor_id in SENSOR_TYPES.keys():
            if not client.is_connected:
                _LOGGER.warning("[CONN] Connection lost during reads")
                break

            value = await self._read_single_sensor(client, sensor_id)
            results[sensor_id] = value

        # 成功后记录
        self._circuit_breaker.record_success()

    except BleakError as err:
        _LOGGER.error("[CONN] BLE error: %s", err)
        self._circuit_breaker.record_failure()
        # 连接失败，清理 client
        self._client = None
        # 抛出异常让上层知道失败
        raise
    except Exception as err:
        _LOGGER.error("[CONN] Unexpected error: %s", err, exc_info=True)
        self._circuit_breaker.record_failure()
        self._client = None
        raise

    return results
```

3. **新增清理方法**:
```python
async def disconnect(self):
    """Explicitly disconnect from device."""
    if self._client and self._client.is_connected:
        try:
            await self._client.disconnect()
            _LOGGER.debug("[CONN] Disconnected from %s", self.mac_address)
        except Exception as err:
            _LOGGER.debug("[CONN] Error during disconnect: %s", err)
        finally:
            self._client = None
            self._last_disconnect_time = time.time()
```

**验证标准**:
- 多次更新复用同一个连接
- 日志显示 "Reusing existing connection"
- 更新速度显著提升（从 32秒 降到 < 10秒）
- 减少 "connection abort" 错误

---

### 任务 3: 移除不必要的连接锁 🧹 低优先级

**目标**: 简化代码，移除 `self._connection_lock`

**前置条件**: 任务 2 完成后

**修改文件**: `custom_components/em1003/__init__.py`

**实现方案**:
1. 删除 `self._connection_lock = asyncio.Lock()` (line 226)
2. 移除所有 `async with self._connection_lock:` 的使用
3. 依靠 `self._client.is_connected` 状态检查来避免冲突

**理由**:
- BLE 本身就是单连接协议
- 持久连接架构下，连接状态检查就足够了
- 简化代码，减少锁开销

**验证标准**:
- 代码中没有 `_connection_lock` 相关引用
- 功能正常，没有并发问题
- 代码更简洁易读

---

## 实施计划

### 第一阶段: 快速修复（立即执行）
- ✅ 任务 1: 修复 success 误报
- 测试验证

### 第二阶段: 架构重构（后续执行）
- 🔧 任务 2: 重构为持久连接
- 测试验证性能提升

### 第三阶段: 代码清理（可选）
- 🧹 任务 3: 移除连接锁
- 最终测试

---

## 预期效果

修复完成后:
1. ✅ 连接失败时状态正确显示为失败
2. ✅ 传感器 unavailable 状态准确
3. ✅ 更新速度提升 70%+（32秒 → <10秒）
4. ✅ 减少蓝牙栈压力和 connection abort 错误
5. ✅ 代码更简洁易维护

---

## 参考信息

**相关文件**:
- `custom_components/em1003/__init__.py` - 核心连接逻辑
- `custom_components/em1003/sensor.py` - Coordinator 更新逻辑
- `custom_components/em1003/const.py` - 常量定义

**关键日志标签**:
- `[DIAG]` - 诊断信息
- `[CIRCUIT]` - 熔断器状态
- `[CONN]` - 连接管理（新增）
- `[REQ]` - 请求处理

**测试设备**:
- MAC: `54:6C:0E:49:2B:55`
- Model: EM1003 BLE Air Quality Sensor
