"""F7 — Backward compatibility with already-registered devices (docs/
ble_session_refactor.md section 10, table "F7").

Owned by the F7 test author (see section 11, "File ownership"). Implements
every case in the F7 matrix (19 cases, F7-01..F7-19) plus three additional
regression guards marked ``F7-Xn`` for gaps the matrix does not spell out
explicitly but that the plan's backward-compatibility goal (G7) implies.

Scope, per the task brief and section 10's shared ``legacy_install`` fixture
description: simulate an install created by v1.0.3 —

* a ``MockConfigEntry`` with ``data == {"mac_address": MAC}`` ONLY (no
  ``device_name`` yet), ``version=1``, a v1.0.3-shaped title, and
  parametrised ``options`` (``{}`` and ``{"scan_interval": 120}``);
* a pre-populated entity registry holding the 9 legacy unique ids (8
  sensors + the buzzer switch), with a customised ``entity_id`` on one, a
  ``name`` override on another, a ``disabled_by=RegistryEntryDisabler.USER``
  on a third, and a custom ``icon``/``area_id`` on a fourth;
* a pre-populated device registry entry with the legacy ``identifiers`` /
  ``connections`` and a ``name_by_user``.

then runs the *new* (post-refactor) ``async_setup_entry`` against that
install and asserts zero entity/device churn, all customisations intact,
and the additive-only contract on ``entry.data``.

This file only builds what it needs locally (a ``make_legacy_install``
fixture + a couple of small helpers) rather than editing ``tests/fakes.py``
or ``tests/conftest.py``, per the task brief and section 11's file
ownership table — this file's owner may only touch
``tests/test_backward_compat.py``.

The source under test (``__init__.py``, ``coordinator.py``, ``sensor.py``,
``switch.py``, ``config_flow.py``, ``device.py``, ``const.py``) is being
written concurrently by other agents to match the plan's contract. These
tests are written against *that* contract (sections 3, 6, 7 and the
BC-01..BC-22 invariant table in section 1.3), not against whatever
currently happens to be on disk.

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_backward_compat.py -q
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from homeassistant.components.sensor.const import AMBIGUOUS_UNITS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.em1003 as em1003_package
from custom_components.em1003 import const as em1003_const
from custom_components.em1003.config_flow import EM1003ConfigFlow
from custom_components.em1003.device import EM1003Device

from .conftest import MAC

DOMAIN = em1003_const.DOMAIN

SERVICES_YAML_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "em1003" / "services.yaml"
)

#: Precision-per-key contract (BC-12): temperature 2, humidity 1,
#: formaldehyde 3, everything else 0.
_PRECISION_BY_KEY: dict[str, int] = {"temperature": 2, "humidity": 1, "formaldehyde": 3}

#: The exact sensor table pinned by F7-17 / BC-11.
_EXPECTED_SENSOR_TABLE: dict[int, tuple[str, str, str, str | None]] = {
    0x01: ("temperature", "Temperature", "°C", "temperature"),
    0x06: ("humidity", "Humidity", "%", "humidity"),
    0x08: ("noise", "Noise Level", "dB", None),
    0x09: ("pm25", "PM2.5", "µg/m³", "pm25"),
    0x0A: ("formaldehyde", "Formaldehyde", "mg/m³", None),
    0x11: ("pm10", "PM10", "µg/m³", "pm10"),
    0x12: ("tvoc", "TVOC", "µg/m³", "volatile_organic_compounds"),
    0x13: ("eco2", "eCO2", "ppm", "carbon_dioxide"),
}


# ---------------------------------------------------------------------------
# Local fixture: a v1.0.3-shaped install, pre-existing in the registries
# ---------------------------------------------------------------------------


@dataclass
class LegacyInstall:
    """A simulated v1.0.3 install: the config entry plus everything a real
    user's `.storage` would already contain for it, *before* the refactored
    integration ever runs `async_setup_entry` against it."""

    entry: MockConfigEntry
    mac: str
    device_id: str
    entity_ids: dict[str, str]  # legacy unique_id -> entity_id
    customised_entity_unique_id: str  # pm25: customised entity_id
    customised_name_unique_id: str  # temperature: customised name
    disabled_unique_id: str  # eco2: disabled_by=USER
    customised_area_icon_unique_id: str  # humidity: customised icon + area
    area_id: str
    name_by_user: str = "客厅空气"


@pytest.fixture
def make_legacy_install(hass, mock_entry) -> Callable[..., LegacyInstall]:
    """Factory fixture: ``make_legacy_install(mac=MAC, options=None) ->
    LegacyInstall``.

    Builds the config entry, adds it to ``hass`` (needed so the device/entity
    registry rows below can link to it by ``entry_id``, exactly as a real
    ``.storage`` restore would already have it), and pre-populates the
    device/entity registries exactly as a real v1.0.3 install would already
    have them. Does **not** run setup — that happens in ``_setup_legacy``
    below, so a test can still configure ``ble_world`` (e.g.
    ``device_present = False``) before the new code ever touches it.
    """

    def _make(mac: str = MAC, options: dict[str, Any] | None = None) -> LegacyInstall:
        # `mock_entry` defaults `data` to exactly {"mac_address": mac} when
        # no `data=` override is given — matching a v1.0.3 entry, which
        # predates CONF_DEVICE_NAME entirely.
        entry = mock_entry(mac, options=options)
        # Registered in hass now (not yet set up) so the device/entity
        # registries below can link to it by entry_id, exactly as a real
        # `.storage` restore would already have it linked before the
        # integration's `async_setup_entry` ever runs.
        entry.add_to_hass(hass)

        device_registry = dr.async_get(hass)
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, mac)},
            connections={("mac", mac)},
            name=entry.title,
            manufacturer="EM1003",
            model="BLE Air Quality Sensor",
        )
        name_by_user = "客厅空气"
        device_registry.async_update_device(device.id, name_by_user=name_by_user)

        area_registry = ar.async_get(hass)
        area = area_registry.async_get_or_create("Living Room")

        entity_registry = er.async_get(hass)
        entity_ids: dict[str, str] = {}

        customised_entity_unique_id = f"{mac}_pm25"
        customised_name_unique_id = f"{mac}_temperature"
        disabled_unique_id = f"{mac}_eco2"
        customised_area_icon_unique_id = f"{mac}_humidity"

        for sensor_id, info in em1003_const.SENSOR_TYPES.items():
            unique_id = f"{mac}_{info['key']}"
            extra: dict[str, Any] = {}
            if unique_id == customised_entity_unique_id:
                # A user renamed this entity's entity_id at some point.
                extra["suggested_object_id"] = "living_room_air_pm25"

            reg_entry = entity_registry.async_get_or_create(
                "sensor",
                DOMAIN,
                unique_id,
                config_entry=entry,
                device_id=device.id,
                original_name=info["name"],
                # `object_id_base` (not `original_name`) is what HA's own
                # entity_platform passes to derive a fresh entity_id from
                # the entity's name - matching that here is what makes each
                # non-customised sensor land on its own distinct entity_id
                # (e.g. "..._humidity") instead of all colliding on the
                # bare device-name slug and getting "_2"/"_3"/... suffixes.
                object_id_base=info["name"],
                has_entity_name=True,
                **extra,
            )

            if unique_id == customised_name_unique_id:
                entity_registry.async_update_entity(reg_entry.entity_id, name="Bedroom Temp")
            if unique_id == disabled_unique_id:
                entity_registry.async_update_entity(
                    reg_entry.entity_id, disabled_by=er.RegistryEntryDisabler.USER
                )
            if unique_id == customised_area_icon_unique_id:
                entity_registry.async_update_entity(
                    reg_entry.entity_id, icon="mdi:custom-icon", area_id=area.id
                )

            entity_ids[unique_id] = reg_entry.entity_id

        switch_unique_id = f"{mac}_buzzer"
        switch_entry = entity_registry.async_get_or_create(
            "switch",
            DOMAIN,
            switch_unique_id,
            config_entry=entry,
            device_id=device.id,
            original_name="Buzzer",
            object_id_base="Buzzer",
            has_entity_name=True,
        )
        entity_ids[switch_unique_id] = switch_entry.entity_id

        return LegacyInstall(
            entry=entry,
            mac=mac,
            device_id=device.id,
            entity_ids=entity_ids,
            customised_entity_unique_id=customised_entity_unique_id,
            customised_name_unique_id=customised_name_unique_id,
            disabled_unique_id=disabled_unique_id,
            customised_area_icon_unique_id=customised_area_icon_unique_id,
            area_id=area.id,
            name_by_user=name_by_user,
        )

    return _make


async def _setup_legacy(hass, install: LegacyInstall) -> bool:
    """Run setup against the already-`add_to_hass`-ed legacy entry — the
    moment v1.1.0 starts against a pre-existing install for the first time."""
    result = await hass.config_entries.async_setup(install.entry.entry_id)
    await hass.async_block_till_done()
    return result


def _em1003_entity_ids(entity_registry: er.EntityRegistry) -> set[str]:
    """Every entity_id currently registered under the em1003 platform,
    regardless of which config entry it belongs to."""
    return {e.entity_id for e in entity_registry.entities.values() if e.platform == DOMAIN}


def _em1003_devices(device_registry: dr.DeviceRegistry, mac: str) -> list[dr.DeviceEntry]:
    """Every device registered with the legacy `(DOMAIN, mac)` identifier."""
    return [d for d in device_registry.devices.values() if (DOMAIN, mac) in d.identifiers]


_OPTIONS_PARAMS = pytest.mark.parametrize(
    "options", [{}, {"scan_interval": 120}], ids=["opts_empty", "opts_120"]
)


# ---------------------------------------------------------------------------
# F7-01 .. F7-11: full-integration backward compatibility
# ---------------------------------------------------------------------------


@_OPTIONS_PARAMS
async def test_f7_01_no_new_or_duplicate_entities(hass, ble_world, make_legacy_install, options):
    """F7-01: setup against a pre-populated registry creates zero new
    entities and reuses every legacy unique id, still bound to this entry."""
    install = make_legacy_install(options=options)
    entity_registry = er.async_get(hass)
    pre_ids = _em1003_entity_ids(entity_registry)
    assert len(pre_ids) == 9

    assert await _setup_legacy(hass, install)

    post_ids = _em1003_entity_ids(entity_registry)
    assert len(post_ids) == 9, "entity count changed - new entities were created"
    assert post_ids == pre_ids, "entity registry churned instead of being reused"

    for unique_id, entity_id in install.entity_ids.items():
        domain = entity_id.split(".", 1)[0]
        assert entity_registry.async_get_entity_id(domain, DOMAIN, unique_id) == entity_id
        reg_entry = entity_registry.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.config_entry_id == install.entry.entry_id


@_OPTIONS_PARAMS
async def test_f7_02_entity_ids_preserved(hass, ble_world, make_legacy_install, options):
    """F7-02: the customised entity_id survives, and nothing anywhere gets
    a duplicate `_2`-style suffix."""
    install = make_legacy_install(options=options)
    assert await _setup_legacy(hass, install)

    pm25_entity_id = install.entity_ids[install.customised_entity_unique_id]
    assert pm25_entity_id == "sensor.living_room_air_pm25"

    entity_registry = er.async_get(hass)
    for entity_id in install.entity_ids.values():
        assert not entity_id.endswith("_2"), f"{entity_id} was given a duplicate-looking suffix"
        assert entity_registry.async_get(entity_id) is not None


async def test_f7_03_user_customisations_preserved(hass, ble_world, make_legacy_install):
    """F7-03: the name override, the disabled_by=USER flag and the
    icon/area_id customisation are byte-for-byte unchanged after setup."""
    install = make_legacy_install(options={})
    entity_registry = er.async_get(hass)

    name_entity_id = install.entity_ids[install.customised_name_unique_id]
    disabled_entity_id = install.entity_ids[install.disabled_unique_id]
    area_icon_entity_id = install.entity_ids[install.customised_area_icon_unique_id]

    assert await _setup_legacy(hass, install)

    assert entity_registry.async_get(name_entity_id).name == "Bedroom Temp"
    assert entity_registry.async_get(disabled_entity_id).disabled_by == er.RegistryEntryDisabler.USER
    assert entity_registry.async_get(area_icon_entity_id).icon == "mdi:custom-icon"
    assert entity_registry.async_get(area_icon_entity_id).area_id == install.area_id


@_OPTIONS_PARAMS
async def test_f7_04_device_registry_untouched(hass, ble_world, make_legacy_install, options):
    """F7-04: device count/identifiers/connections/name_by_user unchanged,
    still linked to this config entry."""
    install = make_legacy_install(options=options)
    device_registry = dr.async_get(hass)
    assert len(_em1003_devices(device_registry, install.mac)) == 1

    assert await _setup_legacy(hass, install)

    devices = _em1003_devices(device_registry, install.mac)
    assert len(devices) == 1
    device = devices[0]
    assert device.id == install.device_id
    assert device.identifiers == {(DOMAIN, install.mac)}
    # HA always lower-cases a "mac" connection tuple regardless of the MAC's
    # original case (bleak/HA convention) - this is the real invariant BC-13
    # describes, not a literal string match on `install.mac`.
    assert device.connections == {("mac", install.mac.lower())}
    assert device.name_by_user == install.name_by_user
    assert install.entry.entry_id in device.config_entries


@_OPTIONS_PARAMS
async def test_f7_05_mac_address_data_value_unchanged(hass, ble_world, make_legacy_install, options):
    """F7-05: `entry.data["mac_address"]` keeps its exact case/format."""
    install = make_legacy_install(options=options)
    original_value = install.entry.data[em1003_const.CONF_MAC_ADDRESS]
    assert original_value == install.mac

    assert await _setup_legacy(hass, install)

    assert install.entry.data[em1003_const.CONF_MAC_ADDRESS] == original_value
    assert install.entry.data[em1003_const.CONF_MAC_ADDRESS] == install.mac


async def test_f7_06_only_additive_data_keys_on_success(hass, ble_world, make_legacy_install):
    """F7-06: on a successful (uncached) name read, the only key setup adds
    to `entry.data` is `device_name`."""
    install = make_legacy_install(options={})
    assert await _setup_legacy(hass, install)

    extra_keys = set(install.entry.data) - {em1003_const.CONF_MAC_ADDRESS}
    assert extra_keys <= {em1003_const.CONF_DEVICE_NAME}


async def test_f7_06_only_additive_data_keys_on_name_read_failure(hass, ble_world, make_legacy_install):
    """F7-06 (failure branch): when the name read fails, `entry.data` is
    left with *exactly* the legacy key - nothing is written on failure, so
    the next start retries."""
    ble_world.device_present = False
    install = make_legacy_install(options={})

    assert await _setup_legacy(hass, install)  # BC-16: setup still succeeds

    assert set(install.entry.data) == {em1003_const.CONF_MAC_ADDRESS}


@_OPTIONS_PARAMS
async def test_f7_07_options_untouched(hass, ble_world, make_legacy_install, options):
    """F7-07: setup never writes `entry.options`."""
    install = make_legacy_install(options=options)
    assert await _setup_legacy(hass, install)
    assert dict(install.entry.options) == options


@_OPTIONS_PARAMS
async def test_f7_08_title_version_unique_id_unchanged(hass, ble_world, make_legacy_install, options):
    """F7-08: title / version / minor_version / unique_id are untouched."""
    install = make_legacy_install(options=options)
    entry = install.entry
    title_before = entry.title
    version_before = entry.version
    minor_version_before = entry.minor_version
    unique_id_before = entry.unique_id

    assert await _setup_legacy(hass, install)

    assert entry.title == title_before
    assert entry.version == version_before
    assert entry.minor_version == minor_version_before
    assert entry.unique_id == unique_id_before


async def test_f7_09_disabled_entity_not_polled_into_existence(hass, ble_world, make_legacy_install):
    """F7-09: the disabled entity keeps `disabled_by` and gets no state; the
    other 8 entities do get a state."""
    install = make_legacy_install(options={})
    assert await _setup_legacy(hass, install)

    entity_registry = er.async_get(hass)
    disabled_entity_id = install.entity_ids[install.disabled_unique_id]
    assert entity_registry.async_get(disabled_entity_id).disabled_by == er.RegistryEntryDisabler.USER
    assert hass.states.get(disabled_entity_id) is None

    for unique_id, entity_id in install.entity_ids.items():
        if unique_id == install.disabled_unique_id:
            continue
        assert hass.states.get(entity_id) is not None, f"{entity_id} unexpectedly has no state"


async def test_f7_10_entities_get_states_from_first_poll(hass, ble_world, make_legacy_install):
    """F7-10: the (non-disabled) sensors and the switch pick up real values
    from the very first poll; the switch reflects the polled buzzer state."""
    ble_world.buzzer_state = True
    install = make_legacy_install(options={})

    assert await _setup_legacy(hass, install)

    for sensor_id, info in em1003_const.SENSOR_TYPES.items():
        unique_id = f"{install.mac}_{info['key']}"
        if unique_id == install.disabled_unique_id:
            continue
        entity_id = install.entity_ids[unique_id]
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state not in ("unknown", "unavailable"), f"{entity_id} has no polled value"

    switch_entity_id = install.entity_ids[f"{install.mac}_buzzer"]
    switch_state = hass.states.get(switch_entity_id)
    assert switch_state is not None
    assert switch_state.state == "on"


async def test_f7_11_unload_reload_cycle_preserves_everything(hass, ble_world, make_legacy_install):
    """F7-11: an unload/reload cycle leaves F7-01..F7-08's invariants
    intact - no re-churn, no lost customisation, no rewritten metadata."""
    install = make_legacy_install(options={})
    entry = install.entry
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    pre_ids = _em1003_entity_ids(entity_registry)
    title_before = entry.title
    version_before = entry.version
    minor_version_before = entry.minor_version
    unique_id_before = entry.unique_id

    assert await _setup_legacy(hass, install)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    post_ids = _em1003_entity_ids(entity_registry)
    assert post_ids == pre_ids
    assert len(post_ids) == 9

    assert entry.title == title_before
    assert entry.version == version_before
    assert entry.minor_version == minor_version_before
    assert entry.unique_id == unique_id_before
    assert set(entry.data) - {em1003_const.CONF_MAC_ADDRESS} <= {em1003_const.CONF_DEVICE_NAME}
    assert dict(entry.options) == {}

    devices = _em1003_devices(device_registry, install.mac)
    assert len(devices) == 1
    assert devices[0].name_by_user == install.name_by_user

    name_entity_id = install.entity_ids[install.customised_name_unique_id]
    disabled_entity_id = install.entity_ids[install.disabled_unique_id]
    assert entity_registry.async_get(name_entity_id).name == "Bedroom Temp"
    assert entity_registry.async_get(disabled_entity_id).disabled_by == er.RegistryEntryDisabler.USER

    for unique_id, entity_id in install.entity_ids.items():
        domain = entity_id.split(".", 1)[0]
        assert entity_registry.async_get_entity_id(domain, DOMAIN, unique_id) == entity_id


# ---------------------------------------------------------------------------
# F7-12 .. F7-15: no migration, entity attribute contract, device surface,
# legacy lowercase MAC
# ---------------------------------------------------------------------------


async def test_f7_12_no_migration_path_exists(hass, ble_world, make_legacy_install):
    """F7-12: there is no `async_migrate_entry`; `VERSION == 1`; the entry's
    own `version` is unchanged (still 1) after setup."""
    assert not hasattr(em1003_package, "async_migrate_entry")
    assert EM1003ConfigFlow.VERSION == 1

    install = make_legacy_install(options={})
    assert await _setup_legacy(hass, install)
    assert install.entry.version == 1


async def test_f7_13_entity_attribute_contract(hass, ble_world, make_legacy_install):
    """F7-13: for each of the 8 sensors, has_entity_name/state_class/
    precision/unit/device_class/icon match the pinned contract - even
    though the registry rows here predate this refactor and were only
    minimally seeded, `async_get_or_create` fills in the real values.

    The disabled sensor (F7-09) is excluded: a disabled entity is never
    added to hass, so it never computes `capabilities`/`options` at all -
    that is expected disabled-entity behaviour, not a contract violation,
    and is already covered separately by F7-03/F7-09.
    """
    install = make_legacy_install(options={})
    assert await _setup_legacy(hass, install)

    entity_registry = er.async_get(hass)
    for sensor_id, info in em1003_const.SENSOR_TYPES.items():
        unique_id = f"{install.mac}_{info['key']}"
        if unique_id == install.disabled_unique_id:
            continue
        entity_id = install.entity_ids[unique_id]
        reg_entry = entity_registry.async_get(entity_id)
        assert reg_entry is not None

        assert reg_entry.has_entity_name is True
        assert reg_entry.capabilities.get("state_class") == "measurement"

        expected_precision = _PRECISION_BY_KEY.get(info["key"], 0)
        actual_precision = reg_entry.options.get("sensor", {}).get("suggested_display_precision")
        assert actual_precision == expected_precision, (
            f"{info['key']}: precision {actual_precision} != {expected_precision}"
        )

        # Home Assistant itself silently canonicalises a handful of
        # "ambiguous" unit strings (e.g. the micro-sign "µ" U+00B5 used in
        # SENSOR_TYPES vs. the Greek "μ" U+03BC HA's own constants use) via
        # `AMBIGUOUS_UNITS` - that substitution happens for every
        # integration, so the real contract is "matches after that
        # substitution", not "byte-for-byte identical".
        expected_unit = AMBIGUOUS_UNITS.get(info["unit"], info["unit"])
        assert reg_entry.unit_of_measurement == expected_unit

        expected_device_class = info["device_class"]
        if expected_device_class is None:
            assert reg_entry.original_device_class is None
        else:
            assert reg_entry.original_device_class == expected_device_class

        assert reg_entry.original_icon == info["icon"]


async def test_f7_14_em1003_device_legacy_surface(hass):
    """F7-14: `EM1003Device` keeps its whole legacy public surface, incl.
    positional construction and the deletion of `read_sensor`."""
    device = EM1003Device(hass, MAC, "Legacy Name")

    assert device.mac_address == MAC
    assert device.device_name == "Legacy Name"

    for method_name in ("read_all_sensors", "read_buzzer_state", "set_buzzer_state", "disconnect"):
        method = getattr(device, method_name)
        assert asyncio.iscoroutinefunction(method), f"{method_name} is not a coroutine function"

    assert hasattr(device, "sensor_data")
    assert hasattr(device, "buzzer_state")
    assert hasattr(device, "mac_address")
    assert hasattr(device, "device_name")
    assert not hasattr(device, "read_sensor"), "read_sensor() must be deleted (D8)"


async def test_f7_15_legacy_lowercase_mac_entry(hass, ble_world, make_legacy_install):
    """F7-15: an entry created back when MACs were stored lowercase still
    sets up cleanly and reuses its (lowercase) legacy unique ids."""
    lower_mac = MAC.lower()
    install = make_legacy_install(mac=lower_mac, options={})
    entity_registry = er.async_get(hass)
    pre_ids = _em1003_entity_ids(entity_registry)
    assert len(pre_ids) == 9
    assert all(lower_mac in uid for uid in install.entity_ids)

    assert await _setup_legacy(hass, install)
    assert install.entry.state is ConfigEntryState.LOADED

    post_ids = _em1003_entity_ids(entity_registry)
    assert post_ids == pre_ids
    assert len(post_ids) == 9

    for unique_id, entity_id in install.entity_ids.items():
        domain = entity_id.split(".", 1)[0]
        assert entity_registry.async_get_entity_id(domain, DOMAIN, unique_id) == entity_id

    device_registry = dr.async_get(hass)
    devices = _em1003_devices(device_registry, lower_mac)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, lower_mac)}

    # mac_address is never normalised (BC-03/BC-22): it stays lowercase.
    assert install.entry.data[em1003_const.CONF_MAC_ADDRESS] == lower_mac


# ---------------------------------------------------------------------------
# F7-16 .. F7-19: constant contracts (no hass/BLE needed)
# ---------------------------------------------------------------------------


def test_f7_16_constant_contract_identity():
    """F7-16: the identity constants a legacy entry's `entry.data` /
    `entry.options` keys depend on."""
    assert em1003_const.DOMAIN == "em1003"
    assert em1003_const.CONF_MAC_ADDRESS == "mac_address"
    assert em1003_const.CONF_SCAN_INTERVAL == "scan_interval"
    assert em1003_const.DEFAULT_SCAN_INTERVAL == 60
    assert em1003_const.DEFAULT_NAME == "EM1003"
    assert em1003_const.CONF_DEVICE_NAME == "device_name"


def test_f7_17_constant_contract_sensors():
    """F7-17: `SENSOR_TYPES` ids/keys/names/units/device_classes, in order."""
    assert list(em1003_const.SENSOR_TYPES.keys()) == list(_EXPECTED_SENSOR_TABLE.keys())

    for sensor_id, (key, name, unit, device_class) in _EXPECTED_SENSOR_TABLE.items():
        info = em1003_const.SENSOR_TYPES[sensor_id]
        assert info["id"] == sensor_id
        assert info["key"] == key
        assert info["name"] == name
        assert info["unit"] == unit
        assert info["device_class"] == device_class


def test_f7_18_constant_contract_services():
    """F7-18: the 6 service names, and `services.yaml` declaring exactly
    those 6 with the right required fields."""
    assert em1003_const.SERVICE_SCAN_DEVICE == "scan_device"
    assert em1003_const.SERVICE_READ_CHARACTERISTIC == "read_characteristic"
    assert em1003_const.SERVICE_WRITE_CHARACTERISTIC == "write_characteristic"
    assert em1003_const.SERVICE_LIST_SERVICES == "list_services"
    assert em1003_const.SERVICE_DISCOVER_ALL == "discover_all"
    assert em1003_const.SERVICE_READ_DEVICE_NAME == "read_device_name"

    with SERVICES_YAML_PATH.open("r", encoding="utf-8") as fh:
        services = yaml.safe_load(fh)

    expected_services = {
        "scan_device",
        "read_characteristic",
        "write_characteristic",
        "list_services",
        "discover_all",
        "read_device_name",
    }
    assert set(services.keys()) == expected_services

    for name in expected_services:
        fields = services[name].get("fields", {})
        assert fields.get("mac_address", {}).get("required") is True, f"{name}.mac_address must be required"

    for name in ("read_characteristic", "write_characteristic"):
        fields = services[name].get("fields", {})
        assert fields.get("characteristic_uuid", {}).get("required") is True, (
            f"{name}.characteristic_uuid must be required"
        )

    write_fields = services["write_characteristic"].get("fields", {})
    assert write_fields.get("data", {}).get("required") is True


def test_f7_19_protocol_constant_contract():
    """F7-19: the raw protocol byte values and UUIDs the wire format
    depends on, unchanged."""
    assert em1003_const.CMD_READ_SENSOR == 0x06
    assert em1003_const.CMD_BUZZER == 0x50
    assert em1003_const.CMD_BUZZER_SET_RESPONSE == 0x05
    assert em1003_const.BUZZER_ON == 0x01
    assert em1003_const.BUZZER_OFF == 0x00
    assert em1003_const.DEVICE_NAME_UUID == "00002A00-0000-1000-8000-00805F9B34FB"
    assert em1003_const.EM1003_WRITE_CHAR_UUID == "0000FFF1-0000-1000-8000-00805F9B34FB"
    assert em1003_const.EM1003_NOTIFY_CHAR_UUID == "0000FFF4-0000-1000-8000-00805F9B34FB"


# ---------------------------------------------------------------------------
# F7-X1 .. F7-X3: additional regression guards the matrix implies but does
# not spell out as separate cases
# ---------------------------------------------------------------------------


async def test_f7_x1_scan_interval_option_takes_effect_without_entity_churn(
    hass, ble_world, make_legacy_install
):
    """F7-X1: a legacy entry that already had `options={"scan_interval":
    120}` gets a coordinator honouring that interval on the very first
    setup under the new code - not just "options survive as inert data"
    (F7-07) but "options still do what they did before" - with zero entity
    churn alongside it."""
    install = make_legacy_install(options={"scan_interval": 120})
    entity_registry = er.async_get(hass)
    pre_ids = _em1003_entity_ids(entity_registry)

    assert await _setup_legacy(hass, install)

    coordinator = hass.data[DOMAIN][install.entry.entry_id]["coordinator"]
    assert coordinator.update_interval == timedelta(seconds=120)
    assert _em1003_entity_ids(entity_registry) == pre_ids


async def test_f7_x2_double_reload_is_idempotent(hass, ble_world, make_legacy_install):
    """F7-X2: reloading a legacy entry twice in a row (e.g. two options
    saves back-to-back) never accumulates entities/devices - a regression
    where teardown leaked something would show up as growth here, which a
    single reload (F7-11) might not catch."""
    install = make_legacy_install(options={})
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    pre_entity_ids = _em1003_entity_ids(entity_registry)

    assert await _setup_legacy(hass, install)

    for _ in range(2):
        assert await hass.config_entries.async_reload(install.entry.entry_id)
        await hass.async_block_till_done()

    assert _em1003_entity_ids(entity_registry) == pre_entity_ids
    assert len(_em1003_devices(device_registry, install.mac)) == 1


async def test_f7_x3_unload_keeps_registry_rows(hass, ble_world, make_legacy_install):
    """F7-X3: unloading a legacy entry (e.g. the user disables it, or HA
    restarts and re-sets-it-up) must NOT delete its entities or device from
    the registries - only removing the config entry does that. A regression
    here would silently wipe a user's history/customisation on any restart
    that happens to unload before reloading."""
    install = make_legacy_install(options={})
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    pre_ids = _em1003_entity_ids(entity_registry)

    assert await _setup_legacy(hass, install)

    unload_ok = await hass.config_entries.async_unload(install.entry.entry_id)
    await hass.async_block_till_done()
    assert unload_ok
    assert install.entry.state is ConfigEntryState.NOT_LOADED

    assert _em1003_entity_ids(entity_registry) == pre_ids
    devices = _em1003_devices(device_registry, install.mac)
    assert len(devices) == 1
    assert devices[0].name_by_user == install.name_by_user

    name_entity_id = install.entity_ids[install.customised_name_unique_id]
    assert entity_registry.async_get(name_entity_id).name == "Bedroom Temp"
