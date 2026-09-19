"""Shared pytest fixtures for the EM1003 integration test suite.

This module is owned by the test-scaffold author (see
``docs/ble_session_refactor.md`` section 11). Every other test file in this
package builds on top of the fixtures defined here rather than talking to
``tests/fakes.py`` or ``pytest_homeassistant_custom_component`` directly.

--------------------------------------------------------------------------
What's wired up automatically (no fixture request needed)
--------------------------------------------------------------------------
* Custom integrations are enabled for every test in this package (a plain
  ``import custom_components.em1003`` — or ``hass.config_entries.async_setup``
  finding it via HA's loader — works without each test requesting
  ``enable_custom_integrations`` itself). This only works because
  ``tests/__init__.py`` exists, making ``tests`` a real package: pytest then
  inserts the repository root (the parent of ``tests/``, which is also the
  parent of ``custom_components/``) onto ``sys.path``, so
  ``import custom_components.em1003`` resolves to *this* repo's code.
* ``pytest.ini`` sets ``asyncio_mode = auto``, so async test functions and
  async fixtures need no ``@pytest.mark.asyncio`` decorator.

--------------------------------------------------------------------------
Fixtures you will actually use
--------------------------------------------------------------------------
``ble_world`` — a `FakeBleWorld` (see ``tests/fakes.py``) already patched
into every place the integration reaches real Bluetooth: both
``custom_components.em1003.device.establish_connection`` and, guarded so it
keeps working after the refactor removes it, the pre-refactor
``custom_components.em1003.establish_connection``; likewise
``bluetooth.async_ble_device_from_address`` / ``async_last_service_info`` /
``async_discovered_service_info`` wherever the integration imports the
``bluetooth`` module (``device.py``, ``__init__.py``, and, if present,
``config_flow.py``), plus config_flow's direct
``from homeassistant.components.bluetooth import async_discovered_service_info``
binding. It also pulls in ``pytest_homeassistant_custom_component``'s
``enable_bluetooth`` fixture, so the real ``bluetooth`` integration and
``bluetooth_adapters`` are properly (and cheaply — everything OS-level is
mocked by ``mock_bluetooth_adapters``) initialized in ``hass``; adding
``dependencies: ["bluetooth_adapters"]`` to ``manifest.json`` will not break
setup in tests that use this fixture. Configure the world *before* the
integration talks to it, e.g.::

    async def test_temperature(hass, ble_world, mock_entry, setup_integration):
        ble_world.set_sensor_value(0x01, 21.5)
        entry = mock_entry()
        assert await setup_integration(hass, entry)
        state = hass.states.get("sensor.em1003_temperature")
        assert float(state.state) == 21.5
        assert ble_world.links_are_clean

``fast_timeouts`` — shrinks every ``BLE_*TIMEOUT*`` / ``BLE_*DELAY*`` /
``BLE_*GUARD*`` constant in ``custom_components.em1003.const`` to a small
value (0.05s; 0.2s for ``BLE_SESSION_TIMEOUT``) via ``monkeypatch``, so
hang/slow-fake tests finish in about a second instead of minutes. It
discovers the constants *by name* at fixture-call time, so it is a safe
no-op today (none of them exist yet, pre-refactor) and starts working the
moment the device implementer adds them, with no changes needed here. Use
it together with ``ble_world.hang_at`` / ``ble_world.raise_at``::

    async def test_hang_at_connect(hass, ble_world, fast_timeouts, ...):
        ble_world.hang_at.add("connect")
        ...  # the operation under test must now return in ~1s, not hang

``mock_entry`` — a factory fixture: ``mock_entry(mac=..., data=..., options=...,
title=..., unique_id=..., entry_id=..., version=1) -> MockConfigEntry``. Not
yet added to ``hass`` — call ``entry.add_to_hass(hass)`` yourself, or use
``setup_integration`` which does it for you. Defaults to
``MAC = "AA:BB:CC:DD:EE:FF"`` (matching ``docs/ble_session_refactor.md``'s
test matrix) with ``data={"mac_address": MAC}`` and empty options, mirroring
a real config entry created by the config flow.

``setup_integration`` — an async helper: ``await setup_integration(hass, entry)``
does ``entry.add_to_hass(hass)``, ``await hass.config_entries.async_setup(entry.entry_id)``,
``await hass.async_block_till_done()``, and returns the ``async_setup``
result (``True``/``False``).

--------------------------------------------------------------------------
Full example combining everything
--------------------------------------------------------------------------
    async def test_scan_interval_from_options(hass, ble_world, mock_entry, setup_integration):
        entry = mock_entry(options={"scan_interval": 120})
        assert await setup_integration(hass, entry)
        coordinator = hass.data["em1003"][entry.entry_id]["coordinator"]
        assert coordinator.update_interval == timedelta(seconds=120)
"""
from __future__ import annotations

from typing import Any, Callable

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

