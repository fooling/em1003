# EM1003 BLE Session Refactor — Authoritative Design & Test Plan

Status: **authoritative**. Where this document and the task brief disagree, this document wins.
Scope: `custom_components/em1003/*` + `tests/*`. Target: HA 2026.2.x, bleak 2.1.x, bleak-retry-connector 4.4.x, Python 3.13.

Everything below is written so that the device implementer, the HA-side implementer and the test
authors can work **in parallel without seeing each other's code**. Every name, signature, return
value, raise/no-raise rule and observable side effect that crosses a file boundary is pinned here.

---

## 1. Goals, non-goals, invariants

### 1.1 Goals

| # | Goal |
|---|------|
| G1 | One BLE link at a time per device, always closed, never leaked (fixes D1) |
| G2 | No unbounded `await` anywhere on the BLE path; the coordinator can never freeze forever (fixes D2) |
| G3 | A circuit breaker that is correct, unit-testable and cannot be poisoned by unrelated failures (fixes D3) |
| G4 | One BLE connection per poll covering sensors **and** buzzer; no extra connections at startup (fixes D4) |
| G5 | Correct manifest dependencies (D5), working options flow with live reload (D6), bleak-2.x-correct RSSI/discovery (D7) |
| G6 | Clean lifecycle: coordinator in `coordinator.py`, proper unload, services registered once, no stale-value-as-fresh (D8) |
| G7 | 100 % backward compatible for installs created by v1.0.3 — zero user action, zero entity churn, zero history loss |

### 1.2 Non-goals

* No config-entry migration. `VERSION` stays `1`, `minor_version` untouched.
* No new entities, no renamed entities, no changed units/device classes/precisions.
* No change to the wire protocol or to any value formula.
* No new runtime requirement beyond what HA already ships (no new pip deps).
* No persistent BLE link ("keep the connection open") — the device stops advertising while connected.

### 1.3 Backward-compatibility invariants (checkable; each has tests in F7)

| ID | Invariant | Checked by |
|----|-----------|-----------|
| BC-01 | `DOMAIN == "em1003"` | F7-16 |
| BC-02 | `ConfigFlow.VERSION == 1`; no `async_migrate_entry` exists | F7-12 |
| BC-03 | `entry.data["mac_address"]` key name and stored value are never rewritten by setup | F7-05 |
| BC-04 | Only **additive** keys may appear in `entry.data`; the only one added is `"device_name"` | F7-06 |
| BC-05 | An entry with `options == {}` works and polls every `DEFAULT_SCAN_INTERVAL == 60` s | F7-03, F4-17 |
| BC-06 | `entry.options["scan_interval"]` key and semantics unchanged; setup never writes options | F7-07 |
| BC-07 | `entry.title` is never modified by setup/unload/reload | F7-08, F5-09 |
| BC-08 | `entry.unique_id` (the MAC) is never modified | F7-08 |
| BC-09 | Sensor `unique_id == f"{mac}_{SENSOR_TYPES[id]['key']}"` for all 8 ids | F7-01, F7-17 |
| BC-10 | Switch `unique_id == f"{mac}_buzzer"` | F7-01, F7-17 |
| BC-11 | `SENSOR_TYPES` ids, `key`, `name`, `unit`, `device_class`, `icon` unchanged (incl. **TVOC unit `µg/m³` with a raw/direct value — do NOT "fix" it to mg/m³**) | F7-17 |
| BC-12 | `_attr_has_entity_name = True`, `_attr_state_class = MEASUREMENT`, precisions 2/1/3/0 unchanged | F7-13 |
| BC-13 | Device registry `identifiers == {(DOMAIN, mac)}`, `connections == {("mac", mac)}` | F7-04 |
| BC-14 | Device name resolution: BLE-read name if available, else `entry.title` | F5-06..F5-09 |
| BC-15 | The 6 service names and their schemas are unchanged | F7-18, F5-15 |
| BC-16 | Setup succeeds when the device is unreachable (entry state LOADED, entities unavailable) | F5-01 |
| BC-17 | Sensors keep serving the last valid value for < 20 min, then go unavailable | F4-08..F4-10 |
| BC-18 | Negative filter applies to exactly `{0x06, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13}` | F3-08..F3-11 |
| BC-19 | Value formulas unchanged, incl. integer-typed results for direct sensors | F3-01..F3-07 |
| BC-20 | `EM1003Device` keeps public names `read_all_sensors`, `read_buzzer_state`, `set_buzzer_state`, `disconnect`, `sensor_data`, `buzzer_state`, `mac_address`, `device_name` | F7-14 |
| BC-21 | `device.py` imports `establish_connection` by that name and the HA bluetooth module as `bluetooth`; **every** BLE connection of the integration goes through `device.py` | F5-16, F5-17 |
| BC-22 | A legacy lowercase-MAC entry still sets up and reuses its entities | F7-15 |

### 1.4 Accepted, deliberate deltas (user-visible but safe)

* Sensors that report a filtered negative value now report *no new value* for that poll (previously the
  previous value was re-reported as if fresh, so `data_age_seconds` lied). The entity-level 20-minute
  cache keeps the state string identical; only `data_age_seconds` / `last_update` now tell the truth.
* The buzzer switch no longer connects at startup; it shows `unknown` until the first successful poll
  (at most one scan interval).
* `manifest.json`: `dependencies: ["bluetooth_adapters"]`, `version: "1.1.0"`,
  `documentation: "https://github.com/fooling/em1003"`. `requirements`, `bluetooth` matchers, `domain`,
  `name`, `config_flow`, `integration_type`, `iot_class` are unchanged.
* `const.VERSION` is bumped to `"1.1.0"` to match the manifest. Tests assert
  `const.VERSION == manifest["version"]`, never a hard-coded `"1.0.3"`.
* `strings.json` gains the `options` block that `translations/en.json` already has (additive).
* **Raised Home Assistant floor (review round).** The refactor needs
  `DataUpdateCoordinator(config_entry=...)` (HA 2024.11+) and the base `OptionsFlow.config_entry`
  property (HA 2024.12+), so `hacs.json`'s `homeassistant` key goes from `"2023.1.0"` to
  `"2024.12.0"`. HACS then refuses to offer 1.1.0 to a core that cannot run it, and those users
  keep their working 1.0.3 install untouched. Also recorded in the README install sections.

---

## 2. Module layout

```
custom_components/em1003/
  const.py        constants only (protocol, sensors, timeouts, breaker tuning)   [device implementer]
  device.py       EM1003Device, CircuitBreaker, session machinery, pure parsers  [device implementer]
  coordinator.py  EM1003DataUpdateCoordinator                                     [HA-side implementer]
  __init__.py     setup/unload/services/device-name/options listener              [HA-side implementer]
  sensor.py       8 sensor entities (logic unchanged, import moved)               [HA-side implementer]
  switch.py       buzzer switch (no own connection)                               [HA-side implementer]
  config_flow.py  config + options flow                                           [HA-side implementer]
```

Import direction is strictly `const.py` ← `device.py` ← `coordinator.py` ← `__init__.py` / `sensor.py` /
`switch.py`. `config_flow.py` imports `const` and (lazily, inside the function) `__init__.async_read_device_name`.
No cycles.

---

## 3. `const.py` — additions

Everything that exists today stays, byte-for-byte, except `VERSION`. New symbols:

```python
# ---- additive config-entry data key (never required; old entries lack it) ----
CONF_DEVICE_NAME = "device_name"

# ---- BLE timing budget, seconds. Tests shrink these by monkeypatching THIS module. ----
BLE_CONNECT_TIMEOUT = 20.0        # bound on establish_connection()
BLE_START_NOTIFY_TIMEOUT = 10.0   # bound on client.start_notify()
BLE_WRITE_TIMEOUT = 5.0           # bound on client.write_gatt_char()
BLE_RESPONSE_TIMEOUT = 2.0        # bound on waiting for one notification
BLE_READ_CHAR_TIMEOUT = 10.0      # bound on client.read_gatt_char() (device name, debug services)
BLE_STOP_NOTIFY_TIMEOUT = 5.0     # bound on client.stop_notify()
BLE_DISCONNECT_TIMEOUT = 10.0     # bound on client.disconnect()
BLE_SESSION_TIMEOUT = 60.0        # overall deadline: connect + body of ONE session
                                  # (review round: was 120.0 — worst legitimate poll is
                                  #  connect 20s + 9 x (write+response ~2s + 0.3s pacing) ~ 41s)
BLE_UNLOAD_DISCONNECT_TIMEOUT = 10.0  # bound on the unload-time disconnect (review round)
BLE_RECONNECT_GUARD = 2.0         # min seconds between our disconnect and our next connect
BLE_INTER_REQUEST_DELAY = 0.3     # pause between consecutive sensor requests
BLE_BUZZER_SETTLE_DELAY = 0.1     # pause between buzzer SET and the verify QUERY

# ---- circuit breaker tuning ----
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_OPEN_DURATION = 60.0
CIRCUIT_MAX_OPEN_DURATION = 3600.0

# ---- protocol / bookkeeping ----
NEGATIVE_FILTER_SENSOR_IDS = (0x06, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13)
SEQ_ID_QUARANTINE = 16            # recently-completed seq ids that allocation avoids

VERSION = "1.1.0"                 # must equal manifest.json "version"
```

`DEVICE_TIMEOUT = 30.0` stays (unused, kept so nothing importing it breaks).

### 3.1 How timeouts are looked up (mandatory)

`device.py` must do **both**:

