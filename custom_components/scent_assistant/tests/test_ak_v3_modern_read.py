"""Synthetic direct-push AK V3 4A collector tests."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


def _frame(
    slot: int, *, endpoint: int = 1, state: int = 0x03, total: int = 0x03,
    intensity: int | None = None,
) -> bytes:
    return bytes([
        0x4A, endpoint, 0x02, total, slot, slot, state,
        8, 0, 20, 0, 0x7F, 0x01, slot + 3 if intensity is None else intensity, 0, 10, 0, 120,
    ])


class AKV3ModernReadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        self.device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.device._protocol._v3_mode = True
        self.device._device_type = DEVICE.DeviceType.SCENT_MARKETING_AK
        self.device._state = PROTOCOL.DiffuserState()
        self.device._ble_connected = True
        self.device._ble_notify_subscribed = True
        self.device._ak_v3_read_transaction_id = 0
        self.device._ak_v3_action_owner = None
        self.device._ak_v3_action_id = 0
        self.device._ak_v3_modern_read = None
        self.device._ak_v3_modern_diagnostic_trace = None
        self.device._ak_v3_restored_identities = set()
        self.device._ak_v3_confirmation_metadata = {}
        self.device._ak_v3_slot_lifecycle = {}
        self.device._ak_v3_store = None
        self.device._ak_v3_startup_generation = 1
        self.device._ak_v3_current_fields = set()
        self.device._ak_v3_retained_fields = set()
        self.device._ak_v3_retained_generation = None
        self.device._recent_notifications = []
        self.device._recent_commands = []
        self.device._state_callbacks = []
        self.device._notify_state_changed = lambda: None
        self.sent = []

        async def send(frame):
            self.sent.append(frame)
            return True

        self.device._ble_send = send
        self.device._persist_ak_v3_schedules = lambda: None

    async def _drain(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_collector_arms_without_v2_schedule_frames(self):
        await self.device._async_start_ak_v3_modern_read()
        self.assertEqual([], self.sent)
        self.assertIsNotNone(self.device._ak_v3_modern_read)

    async def test_direct_push_collects_five_ordered_records_and_only_optionally_acks(self):
        await self.device._async_start_ak_v3_modern_read()
        for slot in range(1, 6):
            self.device._on_ble_notification(1, bytearray(_frame(slot)))
            await self._drain()

        transaction = self.device._ak_v3_modern_read
        self.assertIsNotNone(transaction)
        self.assertTrue(transaction.records_complete_event.is_set())
        self.assertEqual("complete", transaction.completion_result)
        self.assertEqual({(1, slot) for slot in range(1, 6)}, set(self.device.state.ak_v3_schedules))
        self.assertEqual([b"\xCA\x01" + bytes([slot]) for slot in range(1, 6)], self.sent)
        self.assertFalse(any(frame[:1] in (b"\x83", b"\x89", b"\x86") for frame in self.sent))

    async def test_generation_retains_only_committed_schedule_table(self):
        await self.device._async_start_ak_v3_modern_read()
        for slot in range(1, 5):
            self.device._on_ble_notification(1, bytearray(_frame(slot)))
        self.assertFalse(self.device.ak_v3_read_available("schedules"))
        self.device._on_ble_notification(1, bytearray(_frame(5)))
        self.assertTrue(self.device.ak_v3_read_available("schedules", "fan_aggregate"))
        self.device._ble_connected = False
        self.device._ble_notify_subscribed = False
        self.assertTrue(self.device.ak_v3_read_available("schedules", "fan_aggregate"))
        self.device._ble_connected = True
        self.device._ble_notify_subscribed = True
        self.device._begin_ak_v3_generation()
        self.assertFalse(self.device.ak_v3_read_available("schedules"))

    async def test_out_of_order_or_wrong_endpoint_records_are_not_acknowledged(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._ak_v3_modern_read.expected_endpoint = 1
        self.device._on_ble_notification(1, bytearray(_frame(2)))
        self.device._on_ble_notification(1, bytearray(_frame(1, endpoint=2)))
        await self._drain()
        self.assertEqual([], self.sent)

    async def test_failed_optional_ack_cannot_erase_a_complete_collection(self):
        async def send(_frame):
            return False

        self.device._ble_send = send
        await self.device._async_start_ak_v3_modern_read()
        for slot in range(1, 6):
            self.device._on_ble_notification(1, bytearray(_frame(slot)))
        await self._drain()
        transaction = self.device._ak_v3_modern_read
        self.assertTrue(transaction.records_complete_event.is_set())
        self.assertEqual("complete", transaction.completion_result)

    async def test_4d_fan_activity_and_4a_total_fan_are_independent(self):
        self.device._on_ble_notification(1, bytearray(b"\x4d\x03"))
        self.assertTrue(self.device.state.fan_active)
        self.assertIsNone(self.device.state.fan)
        await self.device._async_start_ak_v3_modern_read()
        self.device._on_ble_notification(1, bytearray(_frame(1, total=0x02)))
        self.assertIsNone(self.device.state.fan)
        self.assertIsNone(self.device.state.diffusion_enabled)
        for slot in range(2, 6):
            self.device._on_ble_notification(1, bytearray(_frame(slot, total=0x02)))
        self.assertTrue(self.device.state.fan)
        self.assertFalse(self.device.state.diffusion_enabled)
        self.assertTrue(self.device.state.fan_active)

    async def test_partial_4a_table_keeps_aggregate_hidden_until_five_unique_slots(self):
        await self.device._async_start_ak_v3_modern_read()
        for slot in range(1, 5):
            self.device._on_ble_notification(1, bytearray(_frame(slot, total=0x03)))
        self.assertIsNone(self.device.state.fan)
        self.assertIsNone(self.device.state.diffusion_enabled)
        self.assertEqual({}, self.device.state.ak_v3_schedules)
        self.device._on_ble_notification(1, bytearray(_frame(5, total=0x03)))
        self.assertTrue(self.device.state.fan)
        self.assertTrue(self.device.state.diffusion_enabled)
        self.assertEqual({(1, slot) for slot in range(1, 6)}, set(self.device.state.ak_v3_schedules))

    async def test_4a_slots_do_not_overwrite_global_intensity(self):
        self.device._state.intensity = 12
        intensities = (10, 6, 8, 4, 6)
        await self.device._async_start_ak_v3_modern_read()
        for slot, intensity in enumerate(intensities, start=1):
            self.device._on_ble_notification(1, bytearray(_frame(slot, intensity=intensity)))
        await self._drain()

        self.assertEqual(12, self.device.state.intensity)
        self.assertEqual(
            intensities,
            tuple(self.device.state.ak_v3_schedules[(1, slot)].intensity for slot in range(1, 6)),
        )

    async def test_identical_4a_table_does_not_notify_state_callbacks(self):
        await self._commit_table()
        callbacks = []
        self.device._notify_state_changed = lambda: callbacks.append(True)

        await self._commit_table()
        await self._drain()

        self.assertEqual([], callbacks)

    async def _commit_table(self):
        await self.device._async_start_ak_v3_modern_read()
        for slot in range(1, 6):
            self.device._on_ble_notification(1, bytearray(_frame(slot)))
        await self._drain()

    async def test_committed_diagnostic_returns_detached_current_table_without_ble(self):
        await self._commit_table()
        callbacks = []
        self.device._notify_state_changed = lambda: callbacks.append(True)

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("committed diagnostic must not use BLE")

        self.device._ble_send = forbidden
        self.device._ble_connect = forbidden
        self.device._teardown_ble_client = forbidden
        result = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")

        self.assertTrue(result["success"])
        self.assertEqual("committed", result["source"])
        self.assertEqual(1, result["generation"])
        self.assertEqual([1, 2, 3, 4, 5], [record["slot"] for record in result["decoded_4a_records"]])
        result["decoded_4a_records"][0]["intensity"] = 99
        result["decoded_4a_records"][0]["raw_frame"] = "changed"
        self.assertEqual(4, self.device.state.ak_v3_schedules[(1, 1)].intensity)
        self.assertEqual(_frame(1).hex(), self.device.state.ak_v3_schedules[(1, 1)].raw_frame.hex())
        self.assertEqual([], callbacks)

    async def test_committed_diagnostic_remains_available_after_intentional_disconnect(self):
        await self._commit_table()
        self.device._ble_connected = False
        self.device._ble_notify_subscribed = False
        result = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertTrue(result["success"])

    async def test_committed_diagnostic_rejects_stale_partial_or_staged_table(self):
        await self._commit_table()
        self.device._begin_ak_v3_generation()
        stale = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertFalse(stale["success"])

        await self._commit_table()
        self.device.state.ak_v3_schedules.pop((1, 5))
        partial = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertFalse(partial["success"])

        await self._commit_table()
        self.device._ak_v3_confirmation_metadata[(1, 1)]["source"] = "explicit_user_logical"
        staged = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertFalse(staged["success"])

    async def test_committed_diagnostic_rejects_malformed_duplicate_and_mismatched_records(self):
        await self._commit_table()
        original = self.device.state.ak_v3_schedules[(1, 1)]
        self.device._state.ak_v3_schedules[(1, 1)] = replace(original, raw_frame=b"\x4A")
        malformed = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertFalse(malformed["success"])

        await self._commit_table()
        self.device._state.ak_v3_schedules[(1, 2)] = self.device.state.ak_v3_schedules[(1, 1)]
        duplicate = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertFalse(duplicate["success"])

        await self._commit_table()
        original = self.device.state.ak_v3_schedules[(1, 1)]
        self.device._state.ak_v3_schedules[(1, 1)] = replace(original, intensity=original.intensity + 1)
        mismatched = await self.device.async_diagnose_ak_v3_schedule_table(1, source="committed")
        self.assertFalse(mismatched["success"])

    async def test_default_diagnostic_still_uses_physical_path(self):
        calls = []
        self.device._ak_v3_transaction_lock = asyncio.Lock()
        self.device._ble_lock = asyncio.Lock()

        async def teardown(*_args, **_kwargs):
            calls.append("teardown")

        async def connect(**_kwargs):
            calls.append("connect")
            return False

        self.device._teardown_ble_client = teardown
        self.device._ble_connect = connect
        result = await self.device.async_diagnose_ak_v3_schedule_table(1)
        self.assertFalse(result["success"])
        self.assertEqual(["teardown", "connect", "teardown"], calls)