# `const.py` is the one module every fixture here — and tests/fakes.py
# itself — fundamentally cannot do without (it supplies the protocol byte
# values the fake world encodes/decodes), so it is imported eagerly like
# any other real dependency.
from custom_components.em1003 import const as em1003_const

from .fakes import FakeBleWorld

#: The MAC address used by every test fixture default and by the test
#: matrix in docs/ble_session_refactor.md. Not normalised — pass a
#: differently-cased/separated value to `mock_entry` to exercise
#: normalisation or legacy-lowercase-entry behaviour.
MAC = "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# Custom integration loading — always on for this test package
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make every test in this package able to load custom_components/em1003.

    Wraps pytest-homeassistant-custom-component's own `enable_custom_integrations`
    fixture so individual test files don't need to request it by name.
    """


# ---------------------------------------------------------------------------
# ble_world: the fake BLE stack, wired into every known patch point
# ---------------------------------------------------------------------------


def _import_optional(module_path: str) -> Any | None:
    """Import `module_path`, returning None instead of raising.

    `device.py`, `__init__.py` and `config_flow.py` are all owned by other
    agents in this workflow and may be mid-edit/transiently broken at any
    given moment. `ble_world` is used by nearly every test in the suite, so
    it must degrade (skip the patches that module would have needed) rather
    than take down test *collection* for the whole session over a
    momentarily-broken import elsewhere. A test that actually exercises a
    broken module will still fail, clearly, on its own merits.
    """
    import importlib

    try:
        return importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 - pragma: no cover - defensive
        return None


def _bluetooth_modules_to_patch() -> list[Any]:
    """Every module in the integration that does `from homeassistant.components
    import bluetooth` (binding the whole module, not individual functions).
    Patching `<module>.bluetooth.<fn>` on each covers `device.py`, the
    pre-and-post-refactor `__init__.py`, and `config_flow.py` if/when it
    also imports `bluetooth` directly (it does not today, but this stays
    correct either way since we check `hasattr` before patching)."""
    modules = [
        _import_optional("custom_components.em1003.device"),
        _import_optional("custom_components.em1003"),
        _import_optional("custom_components.em1003.config_flow"),
    ]
    return [m for m in modules if m is not None]


@pytest.fixture
def ble_world(monkeypatch: pytest.MonkeyPatch, enable_bluetooth: None) -> FakeBleWorld:
    """A `FakeBleWorld` (see tests/fakes.py) patched into every BLE entry
    point the integration currently has, and the ones the refactor adds.

    Depends on pytest-homeassistant-custom-component's `enable_bluetooth`
    fixture so the real `bluetooth` integration (and, transitively,
    `bluetooth_adapters`) is set up in `hass` with everything OS-level
    mocked — this is what keeps `manifest.json`'s future
    `dependencies: ["bluetooth_adapters"]` from breaking test setup, and
    gives any bluetooth call this fixture doesn't know to stub a sane,
    already-initialized (empty) manager to talk to instead of a KeyError.
    """
    world = FakeBleWorld()
    em1003_device_module = _import_optional("custom_components.em1003.device")
    em1003_pkg = _import_optional("custom_components.em1003")
    em1003_config_flow_module = _import_optional("custom_components.em1003.config_flow")

    for module in _bluetooth_modules_to_patch():
        bluetooth_module = getattr(module, "bluetooth", None)
        if bluetooth_module is None:
            continue
        monkeypatch.setattr(
            bluetooth_module, "async_ble_device_from_address", world.async_ble_device_from_address, raising=False
        )
        monkeypatch.setattr(
            bluetooth_module, "async_last_service_info", world.async_last_service_info, raising=False
        )
        monkeypatch.setattr(
            bluetooth_module, "async_discovered_service_info", world.async_discovered_service_info, raising=False
        )

    # device.py always imports establish_connection by name (BC-21).
    if em1003_device_module is not None:
        monkeypatch.setattr(em1003_device_module, "establish_connection", world.establish_connection)

    # __init__.py imports establish_connection directly too, but only in the
    # current pre-refactor code — BC-21 has the refactor remove it so that
    # device.py is the single BLE choke point. The hasattr guard (plus
    # raising=False) means: patch it while it's there, and quietly do
    # nothing once it's gone.
    if em1003_pkg is not None and hasattr(em1003_pkg, "establish_connection"):
        monkeypatch.setattr(em1003_pkg, "establish_connection", world.establish_connection, raising=False)

    # config_flow does `from homeassistant.components.bluetooth import
    # async_discovered_service_info` — a direct name binding, not reached by
    # the `<module>.bluetooth.*` patching above.
    if em1003_config_flow_module is not None and hasattr(em1003_config_flow_module, "async_discovered_service_info"):
        monkeypatch.setattr(
            em1003_config_flow_module,
            "async_discovered_service_info",
            world.async_discovered_service_info,
            raising=False,
        )

    return world


# ---------------------------------------------------------------------------
# fast_timeouts: shrink every BLE_* timing constant that exists
# ---------------------------------------------------------------------------


#: Pacing values (not failure budgets): they exist to be kind to the real
#: peripheral between consecutive requests / reconnects, and nothing about
#: the integration's behaviour depends on their production magnitude. Left
#: at their production values every test that polls would burn
#: ``7 * 0.3s`` per sweep plus a ``2.0s`` reconnect guard of pure wall-clock
#: sleep, which is what pushed the suite from seconds into tens of minutes.
#: They are shrunk for the whole suite by the autouse fixture below; a test
#: that wants different values simply monkeypatches them itself afterwards
#: (test-local monkeypatching always wins over an autouse fixture).
#:
#: ``BLE_RECONNECT_GUARD`` deliberately stays at 0.05 rather than going to
#: ~0: F1-18 asserts on the *value* of the guard sleep and needs it to stay
#: clearly distinguishable from the inter-request delay.
_PACING_OVERRIDES: dict[str, float] = {
    "BLE_INTER_REQUEST_DELAY": 0.005,
    "BLE_BUZZER_SETTLE_DELAY": 0.005,
    "BLE_RECONNECT_GUARD": 0.05,
}


def _shrink_pacing(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Apply `_PACING_OVERRIDES` to whichever constants actually exist."""
    patched: dict[str, float] = {}
    for name, value in _PACING_OVERRIDES.items():
        if isinstance(getattr(em1003_const, name, None), (int, float)):
            monkeypatch.setattr(em1003_const, name, value)
            patched[name] = value
    return patched


