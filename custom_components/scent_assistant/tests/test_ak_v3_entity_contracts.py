"""AK V3 entity and service contracts without Home Assistant runtime access."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


def _load_select_module():
    homeassistant = sys.modules["homeassistant"]
    components = sys.modules["homeassistant.components"]
    select = types.ModuleType("homeassistant.components.select")
    select.SelectEntity = object
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object
    components.select = select
    homeassistant.config_entries = config_entries
    sys.modules.update({
        "homeassistant.components.select": select,
        "homeassistant.config_entries": config_entries,
        "homeassistant.helpers.entity_platform": entity_platform,
    })
    return importlib.import_module("custom_components.scent_assistant.select")


SELECT = _load_select_module()


def _load_button_module():
    homeassistant = sys.modules["homeassistant"]
    components = sys.modules["homeassistant.components"]
    button = types.ModuleType("homeassistant.components.button")
    button.ButtonEntity = object
    const = types.ModuleType("homeassistant.const")

    class EntityCategory(StrEnum):
        DIAGNOSTIC = "diagnostic"

    const.EntityCategory = EntityCategory
    components.button = button
    sys.modules.update({
        "homeassistant.components.button": button,
        "homeassistant.const": const,
    })
    return importlib.import_module("custom_components.scent_assistant.button")


BUTTON = _load_button_module()


def _schedule(slot: int, mode: int = 0):
    return PROTOCOL.ScentMarketingAkProtocol._parse_v3_schedule(bytes([
        0x4A, 1, 0x02, 0x03, slot, slot, 0x03,
        8, 0, 20, 0, 0x7F, mode, 8, 0, 15, 1, 44,
    ]))


class _Device:
    available = True
    protocol_is_v3 = True
    unique_id = "ultra_max_tower"
    name = "Ultra Max Tower"
    device_info = {}
    supports_ak_v3_custom_mode = False
    ak_v3_manual_refresh_available = True
    device_type = DEVICE.DeviceType.SCENT_MARKETING_AK

    def __init__(self) -> None:
        self.state = PROTOCOL.DiffuserState()
        self.state.ak_v3_schedules = {(1, slot): _schedule(slot) for slot in range(1, 6)}
        self.updated = []
        self._sm_metadata = {}
        self._protocol = PROTOCOL.ScentMarketingAkProtocol()

    @property
    def sm_metadata(self):
        return self._sm_metadata

    def register_state_callback(self, _callback) -> None:
        pass

    def ak_v3_read_available(self, *_fields) -> bool:
        return self.available

    async def async_update_ak_v3_slot(self, endpoint, slot, **changes) -> None:
        self.updated.append((endpoint, slot, changes))


class AKV3EntityContractsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = _Device()

    def test_fixed_mode_is_available_for_all_populated_slots_with_stable_ids(self):
        for slot in range(1, 6):
            entity = SELECT.AKV3ScheduleModeSelect(self.device, None, 1, slot)
            self.assertTrue(entity.available)
            self.assertEqual("Fixed", entity.current_option)
            self.assertEqual(["Fixed"], entity.options)
            self.assertEqual(f"ultra_max_tower_schedule_{slot}_mode", entity._attr_unique_id)

    def test_custom_mode_requires_capability_and_usable_limits(self):
        entity = SELECT.AKV3ScheduleModeSelect(self.device, None, 1, 1)
        self.assertEqual(["Fixed"], entity.options)
        self.device.supports_ak_v3_custom_mode = True
        self.device.state.ak_v3_schedules[(1, 1)] = _schedule(1, mode=1)
        self.assertTrue(entity.available)
        self.assertEqual("Custom", entity.current_option)
        self.assertEqual(["Fixed", "Custom"], entity.options)

    async def test_fixed_mode_selection_uses_only_the_normal_slot_encoder_field(self):
        entity = SELECT.AKV3ScheduleModeSelect(self.device, None, 1, 1)
        await entity.async_select_option("Fixed")
        self.assertEqual([(1, 1, {"mode": 0})], self.device.updated)

    def test_v3_time_sync_frame_uses_the_confirmed_opcode_and_fields(self):
        protocol = PROTOCOL.ScentMarketingAkProtocol()
        protocol._v3_mode = True
        from datetime import datetime

        self.assertEqual(
            bytes.fromhex("21061a081d0b2c35"),
            protocol.build_time_sync(datetime(2026, 8, 29, 11, 44, 53)),
        )

    async def test_manual_time_sync_does_not_start_a_schedule_read(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._ble_address = "00:11:22:33:44:55"
        device._ble_has_synced_time = True
        calls = []

        async def connect(**kwargs):
            calls.append(kwargs)
            return True

        device._ble_connect = connect
        self.assertTrue(await device.sync_time())
        self.assertFalse(device._ble_has_synced_time)
        self.assertEqual([{"read_ak_state": False}], calls)

    def test_obsolete_services_are_not_defined(self):
        root = Path("/homeassistant/custom_components/scent_assistant")
        services = (root / "services.yaml").read_text()
        source = "\n".join(
            path.read_text() for path in root.glob("*.py")
        )
        forbidden = (
            "recover_ak_v3_slot_from_raw_" + "baseline",
            "reconcile_ak_v3_" + "slot",
            "E0" + "AA55",
            "SM_AK_V3_" + "COMMIT",
            "bytes([0x" + "C5])",
        )
        for value in forbidden:
            self.assertNotIn(value, services + source)

    def test_restored_entity_factories_remain_registered(self):
        root = Path("/homeassistant/custom_components/scent_assistant")
        platform_sources = "\n".join(
            (root / name).read_text()
            for name in ("switch.py", "time.py", "number.py", "select.py", "sensor.py", "button.py")
        )
        for factory in (
            "DiffuserFanSwitch",
            "DiffuserLockSwitch",
            "DiffuserLampSwitch",
            "AKV3ScheduleEnabledSwitch",
            "AKV3ScheduleDaySwitch",
            "AKV3ScheduleTime",
            "AKV3ScheduleIntensityNumber",
            "AKV3ScheduleDurationNumber",
            "AKV3ScheduleModeSelect",
            "TimeSyncButton",
            "RefreshDiffuserStateButton",
        ):
            self.assertIn(factory, platform_sources)

    async def test_refresh_entity_is_created_without_protocol_or_scan_identity(self):
        self.assertIsNone(self.device._protocol._v3_mode)
        entities = []
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={BUTTON.DOMAIN: {entry.entry_id: self.device}})
        await BUTTON.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual(2, len(entities))
        refresh = entities[1]
        self.assertIsInstance(refresh, BUTTON.RefreshDiffuserStateButton)
        self.device.ak_v3_manual_refresh_available = False
        refresh = BUTTON.RefreshDiffuserStateButton(self.device, None)
        self.assertFalse(refresh.available)
        self.device._protocol._v3_mode = True
        self.device.ak_v3_manual_refresh_available = True
        self.assertTrue(refresh.available)

    async def test_non_ak_entries_receive_an_unavailable_refresh_entity(self):
        self.device._sm_metadata = {"mfr_id": 0x1234}
        self.device.ak_v3_manual_refresh_available = False
        entities = []
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={BUTTON.DOMAIN: {entry.entry_id: self.device}})
        await BUTTON.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual(2, len(entities))
        self.assertFalse(entities[1].available)

    async def test_gw_entries_receive_an_unavailable_refresh_entity(self):
        self.device._sm_metadata = {
            "mfr_id": 0x5942,
            "detected_family": "scent_marketing_gw",
        }
        self.device.ak_v3_manual_refresh_available = False
        entities = []
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={BUTTON.DOMAIN: {entry.entry_id: self.device}})
        await BUTTON.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual(2, len(entities))
        self.assertFalse(entities[1].available)

    async def test_reload_keeps_one_stable_refresh_unique_id(self):
        entry = SimpleNamespace(entry_id="test")
        hass = SimpleNamespace(data={BUTTON.DOMAIN: {entry.entry_id: self.device}})
        first, second = [], []
        await BUTTON.async_setup_entry(hass, entry, first.extend)
        await BUTTON.async_setup_entry(hass, entry, second.extend)
        self.assertEqual([entity._attr_unique_id for entity in first], [
            "ultra_max_tower_time_sync", "ultra_max_tower_refresh_state",
        ])
        self.assertEqual(
            [entity._attr_unique_id for entity in first],
            [entity._attr_unique_id for entity in second],
        )

    def test_refresh_entity_uses_home_assistant_diagnostic_category(self):
        entity = BUTTON.RefreshDiffuserStateButton(self.device, None)
        self.assertIs(entity._attr_entity_category, BUTTON.EntityCategory.DIAGNOSTIC)
        self.assertIsNot(type(entity._attr_entity_category), str)

    def test_slot_update_service_contract_validates_and_forwards_rollback_option(self):
        root = Path("/homeassistant/custom_components/scent_assistant")
        init_source = (root / "__init__.py").read_text()
        services = (root / "services.yaml").read_text()
        self.assertIn("AK_V3_UPDATE_SLOT_SCHEMA", init_source)
        self.assertIn('vol.Optional("rollback_on_failure", default=True): cv.boolean', init_source)
        self.assertIn('rollback_on_failure=call.data["rollback_on_failure"]', init_source)
        self.assertIn("schema=AK_V3_UPDATE_SLOT_SCHEMA", init_source)
        self.assertIn("rollback_on_failure:", services)