```python
from . import const                      # for every timing / tuning value
from .const import (                     # for protocol values that never change at runtime
    CMD_READ_SENSOR, CMD_BUZZER, CMD_BUZZER_SET_RESPONSE, BUZZER_ON, BUZZER_OFF,
    DEVICE_NAME_UUID, EM1003_NOTIFY_CHAR_UUID, EM1003_WRITE_CHAR_UUID,
    NEGATIVE_FILTER_SENSOR_IDS, SENSOR_TYPES,
)
```

Every timing value is read **at call time** as `const.BLE_CONNECT_TIMEOUT`, never captured in a default
argument, a module-level alias, or `self.__init__`. Tests shrink them with

```python
monkeypatch.setattr(em1003_const, "BLE_CONNECT_TIMEOUT", 0.05)
```

(the shared `tests/conftest.py` exposes a fixture that shrinks the whole set at once; tests may also
monkeypatch individual values on top of it).

---

## 4. `device.py` — contract

### 4.1 Pure helpers (no I/O, no state; import-safe)

```python
def parse_sensor_value(sensor_id: int, value_bytes: bytes | bytearray) -> float | int | None
```
* `len(value_bytes) < 2` → `None` (no raise).
* `raw = int.from_bytes(value_bytes[:2], "little")`.
* `0x01` → `(raw - 4000) / 100.0` (float). `0x06` → `raw / 100.0` (float).
  `0x0A` → `(raw - 16384) / 1000.0` (float). **Every other id → `raw` unchanged (`int`).**
* Never raises for any input of any length.

```python
def apply_negative_filter(sensor_id: int, value: float | int | None) -> float | int | None
```
* Returns `None` when `sensor_id in NEGATIVE_FILTER_SENSOR_IDS and value is not None and value < 0`.
* Otherwise returns `value` unchanged. `0x01` (temperature) is never filtered.

```python
def build_sensor_request(seq_id: int, sensor_id: int) -> bytes      # bytes([seq, 0x06, sensor_id])
def build_buzzer_query(seq_id: int) -> bytes                        # bytes([seq, 0x50, 0x00])
def build_buzzer_set(seq_id: int, turn_on: bool) -> bytes           # bytes([seq, 0x50, 0x01, 0x01|0x00])
def match_key(data: bytes | bytearray) -> tuple[int, int] | None
```
`match_key` returns the `(seq_id, key)` a notification belongs to, or `None` when the frame is not a
response we understand:
* `len(data) < 3` → `None`.
* `data[1] == 0x05` (buzzer set ack) → `(data[0], 0x01)`.
* `data[1] == 0x50` (buzzer query) → `(data[0], data[2])`.
* `data[1] == 0x06` (sensor) → `(data[0], data[2])`.
* anything else → `None`.

### 4.2 `PendingRequest`

```python
@dataclass
class PendingRequest:
    seq_id: int
    sensor_id: int            # the match key: sensor id, 0x00 (buzzer query), 0x01 (buzzer set)
    future: asyncio.Future
    timestamp: float          # monotonic, from the device clock
```

### 4.3 `EM1003PollResult`

```python
@dataclass(frozen=True)
class EM1003PollResult:
    sensors: dict[int, float | int | None]   # ALWAYS exactly SENSOR_TYPES.keys(), in that order
    buzzer: bool | None
    ok: bool
    error: str | None = None

    @classmethod
    def failed(cls, error: str) -> EM1003PollResult      # all sensors None, buzzer None, ok=False
```
`ok is True` **iff** at least one sensor value is not `None`. `error` is `None` when `ok` is True.

### 4.4 `CircuitBreaker`

```python
class CircuitBreaker:
    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = const.CIRCUIT_FAILURE_THRESHOLD,
        open_duration: float = const.CIRCUIT_OPEN_DURATION,
        max_backoff: float = const.CIRCUIT_MAX_OPEN_DURATION,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None
```
Public attributes: `state: str`, `failure_count: int`, `base_open_duration: float`,
`max_backoff: float`, `current_open_duration: float`, `open_time: float | None`,
`probe_in_flight: bool`.

Methods:

| Method | Semantics |
|--------|-----------|
| `can_attempt() -> tuple[bool, str]` | Admission decision **with side effects** (see table below). The string is human-readable and never asserted on verbatim by tests (only `.lower()` substrings such as `"open"`, `"half"`). |
| `record_success() -> None` | `state=CLOSED`, `failure_count=0`, `open_time=None`, `current_open_duration=base_open_duration`, `probe_in_flight=False`. |
| `record_failure() -> None` | see table. |
| `reset() -> None` | Same effect as `record_success()`, no logging. Test convenience. |
| `get_state_info() -> str` | Human-readable; must not raise in any state, including `state=OPEN, open_time=None`. |

Exact transitions (`t = clock()`):

| From | `can_attempt()` | effect |
|------|-----------------|--------|
| CLOSED | `(True, "closed")` | none |
| OPEN, `t - open_time < current_open_duration` | `(False, "circuit open (<remaining>s remaining)")` | **none** (rejections never count) |
| OPEN, `t - open_time >= current_open_duration` | `(True, "half-open probe")` | `state=HALF_OPEN`, `probe_in_flight=True`; `failure_count` **not** reset |
| OPEN, `open_time is None` (defensive) | `(True, "circuit reset")` | `state=CLOSED`, `failure_count=0` |
| HALF_OPEN, `probe_in_flight` | `(False, "half-open probe in flight")` | none |
| HALF_OPEN, not `probe_in_flight` | `(True, "half-open probe")` | `probe_in_flight=True` |

| From | `record_failure()` effect |
|------|---------------------------|
| CLOSED | `failure_count += 1`; if `failure_count >= failure_threshold` → `state=OPEN`, `open_time=t`, `current_open_duration = base_open_duration` |
| HALF_OPEN | `failure_count += 1`, `state=OPEN`, `open_time=t`, `current_open_duration = min(current_open_duration * 2, max_backoff)`, `probe_in_flight=False` |
| OPEN (defensive; only reachable via a bypassing caller) | `failure_count += 1` only — `open_time` and `current_open_duration` untouched |

Contract for callers: **every admitted call reports exactly one outcome** (`record_success()` xor
`record_failure()`). A rejected call reports nothing. There is no separate 30-second fast-fail window
anywhere — `_last_connection_failure_time` and `_fast_fail_window` are deleted.

### 4.5 `EM1003Device`

```python
class EM1003Device:
    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        device_name: str | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None
```
Positional compatibility with v1.0.3 is preserved. When `circuit_breaker is None` the device creates
`CircuitBreaker(clock=clock)`.

Public attributes / properties:

| Name | Type | Notes |
|------|------|-------|
| `hass` | `HomeAssistant` | |
| `mac_address` | `str` | as given, never normalised |
| `device_name` | `str` | falls back to `mac_address`; writable by `__init__.py` after name resolution |
| `sensor_data` | `dict[int, float \| int \| None]` | last **valid** value per sensor id; a filtered/absent reading leaves the old entry untouched |
| `buzzer_state` | `bool \| None` | last known buzzer state |
| `circuit_breaker` | `CircuitBreaker` | read-only property returning `self._circuit_breaker` |
| `is_busy` | `bool` | read-only property: `self._lock.locked()` |
| `_lock` | `asyncio.Lock` | test-relevant private; created in `__init__`, one per device |
| `_client` | `BleakClient \| None` | test-relevant private; `None` outside a session, **always** `None` after any session |

Public coroutines:

```python
async def async_poll(self) -> EM1003PollResult
async def read_all_sensors(self) -> dict[int, float | int | None]
async def read_buzzer_state(self) -> bool | None
async def set_buzzer_state(self, turn_on: bool) -> bool
async def async_read_device_name(self) -> str | None
async def async_with_client(self, func, *, notify: bool = False,
                            timeout: float | None = None,
                            use_breaker: bool = False) -> Any
async def disconnect(self) -> None
```

**None of these raise** except `async_with_client` (see below). All of them are serialised by the same
`asyncio.Lock`. `read_sensor()` is **deleted**.

| Method | Success | Failure | Breaker |
|--------|---------|---------|---------|
| `async_poll()` | `EM1003PollResult(ok=True, ...)`; `sensors` always has all 8 keys | `EM1003PollResult.failed(msg)` — never raises, not even on `BleakError`, `TimeoutError` or a missing BLE device | checked; exactly one `record_success()` (≥1 sensor read) or `record_failure()` (0 sensors read / connect / notify failure); rejected → `failed("circuit open…")` **with zero calls to `establish_connection`** and nothing recorded |
| `read_all_sensors()` | `dict` with all 8 keys | same dict, all values `None` — never raises | identical to `async_poll` (it *is* a poll without the buzzer step) |
| `read_buzzer_state()` | `True`/`False` | `None` — never raises | checked & recorded like a poll (success iff a buzzer frame was parsed) |
| `set_buzzer_state(turn_on)` | `True` only when the SET was acked **and** the follow-up QUERY reports `buzzer_state == turn_on` | `False` — never raises | **bypasses the gate** (see 4.6) |
| `async_read_device_name()` | stripped UTF-8 `str` (may be `""` → then returns `None`) | `None` — never raises | **not checked, not recorded** (a startup name read must never poison the poll breaker) |
| `async_with_client(func, …)` | whatever `func(client)` returns | raises `BleakError` if the link cannot be made; re-raises whatever `func` raised, *after* the link is closed | `use_breaker=False` by default: not checked, not recorded |
| `disconnect()` | `None` | `None` — never raises | untouched |

`disconnect()` takes the lock, so it waits for any in-flight session and then closes a lingering link
(there should be none). **It must never be called from inside a session** (that would deadlock);
`__init__.async_unload_entry` calls it only after `coordinator.async_shutdown()`.

`async_with_client` signature detail: `func` is `Callable[[BleakClient], Awaitable[Any]]`. Used by the
debug services so that *all* integration BLE traffic goes through this one patch point (BC-21).