@pytest.fixture(autouse=True)
def fast_ble_pacing(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Shrink the BLE *pacing* delays (not the timeouts) for every test.

    Keeps the whole suite in the seconds range without changing any
    observable behaviour: these constants only space requests out on the
    wire. Failure budgets (`BLE_*TIMEOUT*`) are untouched here — a test
    that wants those shrunk asks for `fast_timeouts`.
    """
    return _shrink_pacing(monkeypatch)


@pytest.fixture
def fast_timeouts(monkeypatch: pytest.MonkeyPatch, fast_ble_pacing: dict[str, float]) -> dict[str, float]:
    """Shrink every `BLE_*TIMEOUT*` constant in
    `custom_components.em1003.const` to a small value (0.05s; 0.2s for the
    overall `BLE_SESSION_TIMEOUT`), discovered by name so this keeps working
    as section 3 of docs/ble_session_refactor.md evolves.

    The `BLE_*DELAY*` / `BLE_*GUARD*` pacing constants keep the (already
    small) values `fast_ble_pacing` gave them: shrinking them to 0.05 as
    well used to make a *healthy* 8-sensor sweep cost `7 * 0.05 = 0.35s` of
    pure pacing and therefore always blow the 0.2s session deadline, which
    is a scaffold artefact rather than anything the plan asks for.

    Returns the dict of {constant_name: new_value} actually patched, in
    case a test wants to assert something patched or read the value back.
    """
    patched: dict[str, float] = dict(fast_ble_pacing)
    for name in dir(em1003_const):
        if not name.startswith("BLE_") or "TIMEOUT" not in name:
            continue
        current = getattr(em1003_const, name)
        if not isinstance(current, (int, float)):
            continue
        new_value = 0.2 if name == "BLE_SESSION_TIMEOUT" else 0.05
        monkeypatch.setattr(em1003_const, name, new_value)
        patched[name] = new_value
    return patched


# ---------------------------------------------------------------------------
# mock_entry: a MockConfigEntry factory with EM1003-shaped defaults
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_entry() -> Callable[..., MockConfigEntry]:
    """Factory fixture: `mock_entry(mac=MAC, data=None, options=None,
    title=None, unique_id=None, entry_id=None, version=1) -> MockConfigEntry`.

    Not added to `hass` yet — call `entry.add_to_hass(hass)` yourself, or
    use the `setup_integration` fixture which does both steps.
    """

    def _make(
        mac: str = MAC,
        *,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        title: str | None = None,
        unique_id: str | None = None,
        entry_id: str | None = None,
        version: int = 1,
    ) -> MockConfigEntry:
        entry_data: dict[str, Any] = {em1003_const.CONF_MAC_ADDRESS: mac}
        if data:
            entry_data.update(data)
        kwargs: dict[str, Any] = {
            "domain": em1003_const.DOMAIN,
            "data": entry_data,
            "options": options or {},
            "unique_id": unique_id if unique_id is not None else mac,
            "title": title if title is not None else f"{em1003_const.DEFAULT_NAME} ({mac})",
            "version": version,
        }
        if entry_id is not None:
            kwargs["entry_id"] = entry_id
        return MockConfigEntry(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# setup_integration: add-to-hass + async_setup + block_till_done, in one call
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_integration() -> Callable[..., Any]:
    """Async helper: `await setup_integration(hass, entry) -> bool`.

    Adds `entry` to `hass`, runs `hass.config_entries.async_setup`, waits
    for `hass.async_block_till_done()`, and returns the setup result.
    """

    async def _setup(hass: Any, entry: MockConfigEntry) -> bool:
        entry.add_to_hass(hass)
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return result

    return _setup
