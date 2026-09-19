"""F6 — Config flow, options flow, manifest tests (docs/ble_session_refactor.md
section 8 / 10).

Owned by the F6 test author (see section 11, "File ownership"). This file
implements every case in the F6 matrix (19 cases, ``docs/ble_session_refactor.md``
section 10) plus a handful of additional regression guards marked ``F6-Xn``
for gaps the matrix does not spell out explicitly.

Written against the plan's contract (section 8: ``config_flow.py`` /
``manifest.json`` / ``strings.json``), not against the pre-refactor code —
``custom_components/em1003/config_flow.py``, ``__init__.py``, ``manifest.json``
and the translation files are owned and edited by the HA-side implementer
concurrently with this file being written. As of writing they already match
the plan, so these tests exercise real, passing behaviour rather than an
aspirational contract — but per the task brief, a collection error caused by
another owner's file still being mid-edit would be tolerated (a fixture-name
or syntax error in *this* file would not be).

Run just this file with:
    /home/fooling/repo/em1003/.venv/bin/python -m pytest tests/test_config_flow.py -q
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.em1003 import const as em1003_const
from custom_components.em1003.config_flow import EM1003OptionsFlow

from .conftest import MAC

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "em1003"

# ---------------------------------------------------------------------------
# Local test helpers.
#
# Per the task brief: "If the scaffold lacks a knob you need, build the
# helper locally inside your test file rather than editing tests/fakes.py."
# Neither a discovered-device stand-in nor a BluetoothServiceInfoBleak
# builder exists in the scaffold, so both live here.
# ---------------------------------------------------------------------------


def _discovered(address: str, name: str = "EM1003", rssi: int = -60) -> SimpleNamespace:
    """A stand-in for one row of `bluetooth.async_discovered_service_info`.

    Matches the `.address` / `.name` / `.rssi` duck type documented in
    `tests/fakes.py`'s `world.discovered` knob.
    """
    return SimpleNamespace(address=address, name=name, rssi=rssi)


def _bluetooth_service_info(address: str = MAC, name: str = "EM1003", rssi: int = -60):
    """Build a real `BluetoothServiceInfoBleak` for `async_step_bluetooth`.

    `config_flow.async_step_bluetooth` only reads `.address` off this object
    (to set the unique id) and passes the whole object through to
    `async_step_bluetooth_confirm`'s discovery-info bookkeeping, so a
    minimally-filled real instance (rather than a further stand-in) keeps
    the test honest about the actual type HA hands the flow.
    """
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    device = BLEDevice(address=address, name=name, details={})
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=rssi,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=device,
        advertisement=adv,
        connectable=True,
        time=0.0,
        tx_power=None,
    )


def _mac_schema_validator(schema: vol.Schema) -> Any:
    """Pull the validator for the `mac_address` field out of a flow's schema."""
    for key, validator in schema.schema.items():
        if key == em1003_const.CONF_MAC_ADDRESS:
            return validator
    raise AssertionError(f"{em1003_const.CONF_MAC_ADDRESS} not found in schema: {schema.schema}")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _no_full_setup():
    """Isolate a config/options-flow test from the *real* `async_setup_entry`.

    `hass.config_entries.async_add`/`async_reload` (reached through
    `flow.async_configure` on `CREATE_ENTRY`, and through the options
    update listener) `await` the integration's real `async_setup_entry`
    synchronously before returning — so, on real Home Assistant, creating
    an entry through the flow always drives a full integration setup (an
    extra BLE connection for the first poll), not just the flow's own
    logic. The F6 matrix's connection-counting cases (F6-09, F6-11) are
    about the *flow's* BLE use specifically, and F5/F4 (owned by other
    test files) already cover full-setup connection counts — so tests that
    only care about flow behaviour patch this out to stay a precise,
    deterministic regression guard for the flow alone.
    """
    return patch("custom_components.em1003.async_setup_entry", return_value=True)