### 4.6 `set_buzzer_state` while the breaker is OPEN / HALF_OPEN

A buzzer toggle is a user-initiated action; silently returning `False` for up to an hour would leave
the user with no recovery path. Rule:

1. `admitted, reason = breaker.can_attempt()`.
2. The operation **runs regardless of `admitted`** (it never consumes the HALF_OPEN probe slot when
   `admitted is False`).
3. Outcome reporting:
   * `admitted is True` → normal: `record_success()` or `record_failure()`.
   * `admitted is False` (bypassed) → on success `record_success()` (the device is clearly back, so the
     breaker closes and the backoff resets); on failure **nothing is recorded** — a bypassed call can
     only help the breaker, never extend the outage.

### 4.7 The session — the single place a link is opened

All BLE work happens inside one internal helper. Required behaviour, in order:

1. `async with self._lock:` — everything below is inside. Nothing else in the integration ever touches
   `self._client`.
2. Breaker gate (when the caller asks for it). Rejected → return the caller's "rejected" value without
   any BLE call.
3. **Reconnect guard** (monotonic): if `self._last_disconnect_time is not None` and
   `self._clock() - self._last_disconnect_time < const.BLE_RECONNECT_GUARD`, `await asyncio.sleep(remaining)`.
   `time.time()` must not appear anywhere in `device.py`.
4. `dev = bluetooth.async_ble_device_from_address(self.hass, self.mac_address, connectable=True)`.
   Falsy → raise/return failure `"Device not found: <mac>"` **without** calling `establish_connection`.
5. Connect, bounded:
   ```python
   async with asyncio.timeout(const.BLE_CONNECT_TIMEOUT):
       client = await establish_connection(
           BleakClient, dev, self.mac_address,
           disconnected_callback=self._handle_disconnect,
           max_attempts=1,
           timeout=const.BLE_CONNECT_TIMEOUT,
       )
   ```
   `max_attempts=1` always — including the device-name read (D4).
6. `self._client = client` (for introspection only). **Teardown always targets the local `client`
   variable, never `self._client`.**
7. If the operation needs notifications:
   `async with asyncio.timeout(const.BLE_START_NOTIFY_TIMEOUT): await client.start_notify(EM1003_NOTIFY_CHAR_UUID, self._notification_handler)`.
   A failure here goes straight to teardown (the link is still closed).
8. Body: steps 3–7 plus the operation body run inside one
   `async with asyncio.timeout(const.BLE_SESSION_TIMEOUT)`. Teardown is **outside** that deadline and
   gets its own fresh timeouts, so a blown deadline still closes the link.
9. `finally:` teardown, which must complete **even when the awaiting task is cancelled**:
   * fail every remaining `PendingRequest` with `BleakError("session ended")` and release its seq id;
   * if notifications were started: `stop_notify` under `const.BLE_STOP_NOTIFY_TIMEOUT`, all exceptions
     swallowed;
   * `client.disconnect()` under `const.BLE_DISCONNECT_TIMEOUT`, all exceptions swallowed; on timeout
     log a warning and simply drop the reference;
   * `self._client = None` and `self._last_disconnect_time = self._clock()` — **unconditionally**.

   Recommended shape (cancellation-safe):
   ```python
   finally:
       task = self.hass.async_create_task(self._async_teardown(client, notify_started))  # or ensure_future
       try:
           await asyncio.shield(task)
       except asyncio.CancelledError:
           await asyncio.wait({task})
           raise
   ```
10. The lock is released when the `async with` block exits — i.e. after teardown, in every path.

Invariant the tests assert directly: **after any public coroutine returns or raises, the fake world
reports zero open links and `device._lock.locked() is False`.**

### 4.8 Request / response machinery

```python
async def _async_request(self, client, payload: bytes, key: int,
                         *, timeout: float | None = None) -> bytes | None
```
* Allocates a seq id (caller passes a payload already built with it — the helper receives the seq id
  as `payload[0]`), registers `PendingRequest`, writes:
  `async with asyncio.timeout(const.BLE_WRITE_TIMEOUT): await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, payload, response=False)`.
* Waits `asyncio.wait_for(pending.future, timeout or const.BLE_RESPONSE_TIMEOUT)`.
* Returns the raw notification `bytes` on success; returns `None` on response timeout (after removing
  the pending entry and releasing the seq id).
* Raises `BleakError` when the write fails or times out, or when the disconnect callback failed the
  future.

Sequence ids:
* `_alloc_seq_id() -> int` picks uniformly at random from
  `set(range(256)) - {in-flight seq ids} - set(self._recent_seq_ids)`.
  If that set is empty it falls back to `set(range(256)) - {in-flight}`; if *that* is empty it raises
  `BleakError("no free sequence id")`.
* `_release_seq_id(seq)` removes it from in-flight and appends it to
  `self._recent_seq_ids: deque[int] = deque(maxlen=const.SEQ_ID_QUARANTINE)`.
* Released on response, on response timeout, on write error, and on session teardown.
* The old "clear the whole cache at 250 entries" logic is deleted; `_used_seq_ids` now tracks
  **in-flight only**.

Notification handler (`_notification_handler(sender, data)`, sync, never raises):
* `key = match_key(data)`; `None` → log debug, return.
* `pending = self._pending_requests.get(key)`; missing → log debug ("unexpected response"), return.
  **It must never complete a different pending request.**
* For a `0x50` buzzer query frame with `len(data) >= 4`: `self.buzzer_state = (data[3] == BUZZER_ON)`.
  Shorter frame: leave `buzzer_state` untouched.
* Sensor parsing is **not** done here; the raw frame is handed to the waiter via
  `future.set_result(bytes(data))` and the caller uses `parse_sensor_value` / `apply_negative_filter`.
* Pops the pending entry and releases the seq id.
* The whole body is wrapped in `try/except Exception` and logs; it never propagates.

Disconnect callback (`_handle_disconnect(client)`, sync):
* Ignores the event when `client is not self._client` (a stale link).
* Sets `self._last_disconnect_time = self._clock()`.
* Fails **every** pending future immediately with `BleakError("device disconnected")` (guarding
  `future.done()`), and releases their seq ids. This is what turns a mid-poll disconnect from
  "8 × 2 s of timeouts" into "return within milliseconds".

### 4.9 `async_poll` body (exact)

```
sensors = {sid: None for sid in SENSOR_TYPES}        # all 8 keys, in SENSOR_TYPES order
buzzer  = None
session(notify=True, breaker=checked):
    for i, sid in enumerate(SENSOR_TYPES):
        if not client.is_connected: break            # remaining stay None
        try:
            frame = await self._async_request(client, build_sensor_request(seq, sid), key=sid)
        except BleakError:
            break                                    # remaining stay None, fast
        if frame is not None:
            value = apply_negative_filter(sid, parse_sensor_value(sid, frame[3:]))
            if value is not None:
                self.sensor_data[sid] = value
            sensors[sid] = value                     # None for filtered/short frames
        if i < len(SENSOR_TYPES) - 1:
            await asyncio.sleep(const.BLE_INTER_REQUEST_DELAY)
    # buzzer, SAME connection, best effort:
    try:
        frame = await self._async_request(client, build_buzzer_query(seq2), key=0x00)
        buzzer = self.buzzer_state if frame is not None else None
    except BleakError:
        buzzer = None                                # never fails the poll, never counts a breaker failure
```
`read_all_sensors()` runs the identical loop with the buzzer step skipped and returns `sensors`.

`set_buzzer_state(turn_on)` in one session (`notify=True`):
`build_buzzer_set` → wait for the `0x05` ack → `await asyncio.sleep(const.BLE_BUZZER_SETTLE_DELAY)` →
`build_buzzer_query` → wait → `return self.buzzer_state == turn_on`.

---

## 5. `coordinator.py` — contract

```python
class EM1003DataUpdateCoordinator(DataUpdateCoordinator[dict[int, float | int | None]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 device: EM1003Device, update_interval: timedelta) -> None
```
* Calls `super().__init__(hass, _LOGGER, config_entry=entry, name=f"EM1003 {device.mac_address}", update_interval=update_interval)`.
* Attributes: `device` (and the alias `em1003_device = device` for compatibility),
  `mac_address: str`, `buzzer_state: bool | None = None`, `last_poll: EM1003PollResult | None = None`.

```python
async def _async_update_data(self) -> dict[int, float | int | None]
```
* `result = await self.device.async_poll()`; store `self.last_poll = result`.
* `if result.buzzer is not None: self.buzzer_state = result.buzzer` (a failed buzzer query never wipes
  the last known state).
* `if not result.ok: raise UpdateFailed(result.error or "no sensor data from <mac>")`.
* else `return result.sensors`.
* It must **not** wrap `async_poll` in a broad `try/except` — `async_poll` never raises. Exactly one
  `UpdateFailed` path.

`sensor.py` re-exports the class (`from .coordinator import EM1003DataUpdateCoordinator  # noqa: F401`)
so the old import path `custom_components.em1003.sensor.EM1003DataUpdateCoordinator` keeps working.

---

## 6. `__init__.py` — contract

### 6.1 `async_setup_entry(hass, entry) -> bool`, in this exact order

1. `mac = entry.data[CONF_MAC_ADDRESS]`
2. `device = EM1003Device(hass, mac)`
3. Name resolution
   * `cached = entry.data.get(CONF_DEVICE_NAME)` → if truthy: `name = cached`, **zero BLE connections**.
   * else `name = await device.async_read_device_name()`; on success
     `hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICE_NAME: name})`
     (additive only; `title`, `options`, `unique_id`, `version` untouched).
   * on failure `name = entry.title`, and **nothing is written to `entry.data`** (so the next start
     retries once).
   * `device.device_name = name`.
