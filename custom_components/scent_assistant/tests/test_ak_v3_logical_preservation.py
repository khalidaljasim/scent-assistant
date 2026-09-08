"""Logical AK V3 preservation never uses BLE transport."""
from __future__ import annotations

import asyncio
import unittest

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


class _Store:
    def __init__(self):
        self.saved = None

    async def async_save(self, value):
        self.saved = value

    async def async_load(self):
        return self.saved


class _Hass:
    def async_create_task(self, coro):
        return asyncio.create_task(coro)


class LogicalPreservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        self.device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.device._protocol._v3_mode = True
        self.device._state = PROTOCOL.DiffuserState()
        self.device._hass = _Hass()
        self.device._ak_v3_store = _Store()
        self.device._ak_v3_restored_identities = set()
        self.device._ak_v3_confirmation_metadata = {}
        self.device._ak_v3_slot_lifecycle = {}
        self.device._notify_state_changed = lambda: None

    async def test_logical_slot_is_stale_persisted_and_write_ineligible(self):
        result = await self.device.async_preserve_ak_v3_logical_slot(
            1, 1, enabled=True, start_hour=5, start_minute=0,
            end_hour=5, end_minute=31, days_mask=0x7F, mode=0,
            intensity=10, work_seconds=15, pause_seconds=300,
        )
        await asyncio.sleep(0)
        self.assertTrue(result["success"])
        self.assertEqual("explicit_user_logical", result["provenance"])
        self.assertFalse(result["write_eligible"])
        self.assertEqual("stale", self.device.ak_v3_slot_lifecycle(1, 1))
        self.assertEqual(5, self.device._state.ak_v3_schedules[(1, 1)].end_hour)
        self.assertEqual(31, self.device._state.ak_v3_schedules[(1, 1)].end_minute)
        self.assertEqual("explicit_user_logical", self.device._ak_v3_store.saved["slots"][0]["source"])

    async def test_logical_only_slot_cannot_start_write(self):
        await self.device.async_preserve_ak_v3_logical_slot(
            1, 1, enabled=True, start_hour=5, start_minute=0,
            end_hour=5, end_minute=31, days_mask=0x7F, mode=0,
            intensity=10, work_seconds=15, pause_seconds=300,
        )
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=9)
        self.assertFalse(result["success"])
        self.assertIn("fresh physical confirmation", result["error"])

    async def test_restart_restore_keeps_logical_provenance_stale(self):
        await self.device.async_preserve_ak_v3_logical_slot(
            1, 1, enabled=True, start_hour=5, start_minute=0,
            end_hour=5, end_minute=31, days_mask=0x7F, mode=0,
            intensity=10, work_seconds=15, pause_seconds=300,
        )
        await asyncio.sleep(0)
        restored = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        restored._protocol = PROTOCOL.ScentMarketingAkProtocol()
        restored._protocol._v3_mode = True
        restored._state = PROTOCOL.DiffuserState()
        restored._ak_v3_store = self.device._ak_v3_store
        restored._ak_v3_restored_identities = set()
        restored._ak_v3_confirmation_metadata = {}
        restored._ak_v3_slot_lifecycle = {}
        restored._notify_state_changed = lambda: None
        await restored._async_restore_ak_v3_schedules()
        self.assertEqual("stale", restored.ak_v3_slot_lifecycle(1, 1))
        self.assertEqual("explicit_user_logical", restored._ak_v3_confirmation_metadata[(1, 1)]["source"])
        self.assertEqual("restored_stale", restored._ak_v3_confirmation_metadata[(1, 1)]["physical_status"])

    async def test_logical_save_completes_before_restart_and_blocks_every_write_path(self):
        for slot in range(2, 6):
            raw = bytes([
                0x4A, 1, 0x02, 0x03, slot, slot, 0x03,
                slot, 0, slot + 1, 0, 0x7F, 0, slot, 0, 15, 1, 44,
            ])
            self.device._state.ak_v3_schedules[(1, slot)] = (
                self.device._protocol._parse_v3_schedule(raw)
            )
            self.device._ak_v3_confirmation_metadata[(1, slot)] = {
                "source": "fresh_physical",
            }

        result = await self.device.async_preserve_ak_v3_logical_slot(
            1, 1, enabled=True, start_hour=5, start_minute=0,
            end_hour=5, end_minute=31, days_mask=0x7F, mode=0,
            intensity=10, work_seconds=15, pause_seconds=300,
        )
        self.assertTrue(result["success"])
        saved = {(item["endpoint"], item["slot"]): item for item in self.device._ak_v3_store.saved["slots"]}
        self.assertEqual("explicit_user_logical", saved[(1, 1)]["source"])
        self.assertEqual({(1, slot) for slot in range(1, 6)}, set(saved))

        restored = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        restored._protocol = PROTOCOL.ScentMarketingAkProtocol()
        restored._protocol._v3_mode = True
        restored._state = PROTOCOL.DiffuserState()
        restored._ak_v3_store = self.device._ak_v3_store
        restored._ak_v3_restored_identities = set()
        restored._ak_v3_confirmation_metadata = {}
        restored._ak_v3_slot_lifecycle = {}
        restored._notify_state_changed = lambda: None
        await restored._async_restore_ak_v3_schedules()

        schedule = restored._state.ak_v3_schedules[(1, 1)]
        self.assertTrue(schedule.enabled)
        self.assertEqual((5, 0, 5, 31, 0x7F, 0, 10, 15, 300), (
            schedule.start_hour, schedule.start_minute, schedule.end_hour,
            schedule.end_minute, schedule.days_mask, schedule.mode,
            schedule.intensity, schedule.work_seconds, schedule.pause_seconds,
        ))
        self.assertEqual("stale", restored.ak_v3_slot_lifecycle(1, 1))
        self.assertEqual("explicit_user_logical", restored._ak_v3_confirmation_metadata[(1, 1)]["source"])
        self.assertEqual({(1, slot) for slot in range(1, 6)}, set(restored._state.ak_v3_schedules))

        sent = []

        async def fail_if_called(frame):
            sent.append(frame)
            raise AssertionError("restored schedule must not reach BLE transport")

        restored._ble_send = fail_if_called
        update = await restored.async_update_ak_v3_slot(1, 1, intensity=9)
        intensity = await restored.async_update_ak_v3_slot_intensity(1, 1, 9)
        generic_results = (
            await restored.set_schedule(
                0x7F, 5, 0, 5, 31, 15, 300, enabled=True
            ),
            await restored.set_schedule_enabled(True),
            await restored.set_intensity(9),
            await restored.set_schedule_mode(False),
            await restored.set_work_duration(20),
            await restored.set_pause_duration(240),
        )
        self.assertFalse(update["success"])
        self.assertFalse(intensity["success"])
        self.assertTrue(all(result is False for result in generic_results))
        self.assertEqual([], sent)

    async def test_invalid_identity_does_not_restore(self):
        self.device._ak_v3_store.saved = {"version": 1, "slots": [
            {"endpoint": 1, "slot": 6, "raw_frame": "4a0102030101030500051f7f000a000f012c"},
        ]}
        await self.device._async_restore_ak_v3_schedules()
        self.assertEqual({}, self.device._state.ak_v3_schedules)