# ---------------------------------------------------------------------------
# F6-01 / F6-02 — user step, discovered dropdown vs. free-text MAC
# ---------------------------------------------------------------------------


async def test_f6_01_user_step_with_discovered_devices_shows_dropdown(hass, ble_world):
    """F6-01: two discovered devices -> a `vol.In` dropdown covering both."""
    ble_world.discovered = [
        _discovered("AA:BB:CC:DD:EE:FF", name="EM1003-A"),
        _discovered("11:22:33:44:55:66", name="EM1003-B"),
    ]

    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "user"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    validator = _mac_schema_validator(result["data_schema"])
    assert isinstance(validator, vol.In)
    assert set(validator.container) == {"AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"}
    assert result["description_placeholders"]["discovered"] == "2"


async def test_f6_02_user_step_without_discovered_devices_shows_text_input(hass, ble_world):
    """F6-02: no discovered devices -> a free-text field, not a dropdown."""
    ble_world.discovered = []

    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "user"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    validator = _mac_schema_validator(result["data_schema"])
    # A bare `str` type annotation, not a `vol.In(...)` restricting the value.
    assert validator is str
    assert result["description_placeholders"]["discovered"] == "0"


# ---------------------------------------------------------------------------
# F6-03 / F6-04 — MAC normalisation
# ---------------------------------------------------------------------------


async def test_f6_03_mac_normalisation_dashes_and_case(hass, ble_world, fast_timeouts):
    """F6-03: `aa-bb-cc-dd-ee-ff` -> stored/keyed as `AA:BB:CC:DD:EE:FF`."""
    with _no_full_setup():
        result = await hass.config_entries.flow.async_init(
            em1003_const.DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: "aa-bb-cc-dd-ee-ff"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {em1003_const.CONF_MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry = hass.config_entries.async_entries(em1003_const.DOMAIN)[0]
    assert entry.unique_id == "AA:BB:CC:DD:EE:FF"


async def test_f6_04_mac_normalisation_strips_whitespace(hass, ble_world, fast_timeouts):
    """F6-04: surrounding whitespace is stripped the same way as F6-03."""
    with _no_full_setup():
        result = await hass.config_entries.flow.async_init(
            em1003_const.DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: "  aa:bb:cc:dd:ee:ff  "}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {em1003_const.CONF_MAC_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry = hass.config_entries.async_entries(em1003_const.DOMAIN)[0]
    assert entry.unique_id == "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# F6-05 / F6-06 — duplicate MAC aborts
# ---------------------------------------------------------------------------


async def test_f6_05_duplicate_mac_aborts(hass, ble_world, mock_entry, fast_timeouts):
    """F6-05: a MAC already configured (same case) aborts `already_configured`."""
    existing = mock_entry(mac=MAC)
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: MAC}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_f6_06_duplicate_against_legacy_lowercase_entry(hass, ble_world, fast_timeouts):
    """F6-06: a pre-existing lowercase-MAC entry still blocks a new uppercase one.

    `async_set_unique_id` / `_abort_if_unique_id_configured` only catch an
    *exact* string match, so this exercises the extra case-insensitive scan
    (BC-22) the plan requires on top of that.
    """
    legacy_mac = MAC.lower()
    legacy = MockConfigEntry(
        domain=em1003_const.DOMAIN,
        data={em1003_const.CONF_MAC_ADDRESS: legacy_mac},
        unique_id=legacy_mac,
        title=f"EM1003 ({legacy_mac})",
    )
    legacy.add_to_hass(hass)
    legacy_data_before = dict(legacy.data)
    legacy_unique_id_before = legacy.unique_id
    legacy_title_before = legacy.title

    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: MAC}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The legacy entry itself must be untouched by the aborted attempt.
    assert dict(legacy.data) == legacy_data_before
    assert legacy.unique_id == legacy_unique_id_before
    assert legacy.title == legacy_title_before
    assert hass.config_entries.async_entries(em1003_const.DOMAIN) == [legacy]


# ---------------------------------------------------------------------------
# F6-07 / F6-08 / F6-09 — device-name read and title on config
# ---------------------------------------------------------------------------


async def test_f6_07_title_on_successful_name_read(hass, ble_world, fast_timeouts):
    """F6-07: a successful BLE name read produces `"<name> (<mac[-8:]>)"`."""
    ble_world.device_name = "3GCG300ZY4"

    with _no_full_setup():
        result = await hass.config_entries.flow.async_init(
            em1003_const.DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: MAC}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"3GCG300ZY4 ({MAC[-8:]})"


async def test_f6_08_title_on_failed_name_read(hass, ble_world, fast_timeouts):
    """F6-08: a failed BLE name read falls back to `"EM1003 (<mac>)"`."""
    ble_world.device_present = False  # "device not found" -> async_read_device_name -> None

    with _no_full_setup():
        result = await hass.config_entries.flow.async_init(
            em1003_const.DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: MAC}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{em1003_const.DEFAULT_NAME} ({MAC})"


async def test_f6_09_name_read_goes_through_device_py(hass, ble_world, fast_timeouts):
    """F6-09: the config-flow name read is visible on the `device.py` choke point.

    (BC-21: every BLE connection of the integration, including this one,
    goes through the fake's `establish_connection` patch on `device.py`.)
    Isolated from the entry's own full setup (see `_no_full_setup`) so this
    counts *only* the flow's own connection, exactly as the matrix asks.
    """
    before = ble_world.connect_attempts

    with _no_full_setup():
        result = await hass.config_entries.flow.async_init(
            em1003_const.DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: MAC}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ble_world.connect_attempts == before + 1
    assert ble_world.links_are_clean


# ---------------------------------------------------------------------------
# F6-10 / F6-11 / F6-12 — bluetooth discovery flow
# ---------------------------------------------------------------------------


async def test_f6_10_bluetooth_discovery_shows_confirm(hass, ble_world):
    """F6-10: a bluetooth discovery shows the confirm-only step."""
    info = _bluetooth_service_info(address=MAC)

    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "bluetooth"}, data=info
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"].get("confirm_only") is True
    assert flow["context"].get("unique_id") == MAC