4. `dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, mac)}, connections={("mac", mac)}, name=name, manufacturer="EM1003", model="BLE Air Quality Sensor")`
5. `interval = timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))`
   `coordinator = EM1003DataUpdateCoordinator(hass, entry, device, interval)`
6. `hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {CONF_MAC_ADDRESS: mac, "name": entry.title, "device_name": name, "device": device, "coordinator": coordinator}`
   — the key names are exactly today's, plus `"coordinator"` now written **here**, before platforms.
7. `await coordinator.async_refresh()` — **not** `async_config_entry_first_refresh()`; a failed first
   poll must never abort setup (BC-16). No `asyncio.sleep(2)`.
8. `entry.async_on_unload(entry.add_update_listener(_async_update_listener))`
9. `_async_register_services(hass)` — guarded by `if hass.services.has_service(DOMAIN, SERVICE_SCAN_DEVICE): return`, so N entries register the 6 services once.
10. `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`
11. `return True`

```python
async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
```

### 6.2 `async_unload_entry(hass, entry) -> bool`

1. `unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)`
2. if `unload_ok`:
   * `data = hass.data[DOMAIN].pop(entry.entry_id, None)`
   * `await data["coordinator"].async_shutdown()`
   * `await data["device"].disconnect()` (closes any lingering link; safe no-op when idle),
     wrapped in `asyncio.timeout(const.BLE_UNLOAD_DISCONNECT_TIMEOUT)` with `TimeoutError`
     suppressed — review round: `disconnect()` waits on the device lock, which an in-flight poll
     against an out-of-range device holds for its whole session budget
   * if `not hass.data[DOMAIN]`: `hass.data.pop(DOMAIN, None)`. The 6 debug services are **not**
     removed (review round: they are registered once and kept for the life of HA, as in v1.0.3)
3. `return unload_ok`

### 6.3 Device lookup + services

```python
def _async_device_for_mac(hass: HomeAssistant, mac_address: str) -> EM1003Device
```
Returns the configured entry's `EM1003Device` when some
`hass.data[DOMAIN][*]["device"].mac_address.upper() == mac_address.upper()`, otherwise a fresh
`EM1003Device(hass, mac_address)` (transient, its own lock). This is what makes a debug service
serialise with an in-flight poll on the same MAC.

```python
async def async_read_device_name(hass: HomeAssistant, mac_address: str) -> str | None
```
Kept at module level (config_flow imports it): `return await _async_device_for_mac(hass, mac).async_read_device_name()`.
`BleakScanner` must not be imported anywhere in `__init__.py`.

The 6 handlers (names, schemas and log-only behaviour unchanged):

| Service | New implementation |
|---------|--------------------|
| `scan_device` | `bluetooth.async_ble_device_from_address` + **`bluetooth.async_last_service_info(hass, mac, connectable=True)` for RSSI**; when not found, lists `bluetooth.async_discovered_service_info(hass)` (name/address/`si.rssi`). **No `BleakScanner`, no `BLEDevice.rssi`.** Opens no link. |
| `discover_all` | `await device.async_with_client(fn)`; `fn` walks `client.services`, reads readable characteristics, logs |
| `list_services` | `await device.async_with_client(fn)`; logs service uuids/descriptions/char counts |
| `read_characteristic` | `await device.async_with_client(fn)`; `read_gatt_char(uuid)` bounded by `const.BLE_READ_CHAR_TIMEOUT` |
| `write_characteristic` | parses `data` (hex string / utf-8 string / `list[int]`) **before** connecting, then `await device.async_with_client(fn)` |
| `read_device_name` | `await device.async_read_device_name()` |

Every handler catches `Exception`, logs, and returns `None` — a service call must never raise.

---

## 7. `sensor.py` / `switch.py` — contract

### 7.1 `sensor.py`

* `async_setup_entry` now only reads `coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]`
  (guaranteed present) and adds the 8 entities. No coordinator construction, no `asyncio.sleep(2)`,
  no first refresh.
* `EM1003Sensor` is **unchanged**: same `unique_id`, name, icon, unit, device-class mapping,
  precisions, `device_info`, `available`, `native_value` (20-minute cache, `datetime.now()`),
  `extra_state_attributes`.

### 7.2 `switch.py`

```python
class EM1003BuzzerSwitch(CoordinatorEntity[EM1003DataUpdateCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    # unique_id f"{mac}_buzzer", name "Buzzer", icon "mdi:volume-high", same device_info
```
* Constructed with `(coordinator, config_entry, mac_address, device_name, device)`.
* `async_added_to_hass()` opens **no** BLE link. It seeds `self._attr_is_on = device.buzzer_state`
  (usually `None` → HA state `unknown`) and then defers to `CoordinatorEntity`.
  The `_coordinator_listener` attribute is deleted; `async_will_remove_from_hass` is not overridden.
* `_handle_coordinator_update()`: if `self.coordinator.buzzer_state is not None` →
  `self._attr_is_on = self.coordinator.buzzer_state`, `self._attr_available = True`,
  `self.async_write_ha_state()`.
* `available` property: `return self._attr_available` (initial value `True`). It is set `False` only by
  a failed set command and back to `True` by the next coordinator update carrying a buzzer state.
  It deliberately does **not** follow `coordinator.last_update_success`.
* `async_turn_on/async_turn_off`:
  ```python
  ok = await self.coordinator.device.set_buzzer_state(True/False)
  if ok:  self._attr_is_on = True/False; self._attr_available = True; self.async_write_ha_state()
  else:   self._attr_available = False;  self.async_write_ha_state()
          raise HomeAssistantError(f"Failed to turn {'on' if … else 'off'} EM1003 buzzer {mac}")
  ```
* `extra_state_attributes` unchanged (`{"mac_address": mac}`).

---

## 8. `config_flow.py` / `manifest.json` / `strings.json`

* `EM1003OptionsFlow.__init__` is **removed**. `async_get_options_flow(config_entry)` stays a
  `@staticmethod` taking the entry and returns `EM1003OptionsFlow()`. The flow uses the inherited
  `self.config_entry` property. Plain `OptionsFlow` + an entry update listener (section 6.1 step 8) —
  **not** `OptionsFlowWithReload` (HA raises `ValueError` if both are used).
* `async_step_init` validation unchanged: `vol.All(vol.Coerce(int), vol.Range(min=10, max=600))`,
  default = current option or 60, `async_create_entry(title="", data=user_input)`.
* `async_step_user`: `mac = user_input[CONF_MAC_ADDRESS].strip().upper().replace("-", ":")`;
  `await self.async_set_unique_id(mac)`; `self._abort_if_unique_id_configured()`; **plus** a
  case-insensitive scan over `self._async_current_entries()` on `entry.data[CONF_MAC_ADDRESS].upper()`
  → `self.async_abort(reason="already_configured")` (protects legacy lowercase entries, BC-22).
  Title logic unchanged: `f"{name} ({mac[-8:]})"` on a successful name read, else `f"EM1003 ({mac})"`.
* `async_step_bluetooth`: unchanged except that it runs the same case-insensitive scan after
  `_abort_if_unique_id_configured()` (review round — the scan is factored into
  `_mac_already_configured(mac)` and shared by both steps; discovery fires without user intent, so
  a legacy lowercase entry was being duplicated there). `async_step_bluetooth_confirm` unchanged.
* `manifest.json`: `dependencies: ["bluetooth_adapters"]`, `version: "1.1.0"`,
  `documentation: "https://github.com/fooling/em1003"`; everything else byte-identical.
* `strings.json`: add the `options.step.init` block mirroring `translations/en.json`.

---

## 9. Defect → observable behaviour (logging-independent)

