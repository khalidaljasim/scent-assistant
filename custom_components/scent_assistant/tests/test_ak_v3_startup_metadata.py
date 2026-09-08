"""AK V3 response-driven startup metadata-chain tests."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


class AKV3StartupMetadataTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        self.device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.device._protocol._v3_mode = True
        self.device._state = PROTOCOL.DiffuserState()
        self.device._ak_v3_metadata_read = None
        self.device._ak_v3_startup_chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(
            1, chain=self.device._ak_v3_startup_chain
        )
        self.device._ak_v3_startup_barrier.armed.set()
        self.device._ak_v3_startup_generation = 1
        self.device._ak_v3_startup_trace_active = False
        self.device._ak_v3_startup_trace = []
        self.device._ak_v3_modern_read = None
        self.device._ak_v3_modern_diagnostic_trace = None
        self.device._recent_notifications = []
        self.device._state_callbacks = []
        self.device._notify_state_changed = lambda: None
        self.device._ble_lock = asyncio.Lock()
        self.device._ble_connected = True
        self.device._ble_client = SimpleNamespace(is_connected=True)
        self.device._ble_notify_subscribed = True
        self.device._ak_v3_entity_platforms_ready = True
        self.device._ble_name = "synthetic"
        self.sent = []

        async def send(frame):
            self.sent.append(frame)
            return True

        self.device._ble_send = send

    async def _drain(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def _reply(self, frame):
        self.device._on_ble_notification(1, bytearray(frame))
        await self._drain()

    async def test_full_chain_advances_only_from_matching_replies(self):
        self.device._state.ak_v3_has_custom_mode = True
        self.device._state.ak_v3_has_lamp = True
        replies = (
            b"\x41", b"\x42Tower", b"\x43Dining", b"\x44" + b"F" * 16, b"\x45Model",
            b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20",
            b"\x47\x00\x0f\x01\x2c", b"\x48" + b"Oil" + b"\x00" * 13, b"\x4b\x00\x02\xbd\x02\xbb",
            b"\x4c", b"\x4d\x03", b"\x4e", b"\x50\x00\x02\x0d\x00\x00\x00\x00",
            b"\x51\x00\x00\x01", b"\x52",
        )
        for reply in replies:
            await self._reply(reply)

        self.assertEqual(
            [
                b"\xC1", b"\xC2", b"\xC3", b"\xC4", b"\xC5", b"\xC6", b"\xC7",
                b"\xC8", b"\xCB", b"\xCC", b"\xCD", b"\xCE", b"\xD0", b"\xD1",
            ],
            self.sent,
        )
        self.assertTrue(self.device._ak_v3_startup_chain.completed.is_set())
        self.assertFalse(self.device._ak_v3_startup_chain.failed)
        self.assertEqual([(15, 300)], self.device.state.grade_table)

    async def test_unrelated_notification_does_not_fail_or_advance_chain(self):
        await self._reply(b"\x4a\x01")

        self.assertEqual([], self.sent)
        self.assertFalse(self.device._ak_v3_startup_chain.failed)
        self.assertFalse(self.device._ak_v3_startup_chain.completed.is_set())

    async def test_trace_redacts_metadata_payloads(self):
        self.device._ak_v3_startup_trace_active = True
        self.device._ak_v3_startup_generation = 7
        self.device._ak_v3_startup_trace = []
        await self._reply(b"\x42Private Device Name")

        trace = next(item for item in self.device._ak_v3_startup_trace if item["direction"] == "RX")
        self.assertEqual("42", trace["opcode"])
        self.assertEqual(20, trace["length"])
        self.assertNotIn("raw_hex", trace)
        self.assertNotIn("Private Device Name", str(trace))

    async def test_malformed_reply_cannot_advance_or_establish_freshness(self):
        await self._reply(b"\x46\x01")

        self.assertEqual([], self.sent)
        self.assertNotIn("grade_limits", self.device.state.ak_v3_metadata_available)
        self.assertFalse(self.device._ak_v3_startup_chain.completed.is_set())

    async def test_out_of_order_replies_each_continue_once(self):
        await self._reply(b"\x44" + b"F" * 16)
        await self._reply(b"\x4b\x00\x02\xbd\x02\xbb")
        await self._reply(b"\x44" + b"F" * 16)
        await self._reply(b"\x4b\x00\x02\xbd\x02\xbb")

        self.assertEqual([b"\xC4", b"\xCB"], self.sent)
        self.assertIn("firmware_version", self.device.state.ak_v3_metadata_available)
        self.assertIn("oil_current_ml", self.device.state.ak_v3_metadata_available)

    async def test_42_through_50_advance_without_optional_41(self):
        for reply in (
            b"\x42Tower", b"\x43Dining", b"\x44" + b"F" * 16, b"\x45Model",
            b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20",
            b"\x47\x00\x0f\x01\x2c", b"\x48" + b"Oil" + b"\x00" * 13,
            b"\x4b\x00\x02\xbd\x02\xbb", b"\x4c", b"\x4d\x03", b"\x4e",
            b"\x50\x00\x02\x0d\x00\x00\x00\x00",
        ):
            await self._reply(reply)

        self.assertEqual(
            [b"\xC2", b"\xC3", b"\xC4", b"\xC5", b"\xC6", b"\xC7", b"\xC8", b"\xCB", b"\xCC", b"\xCD", b"\xCE"],
            self.sent,
        )
        self.assertEqual(5.25, self.device.state.oil_consumption_mlh)

    async def test_nonlamp_completion_requires_no_51_or_52_after_oil(self):
        self.device._state.ak_v3_has_custom_mode = True
        self.device._state.ak_v3_has_oil = True
        for reply in (
            b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20",
            b"\x47\x00\x0f\x01\x2c",
            b"\x4b\x00\x02\xbd\x02\xbb",
            b"\x50\x00\x02\x0d\x00\x00\x00\x00",
        ):
            await self._reply(reply)

        self.assertTrue(self.device._ak_v3_startup_chain.completed.is_set())
        self.assertNotIn(b"\xD0", self.sent)
        self.assertEqual("metadata", self.device._ak_v3_manual_refresh_failure(self.device._ak_v3_startup_chain))

    def test_a316_requirements_follow_authenticated_capabilities(self):
        self.device._state.ak_v3_has_global_control = True
        self.device._state.ak_v3_has_oil = True
        self.device._state.ak_v3_has_custom_mode = True

        self.assertEqual(
            ((0x4D, "control_4d"), (0x4B, "oil_4b"), (0x50, "oil_50"),
             (0x46, "metadata_46"), (0x47, "metadata_47")),
            self.device._ak_v3_required_startup_opcodes(),
        )

    async def test_global_control_completion_requires_fresh_4d(self):
        self.device._state.ak_v3_has_global_control = True
        await self._reply(b"\x4d\x03")

        self.assertTrue(self.device._ak_v3_startup_chain.completed.is_set())
        self.assertEqual(1, self.device._ak_v3_power_generation)
        self.assertTrue(self.device.state.power)

    def test_missing_global_control_response_names_control_stage(self):
        self.device._state.ak_v3_has_global_control = True

        self.assertEqual("control_4d", self.device._ak_v3_manual_refresh_failure(self.device._ak_v3_startup_chain))

    async def test_stale_4d_cannot_change_power_or_complete_current_chain(self):
        self.device._state.ak_v3_has_global_control = True
        self.device._state.power = True
        self.device._ak_v3_startup_generation = 2
        await self._reply(b"\x4d\x00")

        self.assertTrue(self.device.state.power)
        self.assertNotIn(0x4D, self.device._ak_v3_startup_chain.accepted_frames)
        self.assertFalse(self.device._ak_v3_startup_chain.completed.is_set())

    async def test_power_requires_current_generation_4d_freshness(self):
        self.device._state.power = True
        self.device._state.ak_v3_metadata_available.add("power")
        self.device._ak_v3_power_generation = 0
        self.device._ak_v3_startup_chain = None
        await self._reply(b"\x4d\x00")

        self.assertTrue(self.device.state.power)

    def test_disabled_oil_and_custom_stages_are_not_required_but_lamp_is(self):
        self.device._state.ak_v3_has_lamp = True

        self.assertEqual(
            ((0x51, "metadata_51"), (0x52, "metadata_52")),
            self.device._ak_v3_required_startup_opcodes(),
        )

    async def test_missing_41_does_not_fail_after_required_downstream_frames(self):
        chain = self.device._ak_v3_startup_chain
        chain.accepted_frames.update({
            0x4B: b"\x4b\x00\x02\xbd\x02\xbb",
            0x50: b"\x50\x00\x02\x0d\x00\x00\x00\x00",
        })

        self.assertEqual("metadata", self.device._ak_v3_manual_refresh_failure(chain))

    async def test_missing_required_stage_uses_capability_aware_failure_name(self):
        self.device._state.ak_v3_has_custom_mode = True
        chain = self.device._ak_v3_startup_chain
        chain.accepted_frames.update({
            0x4B: b"\x4b\x00\x02\xbd\x02\xbb",
            0x50: b"\x50\x00\x02\x0d\x00\x00\x00\x00",
        })

        self.assertEqual("metadata_46", self.device._ak_v3_manual_refresh_failure(chain))

    async def test_record_boundary_replies_are_order_independent(self):
        await self._reply(b"\x50\x00\x02\x0d\x00\x00\x00\x00")
        await self._reply(b"\x48" + b"Oil" + b"\x00" * 13)

        self.assertEqual([b"\xC8"], self.sent)
        self.assertIn("oil_consumption_mlh", self.device.state.ak_v3_metadata_available)
        self.assertIn("oil_names", self.device.state.ak_v3_metadata_available)

    async def test_live_extension_shapes_advance_once(self):
        await self._reply(b"\x4c\x00\x00\x00")
        await self._reply(b"\x4e\x00\x00\x00\x00")
        await self._reply(b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20\xaa\xbb")

        self.assertEqual([b"\xCC", b"\xCE", b"\xC6"], self.sent)
        self.assertEqual((1, 5, 16, 6, 32), self.device.state.grade_limits)

    async def test_timeout_preserves_accepted_stage_freshness(self):
        await self._reply(b"\x43Dining")
        await self._reply(b"\x48" + b"Oil" + b"\x00" * 13)
        await self._reply(b"\x4b\x00\x02\xbd\x02\xbb")
        await self._reply(b"\x50\x00\x02\x0d\x00\x00\x00\x00")
        chain = self.device._ak_v3_startup_chain

        self.device._invalidate_ak_v3_chain_metadata(chain)

        self.assertEqual("Dining", self.device.state.device_label)
        self.assertEqual(["Oil"], self.device.state.oil_names)
        self.assertEqual(699, self.device.state.oil_current_ml)
        self.assertEqual(5.25, self.device.state.oil_consumption_mlh)
        self.assertIsNone(self.device.state.device_name)

    async def test_conflicting_duplicate_cannot_resend_or_refresh(self):
        await self._reply(b"\x42Tower")
        self.device.state.ak_v3_metadata_available.discard("device_name")
        await self._reply(b"\x42Other")

        self.assertEqual([b"\xC2"], self.sent)
        self.assertNotIn("device_name", self.device.state.ak_v3_metadata_available)

    async def test_47_before_46_reconciles_only_when_max_grade_matches(self):
        await self._reply(b"\x47\x00\x0f\x01\x2c")
        self.assertNotIn("grade_table", self.device.state.ak_v3_metadata_available)
        await self._reply(b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20")

        self.assertEqual([b"\xC7", b"\xC6"], self.sent)
        self.assertIn("grade_table", self.device.state.ak_v3_metadata_available)

    async def test_47_grade_count_mismatch_remains_unavailable(self):
        await self._reply(b"\x47\x00\x0f\x01\x2c")
        await self._reply(b"\x46\x02\x00\x05\x00\x10\x00\x06\x00\x20")

        self.assertIsNone(self.device.state.grade_table)
        self.assertNotIn("grade_table", self.device.state.ak_v3_metadata_available)

    async def test_stale_prearm_and_postrelease_replies_cannot_advance(self):
        chain = self.device._ak_v3_startup_chain
        chain.post_21_sent = False
        await self._reply(b"\x41")
        self.device._ak_v3_startup_generation = 2
        await self._reply(b"\x42Tower")
        self.device._ak_v3_startup_generation = 1
        self.device._ak_v3_startup_barrier.released.set()
        await self._reply(b"\x43Dining")

        self.assertEqual([], self.sent)
        self.assertFalse(chain.accepted_frames)

    def _arm_startup_barrier(self):
        chain = self.device._ak_v3_startup_chain
        barrier = DEVICE.AKV3StartupBarrier(1, chain=chain)
        barrier.armed.set()
        self.device._ak_v3_startup_barrier = barrier
        return barrier, chain

    async def test_startup_waits_for_delayed_41_and_ignores_a1_and_40(self):
        barrier, _chain = self._arm_startup_barrier()
        starts = []

        async def start_schedule_read():
            starts.append("schedule")

        self.device._async_start_ak_v3_modern_read = start_schedule_read
        task = asyncio.create_task(self.device._async_start_ak_v3_startup_reads())
        await self._drain()
        self.assertEqual([], starts)

        await self._reply(b"\xA1")
        await self._reply(b"\x40\x00")
        self.assertEqual([], starts)
        self.assertFalse(barrier.released.is_set())

        await self._reply(b"\x41")
        self.assertEqual(b"\xC1", self.sent[-1])
        self.assertEqual([], starts)

        for reply in (
            b"\x42Tower", b"\x43Dining", b"\x44" + b"F" * 16, b"\x45Model",
            b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20",
            b"\x47\x00\x0f\x01\x2c", b"\x48" + b"Oil" + b"\x00" * 13, b"\x4b\x00\x02\xbd\x02\xbb",
            b"\x4c", b"\x4d\x03", b"\x4e", b"\x50\x00\x02\x0d\x00\x00\x00\x00",
            b"\x51\x00\x00\x01", b"\x52",
        ):
            await self._reply(reply)
        await task

        self.assertTrue(barrier.released.is_set())
        self.assertFalse(barrier.failed)
        self.assertEqual([], starts)

    async def test_missing_optional_52_releases_schedule_without_failure(self):
        barrier, _chain = self._arm_startup_barrier()
        starts = []

        async def start_schedule_read():
            starts.append("schedule")

        self.device._async_start_ak_v3_modern_read = start_schedule_read

        original_timeout = DEVICE.AK_V3_TRANSACTION_READ_SECONDS
        setattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS", 0.01)
        try:
            task = asyncio.create_task(self.device._async_start_ak_v3_startup_reads())
            await self._drain()
            self.assertEqual([], starts)
            await task
        finally:
            setattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS", original_timeout)

        self.assertTrue(barrier.released.is_set())
        self.assertFalse(barrier.failed)
        self.assertEqual([], starts)

    async def test_superseded_barrier_rejects_stale_release(self):
        barrier, _chain = self._arm_startup_barrier()
        wait = asyncio.create_task(self.device._async_wait_for_ak_v3_startup_barrier())
        await self._drain()
        self.device._ak_v3_startup_generation = 2
        barrier.released.set()

        self.assertFalse(await wait)