async def test_f6_11_bluetooth_confirm_creates_entry(hass, ble_world):
    """F6-11: confirming creates the entry with the bare MAC and `DEFAULT_NAME`
    title — unchanged behaviour: no BLE name read happens in this path."""
    info = _bluetooth_service_info(address=MAC)
    before = ble_world.connect_attempts

    with _no_full_setup():
        result = await hass.config_entries.flow.async_init(
            em1003_const.DOMAIN, context={"source": "bluetooth"}, data=info
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {em1003_const.CONF_MAC_ADDRESS: MAC}
    assert result["title"] == em1003_const.DEFAULT_NAME
    # Unchanged behaviour means no BLE traffic was introduced by the refactor.
    assert ble_world.connect_attempts == before


async def test_f6_12_bluetooth_discovery_of_configured_device_aborts(hass, ble_world, mock_entry):
    """F6-12: discovering an already-configured device aborts."""
    existing = mock_entry(mac=MAC)
    existing.add_to_hass(hass)
    info = _bluetooth_service_info(address=MAC)

    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "bluetooth"}, data=info
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# F6-13 .. F6-17 — options flow: open, validate, save, live reload
# ---------------------------------------------------------------------------


async def test_f6_13_options_flow_opens_without_attributeerror(hass, ble_world, mock_entry, setup_integration):
    """F6-13 (D6): the options dialog opens on current HA — no `AttributeError`
    from `OptionsFlow.__init__` assigning the read-only `config_entry` property."""
    entry = mock_entry()
    assert await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    default = _mac_schema_validator  # not used; keep import-linters happy
    schema = result["data_schema"]
    for key in schema.schema:
        if key == em1003_const.CONF_SCAN_INTERVAL:
            assert key.default() == em1003_const.DEFAULT_SCAN_INTERVAL
            break
    else:
        raise AssertionError("scan_interval not present in options schema")