| D | Observable contract a test can assert |
|---|--------------------------------------|
| **D1** | With N concurrent mixed operations on one device: `establish_connection` is never entered twice concurrently; the fake world's concurrent-open-link count never exceeds 1; after all settle, open links == 0 and `device._client is None`. |
| **D2** | With every timeout shrunk to ≤ 0.05 s and the fake hanging at connect / start_notify / write / response / stop_notify / disconnect, each public coroutine returns within ~1 s wall clock, `device._lock.locked() is False` afterwards, and the *next* operation against a healthy fake succeeds. |
| **D3** | Unit level: see §4.4 tables. Device level: exactly one breaker outcome per operation; a breaker-rejected `async_poll` performs 0 `establish_connection` calls and leaves `failure_count` unchanged; a buzzer-only failure inside a good poll leaves `failure_count == 0`. |
| **D4** | One `async_poll` → exactly 1 `establish_connection` call and exactly 1 link opened/closed, covering 8 sensor frames **and** the buzzer frame. Full `async_setup_entry` with a cached `device_name` and a healthy fake → exactly 1 connection in total (the first poll). Without a cached name → exactly 2 (name read + first poll), and the name read uses `max_attempts=1`. |
| **D5** | `manifest.json["dependencies"] == ["bluetooth_adapters"]`. |
| **D6** | `hass.config_entries.options.async_init(entry.entry_id)` returns a form (no `AttributeError`); submitting `{"scan_interval": 120}` stores it and the entry is reloaded, after which `coordinator.update_interval == timedelta(seconds=120)` without a HA restart. |
| **D7** | `BLEDevice.rssi` is never accessed (the fake's `BLEDevice` has no `rssi` and any access raises); `BleakScanner` is not imported by `__init__.py` or `device.py`; RSSI comes from `bluetooth.async_last_service_info`. |
| **D8** | `read_sensor` is absent from `EM1003Device`; `time.time()` does not appear in `device.py`; a filtered negative reading yields `None` in the poll result while `device.sensor_data[sid]` keeps the previous valid value; unload disconnects and pops `hass.data`; services exist once for two entries and survive every unload (review round: they are never removed); the switch has no `_coordinator_listener` attribute; a mid-session disconnect fails pending futures immediately. |

---

## 10. Test case matrix

Conventions for every table below:
* The fake BLE world (`tests/fakes.py`, written concurrently) is patched in at
  `custom_components.em1003.device.establish_connection`, and the HA BLE device lookup at
  `custom_components.em1003.device.bluetooth.async_ble_device_from_address`. Tests refer to it only
  through the fixtures `conftest.py` exposes (a world handle with per-sensor values, injectable
  hangs/errors at named await points, a concurrent/total open-link counter, and a record of every
  frame written). **No test may assume its internals beyond that.**
* "timeouts shrunk" = the conftest `fast_timeouts` fixture, which reduces every `BLE_*TIMEOUT*`
  constant in `custom_components.em1003.const` to 0.05 s (0.2 s for `BLE_SESSION_TIMEOUT`).
  **Amendment (integrator, round 1):** the *pacing* constants (`BLE_INTER_REQUEST_DELAY`,
  `BLE_BUZZER_SETTLE_DELAY`, `BLE_RECONNECT_GUARD`) are **not** part of that 0.05 s sweep.
  They are shrunk for the whole suite by an autouse `fast_ble_pacing` fixture to
  0.005 / 0.005 / 0.05 s. Two reasons: (a) shrinking them all to 0.05 s made a *healthy*
  8-sensor sweep cost `7 × 0.05 = 0.35 s` of pure pacing and therefore always blow the 0.2 s
  session deadline — a scaffold artefact, not a behaviour the plan asks for; (b) at their
  production values (0.3 s / 0.1 s / 2.0 s) every polling test burned ~4 s of wall-clock sleep,
  which put the whole suite at ~15 minutes instead of the required "well under 2 minutes".
  These constants only space requests out on the wire, so no assertion in the matrix depends on
  their production magnitude. A test that needs different values monkeypatches them itself
  (test-local monkeypatching always wins over the autouse fixture).
* `MAC = "AA:BB:CC:DD:EE:FF"` unless stated otherwise.
* "links: 0 after" always means: the world reports 0 open links **and** `device._client is None`
  **and** `device._lock.locked() is False`.

### F1 — Session, lock, timeouts, no leaks → `tests/test_device_session.py` (22 cases)

| ID | Scenario | Fake setup | Expected |
|----|----------|-----------|----------|
| F1-01 | Happy poll opens exactly one link | healthy world, all 8 sensors + buzzer answer | `async_poll()` → `ok is True`, 8 values, `buzzer is not None`; `establish_connection` called once; max concurrent links 1; total links opened 1; links: 0 after |
| F1-02 | Poll uses one connection for sensors **and** buzzer | healthy | frames written in the session: 8 × `[seq,0x06,sid]` then 1 × `[seq,0x50,0x00]`, all on the *same* client instance |
| F1-03 | Lock serialises N mixed operations | healthy, each op takes ≥ 5 ms | `asyncio.gather(poll, read_buzzer_state, set_buzzer_state(True), read_all_sensors, poll)` → world's max concurrent links == 1, `establish_connection` never re-entered while another call is in flight, all 5 results well-formed, links: 0 after |
| F1-04 | 20 concurrent `read_buzzer_state` | healthy | all return the same bool, max concurrent links 1, links: 0 after |
| F1-05 | Hang at connect | `establish_connection` never returns; timeouts shrunk | `async_poll()` returns `EM1003PollResult.failed(...)` within 1 s; `ok is False`; links: 0 after |
| F1-06 | Hang at `start_notify` | connect OK, `start_notify` hangs | poll returns failed within 1 s; the client **was** disconnected (world total closed == total opened); links: 0 after |
| F1-07 | Hang at `write_gatt_char` | first write hangs | poll returns within 1 s with all sensors `None`, `ok is False`; links: 0 after |
| F1-08 | No response to a write | write succeeds, no notification ever | poll completes; that sensor is `None`; remaining sensors still attempted; links: 0 after |
| F1-09 | Hang at `stop_notify` | teardown `stop_notify` hangs | poll still returns its data within 1 s, `ok is True`; disconnect still happened; links: 0 after |
| F1-10 | Hang at `disconnect` | teardown `disconnect` hangs | poll returns within 1 s; `device._client is None`; a warning path taken; the **next** poll (healthy) succeeds |
| F1-11 | Next operation works after every hang point | parametrised over the 6 hang points of F1-05..F1-10, hang cleared afterwards | second `async_poll()` → `ok is True`, exactly one new `establish_connection`, links: 0 after |
| F1-12 | `BleakError` from connect | `establish_connection` raises `BleakError` | poll → failed result (no raise), 0 links opened, links: 0 after |
| F1-13 | Unexpected exception mid-session | write raises `RuntimeError` | `async_poll` returns a failed/partial result without propagating `RuntimeError`; the link created **is** disconnected; links: 0 after |
| F1-14 | Cancellation mid-session | poll cancelled while waiting for a response | `asyncio.CancelledError` propagates out of `async_poll`; the world still shows the link closed; links: 0 after; lock released |
| F1-15 | Cancellation during connect | task cancelled while `establish_connection` is in flight | `CancelledError` propagates; no link left open; the next poll succeeds |
| F1-16 | Session deadline | every step slow enough that the shrunk `BLE_SESSION_TIMEOUT` fires mid-loop | poll returns within ~2 × deadline with partial/None values, `ok` matching whether any sensor answered; links: 0 after |
| F1-17 | Mid-poll unexpected disconnect fails pending futures fast | disconnect callback fired after sensor 3 | `async_poll` returns within ~0.3 s of the drop (not 5 × `BLE_RESPONSE_TIMEOUT`); sensors 1-3 have values, 4-8 are `None`; all 8 keys present; links: 0 after |
| F1-18 | Reconnect guard uses a monotonic clock | injected fake clock; `asyncio.sleep` recorded | after a poll, an immediate second poll sleeps `BLE_RECONNECT_GUARD - (fake_now - last_disconnect)` before connecting; advancing the fake clock past the guard removes the sleep; `time.time` is never called by `device.py` (assert via `monkeypatch` on `time.time` raising) |
| F1-19 | `set_buzzer_state` during an in-flight poll | poll started, then `set_buzzer_state(True)` | the set waits for the poll (its first frame is written after the poll's last frame), both return correct results, neither result is corrupted, max concurrent links 1, links: 0 after |
| F1-20 | `disconnect()` while a poll is in flight | poll in flight | `await device.disconnect()` returns only after the poll finished; poll result intact; links: 0 after; `disconnect()` on an idle device is a no-op that never raises |
| F1-21 | `read_all_sensors` opens one link and skips the buzzer | healthy | exactly 1 `establish_connection`; no `0x50` frame written; dict has all 8 keys; links: 0 after |
| F1-22 | 50 sequential polls leak nothing | healthy, guard shrunk | total links opened == 50, total closed == 50, `len(device._pending_requests) == 0`, `len(device._used_seq_ids) == 0` |

### F2 — Circuit breaker → `tests/test_circuit_breaker.py` (20 cases)

`clock` is an injected callable over a mutable `now`; `cb = CircuitBreaker(3, 60.0, 3600.0, clock=fake)`.

| ID | Scenario | Setup | Expected |
|----|----------|-------|----------|
| F2-01 | Fresh breaker admits | new | `state == "CLOSED"`, `can_attempt() == (True, …)`, `failure_count == 0` |
| F2-02 | Closed → open at threshold | 3 × `record_failure()` | after the 2nd: still CLOSED and admitting; after the 3rd: `state == "OPEN"`, `open_time == now`, `current_open_duration == 60.0` |
| F2-03 | Success resets before threshold | fail, fail, success | `failure_count == 0`, CLOSED |
| F2-04 | Rejected calls do not count | OPEN; call `can_attempt()` 10 × within the window | all `(False, …)`; `failure_count` unchanged; `open_time` unchanged; `state` still OPEN |
| F2-05 | Open duration honoured exactly | OPEN at t=0, dur 60 | `can_attempt()` at t=59.9 → False; at t=60.0 → True and `state == "HALF_OPEN"` |
| F2-06 | HALF_OPEN admits exactly one probe | after F2-05 | the next 5 `can_attempt()` calls all return `(False, …)` while `probe_in_flight` |
| F2-07 | Failed probe re-opens with doubled duration | HALF_OPEN probe → `record_failure()` at t=60 | `state == "OPEN"`, `open_time == 60`, `current_open_duration == 120.0`, `probe_in_flight is False` |
| F2-08 | Repeated failed probes double each time | 4 probe/failure cycles | durations 60 → 120 → 240 → 480 → 960 |
| F2-09 | Backoff capped | force `current_open_duration` near cap, fail probes | never exceeds `max_backoff == 3600.0` |
| F2-10 | Successful probe closes and resets everything | HALF_OPEN probe → `record_success()` | CLOSED, `failure_count == 0`, `open_time is None`, `current_open_duration == 60.0`, `probe_in_flight is False`, next `can_attempt()` True |
| F2-11 | `failure_count` is not zeroed on entering HALF_OPEN | 3 failures then window elapses | after `can_attempt()`: `state == "HALF_OPEN"`, `failure_count == 3` (dead-code regression guard) |
| F2-12 | Defensive OPEN with `open_time is None` | force it | `can_attempt()` → `(True, …)` and `state == "CLOSED"`; no exception |
| F2-13 | `get_state_info()` never raises | every state incl. OPEN/`open_time=None`/HALF_OPEN | returns a `str` in all cases; contains "CLOSED"/"OPEN"/"HALF_OPEN" accordingly |
| F2-14 | `reset()` | any state | CLOSED, counters zeroed, duration back to base |
| F2-15 | Clock is injectable & monotonic-based | default breaker | `CircuitBreaker().` uses `time.monotonic` (assert via monkeypatching `time.monotonic` and observing) |
| F2-16 | Device: one failed poll counts exactly **one** failure | world: connect raises `BleakError` | after `await device.async_poll()`: `failure_count == 1` (not 2), state CLOSED |
| F2-17 | Device: 3 failed polls open the breaker | same, 3 polls | `state == "OPEN"`; the 3rd poll still attempted a connection, the 4th does not |
| F2-18 | Device: breaker-rejected poll is free | breaker forced OPEN | `async_poll()` → `ok is False`, all 8 keys `None`, `buzzer is None`, `establish_connection` call count **0**, `failure_count` unchanged |
| F2-19 | Device: buzzer-only failure in a good poll does not count | sensors answer, buzzer query never answers | `ok is True`, `buzzer is None`, `failure_count == 0`, `state == "CLOSED"` |
| F2-20 | Device: `set_buzzer_state` while OPEN | breaker OPEN; (a) healthy world (b) failing world | (a) returns `True`, a connection **was** attempted, breaker now CLOSED with `failure_count == 0`; (b) returns `False`, `failure_count` and `open_time` unchanged, still OPEN; in neither case is the HALF_OPEN probe slot consumed |

### F3 — Protocol parsing & request matching → `tests/test_protocol.py` (23 cases)

| ID | Scenario | Setup | Expected |
|----|----------|-------|----------|
| F3-01 | Temperature formula & endianness | `parse_sensor_value(0x01, b"\x2c\x1a")` | `27.00` (float) |
| F3-02 | Temperature below 0 °C | raw 3900 (`b"\x3c\x0f"`) | `-1.0`; not filtered (0x01 is exempt) |
| F3-03 | Humidity | `b"\xdb\x11"` | `45.71` |
| F3-04 | Direct sensors keep `int` | 0x08 `b"\x31\x00"`, 0x09 `b"\x1c\x00"`, 0x11 `b"\x16\x00"`, 0x12 `b"\x01\x00"`, 0x13 `b"\x9b\x01"` | `49, 28, 22, 1, 411` and `isinstance(v, int)` for each (no `49.0`) |
| F3-05 | Formaldehyde baseline | 0x0A raw 16384 | `0.0` |
| F3-06 | Formaldehyde raw < 16384 → negative → filtered | raw 16000 | `parse_sensor_value` → `-0.384`; `apply_negative_filter(0x0A, -0.384) is None` |
| F3-07 | 0xFFFF boundary for every id | raw 65535 | 0x01 → `615.35`, 0x06 → `655.35`, 0x0A → `49.151`, others → `65535`; no exception |
| F3-08 | Filter set is exactly BC-18 | `apply_negative_filter(sid, -1)` for all 8 ids | `None` for `{0x06,0x08,0x09,0x0A,0x11,0x12,0x13}`, `-1` for `0x01` |
| F3-09 | Filter passes zero and positives | value 0 and 1 | returned unchanged for every id |
| F3-10 | Filtered reading → `None` in the poll, stale value kept in `sensor_data` | world returns 0x0A raw 16384 in poll 1, raw 16000 in poll 2 | poll1 `sensors[0x0A] == 0.0`; poll2 `sensors[0x0A] is None` **and** `device.sensor_data[0x0A] == 0.0`; `ok is True` (other sensors fine) |
| F3-11 | Filtered reading is not reported as fresh twice | as F3-10 | `sensors[0x0A]` is `None` in poll 2, i.e. the previous value is *not* copied into the result |
| F3-12 | Request bytes on the wire — sensor | healthy poll | for each id the written frame is exactly `bytes([seq, 0x06, sid])`, `len == 3`, `response=False` |
| F3-13 | Request bytes — buzzer query | `read_buzzer_state()` | exactly `bytes([seq, 0x50, 0x00])` |
| F3-14 | Request bytes — buzzer set | `set_buzzer_state(True)` / `(False)` | exactly `bytes([seq, 0x50, 0x01, 0x01])` / `…0x00])`, followed by a `[seq2, 0x50, 0x00]` verify frame |
| F3-15 | Buzzer query response parsing | frames `[s,0x50,0x00,0x01]` / `…0x00]` | `read_buzzer_state()` → `True` / `False`, `device.buzzer_state` matches |
| F3-16 | Buzzer set ack parsing | ack `[s,0x05,0x01]` then query says ON | `set_buzzer_state(True) is True` |
| F3-17 | Buzzer set verify mismatch | ack OK but query reports OFF | `set_buzzer_state(True) is False`, `device.buzzer_state is False` |
| F3-18 | Short buzzer query frame | `[s,0x50,0x00]` (no state byte) | future completes, `buzzer_state` unchanged from before, `read_buzzer_state()` returns the unchanged value; no exception |
| F3-19 | Unknown seq is ignored | inject `[0xEE,0x06,0x01,0x2c,0x1a]` while a request with a different seq is pending | the pending request still times out (`None`); the value is **not** applied to `sensor_data`; no other future completed |
| F3-20 | Mismatched sensor id is ignored | pending `(seq, 0x06)`, inject `[seq,0x06,0x09,…]` | the pending 0x06 request still times out; nothing else completes |
| F3-21 | Short / garbage / empty notifications never raise | inject `b""`, `b"\x01"`, `b"\x01\x02"`, `b"\xff\xff\xff\xff"`, 64 random bytes | handler returns normally, no pending future touched, no exception recorded by the loop exception handler |
| F3-22 | Late response after timeout is ignored | respond to sensor 0x01 **after** `BLE_RESPONSE_TIMEOUT` | `sensors[0x01] is None`, `sensor_data` unchanged, seq id released, no "future already done" error; later sensors unaffected |
| F3-23 | Seq-id allocation: no collision with in-flight, release, exhaustion | (a) 8 concurrent pendings → 8 distinct seq ids, none equal to an in-flight id; (b) after a response/timeout the id is released; (c) pre-fill all 256 as in-flight | (a) & (b) as stated; (c) the request path raises/returns a failure without hanging, and after teardown the in-flight set is empty |

### F4 — Coordinator, sensor and switch entities → `tests/test_coordinator_entities.py` (21 cases)

Setup: `MockConfigEntry(domain="em1003", version=1, unique_id=MAC, title=f"EM1003 ({MAC})", data={"mac_address": MAC, "device_name": "3GCG300ZY4"})` added to `hass`, healthy fake world, `async_setup` + `async_block_till_done`.

| ID | Scenario | Setup | Expected |
|----|----------|-------|----------|
| F4-01 | All 8 sensor entities exist with the right state | healthy world with known raws | 8 states, values matching the formulas, `unit_of_measurement` and `device_class` per `SENSOR_TYPES`, `state_class == "measurement"` |
| F4-02 | Sensor unique ids | — | `{f"{MAC}_{k}" for k in (temperature,humidity,noise,pm25,formaldehyde,pm10,tvoc,eco2)}` present in the entity registry |
| F4-03 | Display precision | — | temperature 2, humidity 1, formaldehyde 3, others 0 |
| F4-04 | One poll per interval, one connection per poll | advance time by 60 s twice | `establish_connection` call count grows by exactly 1 per interval; world max concurrent links 1 |
| F4-05 | Coordinator returns `result.sensors` | — | `coordinator.data` is a dict keyed by the 8 sensor ids; `coordinator.last_update_success is True` |
| F4-06 | All-`None` poll → `UpdateFailed` | world answers nothing | `coordinator.last_update_success is False`; no exception escapes; `coordinator.data` keeps the previous dict |
| F4-07 | Breaker-open poll → `UpdateFailed`, no BLE | breaker forced OPEN | `last_update_success is False`, `establish_connection` count unchanged |
| F4-08 | Entities keep the cached value < 20 min | one good poll, then a dead world; freezer +10 min, several intervals | all 8 states keep the last good values and are not `unavailable` |
| F4-09 | Entities go unavailable after 20 min | same, freezer +21 min, another interval | states become `unavailable` |
| F4-10 | Recovery | world healthy again, one interval | states return to fresh values, `available is True` |
| F4-11 | Partial poll keeps the others fresh | world drops 0x13 only | 7 sensors update; eco2 serves its cached value; `last_update_success is True` |
| F4-12 | Switch takes its state from the poll, no own connection | healthy, buzzer ON | after setup the switch state is `on`; total `establish_connection` calls == 1 (the poll only); no `0x50` frame outside the poll's session |
| F4-13 | Switch is `unknown` before the first successful poll | world dead at startup | switch state `unknown`, not `unavailable`; entity exists |
| F4-14 | `turn_on` writes the right bytes and verifies | healthy | frames `[seq,0x50,0x01,0x01]` then `[seq2,0x50,0x00]`; state becomes `on`; exactly 1 new connection |
| F4-15 | `turn_off` | healthy | frames `…0x01,0x00]` + verify; state `off` |
| F4-16 | Failed set surfaces an error and drops availability | ack missing | `hass.services.async_call("switch","turn_on", blocking=True)` raises `HomeAssistantError`; the entity becomes `unavailable`; **then** one good poll restores `available` and the polled state |
| F4-17 | Toggling during a poll is serialised | poll in flight, toggle issued | max concurrent links 1; both complete; final switch state equals the verified set value |
| F4-18 | Scan interval from options | `options={"scan_interval": 120}` | `coordinator.update_interval == timedelta(seconds=120)`; no poll at +60 s, one poll at +120 s |
| F4-19 | Default interval when options empty | `options={}` | `coordinator.update_interval == timedelta(seconds=60)` |
| F4-20 | Coordinator identity & module | — | `type(coordinator).__name__ == "EM1003DataUpdateCoordinator"`, `type(coordinator).__module__.endswith("coordinator")`, and `custom_components.em1003.sensor.EM1003DataUpdateCoordinator is coordinator.__class__` (re-export intact) |
| F4-21 | Buzzer state survives a failed buzzer query | poll 1 buzzer ON, poll 2 buzzer query unanswered | `coordinator.buzzer_state is True` after poll 2; switch state stays `on` |

### F5 — Setup, unload, services, device name → `tests/test_init_setup.py` (22 cases)

| ID | Scenario | Setup | Expected |
|----|----------|-------|----------|
| F5-01 | Setup succeeds when the device is unreachable | `async_ble_device_from_address` → `None` | `entry.state is ConfigEntryState.LOADED`; 9 entities exist; sensors `unavailable`, switch `unknown`; no exception |
| F5-02 | Setup succeeds when connect raises | `establish_connection` raises `BleakError` | LOADED; `coordinator.last_update_success is False` |
| F5-03 | Coordinator is in `hass.data` before platforms | patch `async_forward_entry_setups` to record | at call time `hass.data["em1003"][entry_id]["coordinator"]` already exists and is the coordinator instance |
| F5-04 | `hass.data` payload keys | healthy | exactly `{"mac_address", "name", "device_name", "device", "coordinator"}`; `["mac_address"] == entry.data["mac_address"]`; `["name"] == entry.title` |
| F5-05 | No `asyncio.sleep(2)` at setup | patch `asyncio.sleep` recorder | no sleep ≥ 1 s during setup |
| F5-06 | Cached device name → zero name-read connections | `data={"mac_address":…, "device_name":"CACHED"}` | total `establish_connection` calls == 1 (the first poll); device registry name == `"CACHED"`; `read_gatt_char(DEVICE_NAME_UUID)` never called |
| F5-07 | Uncached name → exactly one attempt, cached additively | `data={"mac_address": MAC}`, name read succeeds | exactly 2 connections total; `entry.data == {"mac_address": MAC, "device_name": "3GCG300ZY4"}`; the name read used `max_attempts=1`; `entry.title` unchanged |
| F5-08 | Uncached name, read fails → fallback, nothing written | name read fails | device registry name == `entry.title`; `"device_name" not in entry.data`; setup still LOADED; exactly 1 name attempt (no retry loop) |
| F5-09 | `entry.title` / `unique_id` / `version` never change | both F5-07 and F5-08 paths | all three equal their pre-setup values |
| F5-10 | Name read goes through `device.py` | — | the name read is visible to the `device.establish_connection` patch (i.e. counted by the fake); `__init__.py` has no `establish_connection` import |
| F5-11 | Unload disconnects an open link | force a session to still hold a link (hang at disconnect cleared), then unload | `async_unload` returns True; world reports 0 open links; `entry.state is NOT_LOADED` |
| F5-12 | Unload shuts the coordinator down and pops `hass.data` | healthy | `coordinator.update_interval` scheduling stopped (no further polls after +5 intervals); `entry.entry_id not in hass.data.get("em1003", {})` |
| F5-13 | Services registered once for two entries | two entries, different MACs | after both setups each of the 6 services exists exactly once; `hass.services.async_services()["em1003"]` has 6 keys |
| F5-14 | Services survive every unload (amended in the review round; previously "removed only on the last unload") | two entries | after unloading the first **and** after unloading the second: all 6 still registered; `"em1003" not in hass.data` |
| F5-15 | Reload works | `hass.config_entries.async_reload(entry_id)` | ends LOADED; entity ids and unique ids unchanged; services still 6; no duplicate registry entries |
| F5-16 | Every debug service runs against the fake without raising | call all 6 with valid data | each returns; world shows 0 open links afterwards; `scan_device` and `read_device_name` included |
| F5-17 | Debug services serialise with a concurrent poll on the same MAC | poll in flight, call `list_services` for the same MAC | world max concurrent links 1; both complete; the service used the configured entry's device (same `EM1003Device` instance / same lock) |
| F5-18 | Debug service for an unconfigured MAC | call `list_services` with a different MAC | works against a transient device; does not disturb the configured device's lock or breaker |
| F5-19 | `write_characteristic` data parsing | `"0102ff"`, `"01 02 ff"`, `"hello"`, `[1,2,255]` | bytes written are `b"\x01\x02\xff"`, `b"\x01\x02\xff"`, `b"hello"`, `b"\x01\x02\xff"`; an invalid value logs and does not raise |
| F5-20 | `scan_device` does not touch `BleakScanner` | `BleakScanner.discover` patched to raise | the service completes; `BleakScanner` is not importable from `custom_components.em1003.__init__` (`assert not hasattr(mod, "BleakScanner")`) |
| F5-21 | `scan_device` never reads `BLEDevice.rssi` (D7) | fake `BLEDevice` raises `AttributeError` on `rssi` | the service completes; `bluetooth.async_last_service_info` was called |
| F5-22 | A failing debug service still disconnects | `read_characteristic` on a uuid that raises | service returns; world shows 0 open links; the device lock is free |

### F6 — Config flow, options flow, manifest → `tests/test_config_flow.py` (19 cases)

| ID | Scenario | Setup | Expected |
|----|----------|-------|----------|
| F6-01 | User step with discovered devices → dropdown | `async_discovered_service_info` returns 2 | form `user`; the schema for `mac_address` is a `vol.In` containing both addresses; `description_placeholders["discovered"] == "2"` |
| F6-02 | User step without discovered devices → text input | returns `[]` | form `user`; the `mac_address` field accepts a free string; `"discovered" == "0"` |
| F6-03 | MAC normalisation | input `"aa-bb-cc-dd-ee-ff"` | entry created with `data == {"mac_address": "AA:BB:CC:DD:EE:FF"}`, `unique_id == "AA:BB:CC:DD:EE:FF"` |
| F6-04 | Surrounding whitespace | `"  aa:bb:cc:dd:ee:ff  "` | same as F6-03 |
| F6-05 | Duplicate MAC aborts | an entry with that unique_id exists | `type == "abort"`, `reason == "already_configured"` |
| F6-06 | Duplicate against a **legacy lowercase** entry | existing entry `unique_id="aa:bb:cc:dd:ee:ff"`, `data={"mac_address":"aa:bb:cc:dd:ee:ff"}`; new input uppercase | abort `already_configured`; the existing entry's `data`/`unique_id`/`title` are untouched |
| F6-07 | Title on a successful name read | name read → `"3GCG300ZY4"` | title == `"3GCG300ZY4 (DD:EE:FF)"` (`mac[-8:]`) |
| F6-08 | Title on a failed name read | name read → `None` | title == `"EM1003 (AA:BB:CC:DD:EE:FF)"` |
| F6-09 | Name read during config flow goes through `device.py` | — | the fake's `establish_connection` counter increments by exactly 1 |
| F6-10 | Bluetooth discovery step | `async_step_bluetooth` with a `BluetoothServiceInfoBleak` | shows `bluetooth_confirm`; `_set_confirm_only` applied |
| F6-11 | Bluetooth confirm creates the entry | confirm with `{}` | `data == {"mac_address": <address>}`, title `"EM1003"` (`DEFAULT_NAME`, unchanged behaviour) |
| F6-12 | Bluetooth discovery of a configured device aborts | entry exists | abort `already_configured` |
| F6-13 | Options flow opens on current HA (D6) | `hass.config_entries.options.async_init(entry.entry_id)` | `type == "form"`, `step_id == "init"`, **no `AttributeError`**; default equals the current interval (or 60) |
| F6-14 | Options range validation | submit 9 and 601 | both raise `vol.Invalid` / return the form with an error; the stored options are unchanged |
| F6-15 | Options bounds accepted | submit 10 and 600 | both create the entry; `entry.options["scan_interval"]` equals the value |
| F6-16 | Options save + live reload changes the interval (D6) | entry loaded with default 60, submit 120, `async_block_till_done()` | `entry.options == {"scan_interval": 120}`; the entry was reloaded; the new coordinator's `update_interval == timedelta(seconds=120)`; `entry.data` and `entry.title` unchanged |
| F6-17 | Reload from the options listener does not duplicate entities | as F6-16 | entity registry count identical before/after; the same entity ids |
| F6-18 | Manifest sanity | read `manifest.json` | `domain == "em1003"`, `config_flow is True`, `dependencies == ["bluetooth_adapters"]`, `version` is a valid `AwesomeVersion`/semver string equal to `const.VERSION`, `bluetooth == [{"local_name":"EM1003*"},{"local_name":"720*"}]`, `iot_class == "local_polling"`, `integration_type == "device"` |
| F6-19 | Translation keys match the flows | load `strings.json`, `translations/en.json`, `translations/zh-Hans.json` | every file has `config.step.user`, `config.step.bluetooth_confirm`, `config.abort.already_configured`, `options.step.init.data.scan_interval`; the three files have identical key sets |

### F7 — Backward compatibility with already-registered devices → `tests/test_backward_compat.py` (19 cases)

Shared legacy fixture (`legacy_install`): `MockConfigEntry(domain="em1003", version=1, unique_id=MAC, title=f"EM1003 ({MAC})", data={"mac_address": MAC}, options=<param>)`, plus an entity registry pre-populated with the 9 legacy unique ids

```
sensor/em1003:  f"{MAC}_temperature" … f"{MAC}_eco2"      (8, in SENSOR_TYPES order)
switch/em1003:  f"{MAC}_buzzer"
```

where at least one entity has a customised `entity_id` (`sensor.living_room_air_pm25`), one a
`name` override, one `disabled_by=RegistryEntryDisabler.USER`; plus a device registry entry with
`identifiers={("em1003", MAC)}`, `connections={("mac", MAC)}`, `name_by_user="客厅空气"`.
Parametrised over `options={}` and `options={"scan_interval": 120}`.

| ID | Scenario | Expected |
|----|----------|----------|
| F7-01 | No new or duplicate entities | after setup, entity registry count is exactly the pre-seeded 9; every legacy unique id is still present and still bound to this config entry |
| F7-02 | Entity ids preserved | the customised `sensor.living_room_air_pm25` keeps that entity id; no `_2` suffix anywhere |
| F7-03 | User customisations preserved | the `name` override, the `disabled_by=USER` flag and any `area_id`/`icon` are unchanged after setup |
| F7-04 | Device registry untouched | count unchanged (1); `identifiers == {("em1003", MAC)}`; `connections == {("mac", MAC)}`; `name_by_user == "客厅空气"` preserved; `config_entries` still contains this entry |
| F7-05 | `entry.data["mac_address"]` unchanged | identical string object value, same case, same separators |
| F7-06 | Only additive data keys | `set(entry.data) - {"mac_address"} ⊆ {"device_name"}`; with a failing name read the set is exactly `{"mac_address"}` |
| F7-07 | Options untouched | `entry.options` equals the fixture value exactly (setup never writes options) |
| F7-08 | Title / version / minor_version / unique_id unchanged | all four compared before and after setup |
| F7-09 | Disabled entity stays disabled and is not polled into existence | the disabled entity has no state; the others do |
| F7-10 | The 8 sensors + 1 switch get states from the first poll | states match the fake's values; the switch reflects the polled buzzer state |
| F7-11 | Unload/reload cycle preserves everything | after `async_reload`, F7-01..F7-08 all still hold |
| F7-12 | No migration path exists | `not hasattr(custom_components.em1003, "async_migrate_entry")`; `EM1003ConfigFlow.VERSION == 1`; `entry.version == 1` after setup |
| F7-13 | Entity attribute contract | for each of the 8: `has_entity_name is True`, `state_class == "measurement"`, `suggested_display_precision` per BC-12, `unit_of_measurement` and `device_class` per `SENSOR_TYPES`, `icon` per `SENSOR_TYPES` |
| F7-14 | `EM1003Device` legacy surface | `EM1003Device(hass, MAC, "name")` constructs positionally; `read_all_sensors`, `read_buzzer_state`, `set_buzzer_state`, `disconnect` are coroutine functions; `sensor_data`, `buzzer_state`, `mac_address`, `device_name` are attributes; `not hasattr(dev, "read_sensor")` |
| F7-15 | Legacy lowercase MAC entry | fixture with `MAC.lower()` everywhere: setup LOADED, the 9 legacy unique ids (lowercase) reused, entity count unchanged, device identifiers untouched |
| F7-16 | Constant contract — identity | `DOMAIN == "em1003"`, `CONF_MAC_ADDRESS == "mac_address"`, `CONF_SCAN_INTERVAL == "scan_interval"`, `DEFAULT_SCAN_INTERVAL == 60`, `DEFAULT_NAME == "EM1003"`, `CONF_DEVICE_NAME == "device_name"` |
| F7-17 | Constant contract — sensors | exact table assertion over `SENSOR_TYPES`: `{0x01:(temperature, Temperature, °C, temperature), 0x06:(humidity, Humidity, %, humidity), 0x08:(noise, Noise Level, dB, None), 0x09:(pm25, PM2.5, µg/m³, pm25), 0x0A:(formaldehyde, Formaldehyde, mg/m³, None), 0x11:(pm10, PM10, µg/m³, pm10), 0x12:(tvoc, TVOC, µg/m³, volatile_organic_compounds), 0x13:(eco2, eCO2, ppm, carbon_dioxide)}` plus key order |
| F7-18 | Constant contract — services | the 6 names equal `scan_device, read_characteristic, write_characteristic, list_services, discover_all, read_device_name`; `services.yaml` declares exactly those 6 with the required fields (`mac_address`; `+characteristic_uuid`; `+data`) |
| F7-19 | Protocol constant contract | `CMD_READ_SENSOR == 0x06`, `CMD_BUZZER == 0x50`, `CMD_BUZZER_SET_RESPONSE == 0x05`, `BUZZER_ON == 0x01`, `BUZZER_OFF == 0x00`, the three UUID strings unchanged |

**Total: 146 cases** (F1 22, F2 20, F3 23, F4 21, F5 22, F6 19, F7 19).

---

## 11. File ownership

| Owner | Files | Must not touch |
|-------|-------|----------------|
| **Device implementer** | `custom_components/em1003/device.py`, `custom_components/em1003/const.py` | anything else |
| **HA-side implementer** | `custom_components/em1003/__init__.py`, `coordinator.py` (new), `sensor.py`, `switch.py`, `config_flow.py`, `manifest.json`, `services.yaml`, `strings.json`, `translations/en.json`, `translations/zh-Hans.json` | `device.py`, `const.py`, `tests/*` |
| **Test authors** | `tests/test_device_session.py` (F1), `tests/test_circuit_breaker.py` (F2), `tests/test_protocol.py` (F3), `tests/test_coordinator_entities.py` (F4), `tests/test_init_setup.py` (F5), `tests/test_config_flow.py` (F6), `tests/test_backward_compat.py` (F7) | any `custom_components/**` file |
| **Test scaffold author** (already in flight) | `tests/conftest.py`, `tests/fakes.py`, `tests/__init__.py`, and the pytest config (`pytest.ini` or `pyproject.toml` `[tool.pytest.ini_options]`) | everything else |
| **Architect** | `docs/ble_session_refactor.md` (this file) | all code |

If a needed constant is missing, the test author asserts against the name defined in §3 and files it
with the device implementer — nobody edits another owner's file.

Run: `cd /home/fooling/repo/em1003 && .venv/bin/python -m pytest tests -q`.

---

## 12. Review round (post-refactor)

Findings from an independent review of the refactored working tree, each verified by a second
agent before being applied. Every change is minimal and local; nothing in section 1.3's
backward-compatibility list (unique ids, device identifiers/connections, `entry.data`/`options`
keys, `entry.title`, service names/schemas, `ConfigFlow.VERSION`, `SENSOR_TYPES`) was touched.

| # | Change | Where | Guard |
|---|--------|-------|-------|
| 1 | `discover_all` / `list_services` materialise `list(client.services)` before counting — real bleak's `BleakGATTServiceCollection` has no `__len__`, so every real invocation used to die of `TypeError` inside the swallowed `except Exception`. `tests/fakes.py` now mimics that shape (`FakeServiceCollection`: iterable, no `__len__`). | `__init__.py`, `tests/fakes.py` | `test_r_discover_all_service_collection_has_no_len` |
| 2 | `hacs.json` `homeassistant` floor raised `2023.1.0` → `2024.12.0` (see §1.4). | `hacs.json`, `README.md` | `test_r_hacs_declares_the_real_minimum_ha_version` |
| 3 | The BC-22 case-insensitive duplicate scan is factored into `EM1003ConfigFlow._mac_already_configured()` and now also runs in `async_step_bluetooth`, so discovery no longer offers (and duplicates) a legacy lowercase-MAC entry. The legacy entry is never rewritten. | `config_flow.py` | `test_r_bluetooth_discovery_aborts_for_legacy_lowercase_entry` |
| 4 | The 6 debug services are no longer unregistered on the last unload (`_async_unregister_services` and `_ALL_SERVICES` deleted) — back to v1.0.3's "register once, keep for the life of HA". A single-entry install is fully unloaded during every options-triggered reload, and calls made in that window used to raise `ServiceNotFound`. **Deliberate contract change**: F5-14 and D8 amended above. | `__init__.py` | `test_r_services_survive_the_reload_window`, amended `test_f5_14_services_survive_every_unload` |
| 5 | `async_unload_entry`'s `device.disconnect()` is bounded by the new `BLE_UNLOAD_DISCONNECT_TIMEOUT` (10s, `TimeoutError` suppressed): it waits on the lock an in-flight poll holds, so saving options while the device is out of range used to stall the reload for minutes with all 9 entities already removed. `BLE_SESSION_TIMEOUT` lowered 120 → 60s, with the arithmetic documented in `const.py`. | `__init__.py`, `const.py` | `test_r_unload_is_not_blocked_by_an_in_flight_poll` |
| 6 | `write_characteristic`'s GATT write is wrapped in `asyncio.timeout(const.BLE_WRITE_TIMEOUT)` like every sibling debug service, so an unacknowledged write cannot pin the coordinator's device lock for a whole session budget. | `__init__.py` | `test_r_write_characteristic_is_bounded_by_write_timeout` |
| 7 | `establish_connection` is handed `BleakClientWithServiceCache` instead of the plain `BleakClient`, so bleak-retry-connector can `clear_cache()` on a stale-GATT-cache `KeyError` instead of failing every poll until HA restarts. The `establish_connection` import name (BC-21) and its patch point are unchanged. | `device.py` | `test_r_connect_uses_service_cache_capable_client` |
| 8 | Test gaps around the core deadlock fix: F1-17 now makes the drop-triggering sensor silent, so a request really is pending when the link drops (the old version let the fake answer first, and survived deleting `_fail_pending` from `_handle_disconnect`); `set_buzzer_state`'s two untested error branches (SET never acked, verify query never answered) are covered. | `tests/test_device_session.py`, `tests/test_protocol.py` | amended `test_f1_17_...`, `test_r_buzzer_set_never_acknowledged`, `test_r_buzzer_set_verify_query_never_answered` |

Suite after the review round: **212 passed** (203 + 9), no skips, no xfails.