async def test_f6_x2_options_flow_default_reflects_existing_option(hass, ble_world, mock_entry, setup_integration):
    """F6-X1: the options form's default is the entry's *current* interval,
    not always the global default (a plausible over-simplification bug)."""
    entry = mock_entry(options={em1003_const.CONF_SCAN_INTERVAL: 45})
    assert await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    schema = result["data_schema"]
    for key in schema.schema:
        if key == em1003_const.CONF_SCAN_INTERVAL:
            assert key.default() == 45
            break
    else:
        raise AssertionError("scan_interval not present in options schema")


@pytest.mark.parametrize("bad_value", [9, 601])
async def test_f6_14_options_range_validation_rejects_out_of_range(
    hass, ble_world, mock_entry, setup_integration, bad_value
):
    """F6-14: 9 and 601 are both outside `10..600` and must not be accepted."""
    entry = mock_entry()
    assert await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    # The flow's own vol.Range validator is what must reject the value —
    # exercise it exactly as async_configure would apply it.
    with pytest.raises(vol.Invalid):
        schema({em1003_const.CONF_SCAN_INTERVAL: bad_value})

    # And the entry's options must not have moved as a side effect.
    assert entry.options == {}


@pytest.mark.parametrize("good_value", [10, 600])
async def test_f6_15_options_bounds_are_accepted(hass, ble_world, mock_entry, setup_integration, good_value):
    """F6-15: the inclusive bounds 10 and 600 are both accepted."""
    entry = mock_entry()
    assert await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {em1003_const.CONF_SCAN_INTERVAL: good_value}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[em1003_const.CONF_SCAN_INTERVAL] == good_value


async def test_f6_16_options_save_triggers_live_reload(hass, ble_world, mock_entry, setup_integration):
    """F6-16 (D6): saving new options reloads the entry so the *live*
    coordinator's update_interval changes without a HA restart."""
    entry = mock_entry()  # default options -> 60s
    assert await setup_integration(hass, entry)

    data_before = dict(entry.data)
    title_before = entry.title
    old_coordinator = hass.data[em1003_const.DOMAIN][entry.entry_id]["coordinator"]
    assert old_coordinator.update_interval == timedelta(seconds=60)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {em1003_const.CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {em1003_const.CONF_SCAN_INTERVAL: 120}
    assert entry.state is ConfigEntryState.LOADED

    new_coordinator = hass.data[em1003_const.DOMAIN][entry.entry_id]["coordinator"]
    assert new_coordinator.update_interval == timedelta(seconds=120)
    # A reload really happened: a fresh coordinator instance replaced the old one.
    assert new_coordinator is not old_coordinator

    assert dict(entry.data) == data_before
    assert entry.title == title_before


async def test_f6_17_reload_from_options_listener_does_not_duplicate_entities(
    hass, ble_world, mock_entry, setup_integration
):
    """F6-17: the options-triggered reload must not create duplicate/orphaned
    entities — same count, same entity ids, before and after."""
    entry = mock_entry()
    assert await setup_integration(hass, entry)

    registry = er.async_get(hass)
    entries_before = {
        e.entity_id: e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert len(entries_before) == 9  # 8 sensors + 1 switch

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {em1003_const.CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    entries_after = {
        e.entity_id: e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert entries_after == entries_before


# ---------------------------------------------------------------------------
# F6-18 — manifest sanity
# ---------------------------------------------------------------------------


def test_f6_18_manifest_sanity():
    """F6-18: manifest.json matches the plan's D5/G5 requirements exactly."""
    manifest = _load_json(INTEGRATION_DIR / "manifest.json")

    assert manifest["domain"] == "em1003"
    assert manifest["config_flow"] is True
    assert manifest["dependencies"] == ["bluetooth_adapters"]
    assert manifest["iot_class"] == "local_polling"
    assert manifest["integration_type"] == "device"
    assert manifest["bluetooth"] == [{"local_name": "EM1003*"}, {"local_name": "720*"}]

    # version must be a valid, non-empty version string equal to const.VERSION
    # (never a hard-coded literal per section 1.3, so we compare, not assert "1.1.0").
    from awesomeversion import AwesomeVersion

    version = manifest["version"]
    AwesomeVersion(version)  # raises if not parseable
    assert version == em1003_const.VERSION


def test_f6_x3_manifest_documentation_and_requirements_present(hass):
    """F6-X2: `documentation`/`requirements`/`name` stay sane and additive-only
    (guards against an editor accidentally dropping a required manifest key)."""
    manifest = _load_json(INTEGRATION_DIR / "manifest.json")

    assert manifest["documentation"] == "https://github.com/fooling/em1003"
    assert isinstance(manifest["requirements"], list) and manifest["requirements"]
    assert "codeowners" in manifest
    assert manifest["name"]


# ---------------------------------------------------------------------------
# F6-19 — translation keys match the flows
# ---------------------------------------------------------------------------


def _flow_key_paths(doc: dict) -> set[str]:
    """Flatten the handful of keys F6-19 cares about into dotted paths."""
    paths = set()

    def _walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{prefix}.{key}" if prefix else key)
        else:
            paths.add(prefix)

    _walk(doc, "")
    return paths


REQUIRED_TRANSLATION_KEYS = (
    "config.step.user",
    "config.step.bluetooth_confirm",
    "config.abort.already_configured",
    "options.step.init.data.scan_interval",
)


@pytest.mark.parametrize(
    "relative_path",
    [
        "strings.json",
        "translations/en.json",
        "translations/zh-Hans.json",
    ],
)
def test_f6_19_translation_files_have_required_flow_keys(relative_path):
    """F6-19: every translation file has the keys the flows actually use."""
    doc = _load_json(INTEGRATION_DIR / relative_path)
    paths = _flow_key_paths(doc)
    for required in REQUIRED_TRANSLATION_KEYS:
        assert any(p == required or p.startswith(required + ".") for p in paths), (
            f"{relative_path} is missing required key path {required!r}"
        )


def test_f6_19b_translation_files_have_identical_key_sets():
    """F6-19: `strings.json`, `en.json` and `zh-Hans.json` expose the same
    structural keys (values may be translated, keys must match)."""

    def _key_skeleton(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: _key_skeleton(value) for key, value in sorted(node.items())}
        return None

    strings_doc = _load_json(INTEGRATION_DIR / "strings.json")
    en_doc = _load_json(INTEGRATION_DIR / "translations" / "en.json")
    zh_doc = _load_json(INTEGRATION_DIR / "translations" / "zh-Hans.json")

    strings_skeleton = _key_skeleton(strings_doc)
    assert strings_skeleton == _key_skeleton(en_doc)
    assert strings_skeleton == _key_skeleton(zh_doc)


# ---------------------------------------------------------------------------
# Additional F6-Xn cases beyond the matrix
# ---------------------------------------------------------------------------


async def test_f6_x1_name_read_hang_does_not_block_the_config_flow(hass, ble_world, fast_timeouts):
    """F6-X3 (D2 regression, in the config-flow context): if the device-name
    read hangs at the GATT read, the flow must still finish (bounded by
    `BLE_READ_CHAR_TIMEOUT`, shrunk by `fast_timeouts`) rather than freezing
    the whole config flow forever."""
    ble_world.hang_at.add("read_char")

    async def _run_flow():
        with _no_full_setup():
            result = await hass.config_entries.flow.async_init(
                em1003_const.DOMAIN, context={"source": "user"}
            )
            return await hass.config_entries.flow.async_configure(
                result["flow_id"], {em1003_const.CONF_MAC_ADDRESS: MAC}
            )

    result = await asyncio.wait_for(_run_flow(), timeout=2.0)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The hang meant no usable name -> falls back exactly like F6-08.
    assert result["title"] == f"{em1003_const.DEFAULT_NAME} ({MAC})"


async def test_f6_x4_options_flow_never_opens_a_ble_connection(hass, ble_world, mock_entry, setup_integration):
    """F6-X4: opening and submitting the options form must not itself touch
    BLE — only the coordinator's own poll (before and after reload) should."""
    entry = mock_entry()
    assert await setup_integration(hass, entry)
    await hass.async_block_till_done()

    before = ble_world.connect_attempts

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Opening the form itself (before any submission) must be BLE-free.
    assert ble_world.connect_attempts == before

    await hass.config_entries.options.async_configure(
        result["flow_id"], {em1003_const.CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    # The reload's own first poll is expected; nothing beyond exactly one
    # extra connection should have happened as a side effect of the flow.
    assert ble_world.connect_attempts == before + 1


def test_f6_x5_options_flow_has_no_own_init(hass):
    """F6-X5 (root cause of D6): `EM1003OptionsFlow` must not define its own
    `__init__` — that is precisely what assigned the read-only
    `config_entry` property on current HA and broke the options dialog."""
    assert "__init__" not in EM1003OptionsFlow.__dict__


async def test_f6_x6_config_flow_version_is_1(hass):
    """F6-X6: `ConfigFlow.VERSION == 1` (no migration was introduced) — a
    narrow, F6-local slice of BC-02, whose full check lives in F7."""
    from custom_components.em1003.config_flow import EM1003ConfigFlow

    assert EM1003ConfigFlow.VERSION == 1


# ---------------------------------------------------------------------------
# Review-round regression guards (see docs/ble_session_refactor.md, "Review round")
# ---------------------------------------------------------------------------


async def test_r_bluetooth_discovery_aborts_for_legacy_lowercase_entry(
    hass, ble_world, mock_entry, fast_timeouts
):
    """BC-22 on the *discovery* path, not just `async_step_user`.

    HA/BlueZ reports the address uppercase and
    `_abort_if_unique_id_configured()` is an exact string compare, so a
    legacy entry whose MAC/unique id was stored lowercase is invisible to
    it. Without the case-insensitive scan the discovery card is shown for a
    device the user already has, and confirming it creates a second entry
    (18 entities instead of 9, both merged onto one device row). The
    legacy entry itself is never rewritten.
    """
    legacy = mock_entry(mac=MAC.lower())
    legacy.add_to_hass(hass)

    info = _bluetooth_service_info(address=MAC)
    result = await hass.config_entries.flow.async_init(
        em1003_const.DOMAIN, context={"source": "bluetooth"}, data=info
    )
    if result["type"] is FlowResultType.FORM:
        # The card was shown for an already-configured device; the user confirms.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(em1003_const.DOMAIN)
    assert result["type"] is FlowResultType.ABORT, (
        "discovery of an already-configured (legacy lowercase) device must abort, got "
        f"{result['type']}; entries are now {[e.unique_id for e in entries]}"
    )
    assert result["reason"] == "already_configured"
    assert len(entries) == 1
    # The legacy entry is left exactly as it was.
    assert entries[0].unique_id == MAC.lower()
    assert entries[0].data[em1003_const.CONF_MAC_ADDRESS] == MAC.lower()


def test_r_hacs_declares_the_real_minimum_ha_version():
    """HACS must not offer this version to a core that cannot run it.

    HACS gates a download on `hacs.json`'s `homeassistant` key. The refactor
    needs `DataUpdateCoordinator(config_entry=...)` (HA 2024.11+) and the
    base `OptionsFlow.config_entry` property (HA 2024.12+), so anything
    lower would let a working v1.0.3 install update itself into
    SETUP_ERROR.
    """
    hacs = _load_json(INTEGRATION_DIR.parents[1] / "hacs.json")
    declared = tuple(int(part) for part in hacs["homeassistant"].split("."))
    assert declared >= (2024, 12, 0), (
        f"hacs.json declares min HA {hacs['homeassistant']}, but the integration needs 2024.12.0"
    )
