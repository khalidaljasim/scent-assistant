"""AK V3 generation-owned login and subscribed-session tests."""
from __future__ import annotations

import asyncio
import unittest

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


def _login_reply(*, check_password: int = 0) -> bytes:
    return b"\x8fOK_V3.0" + b"\x00" * 4 + bytes([check_password, 0, 0x80])


def _short_v3_login_reply() -> bytes:
    return b"\x8fOK_V3.0"


class AKV3LoginLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        self.device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.device._state = PROTOCOL.DiffuserState()
        self.device._recent_notifications = []
        self.device._recent_commands = []
        self.device._state_callbacks = []
        self.device._notify_state_changed = lambda: None
        self.device._ak_v3_startup_trace_active = False
        self.device._ak_v3_startup_generation = 1
        self.device._ak_v3_startup_chain = None
        self.device._ak_v3_startup_barrier = None
        self.device._ak_v3_modern_read = None
        self.device._ak_v3_modern_diagnostic_trace = None
        self.device._ak_v3_metadata_read = None
        self.device._ble_connection_stage = "ready"
        self.device._ble_lock = asyncio.Lock()
        self.device._ble_disconnect_task = None
        self.device._ble_reconnect_task = None
        self.sent = []

        async def send(frame):
            self.sent.append(frame)
            return True

        self.device._ble_send = send

    async def _complete(self, reply: bytes, generation: int = 1):
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(generation)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(generation)
        task = asyncio.create_task(self.device._async_complete_ak_v3_login(generation))
        await asyncio.sleep(0)
        self.device._on_ble_notification(1, bytearray(reply))
        self.assertTrue(await task)

    async def test_primary_success_sends_no_fallback_and_arms_before_21(self):
        await self._complete(_login_reply())

        self.assertEqual(1, len(self.sent))
        self.assertEqual(b"\x21", self.sent[0][:1])
        self.assertTrue(self.device._ak_v3_startup_chain is not None)
        self.device._on_ble_notification(1, bytearray(b"\x41"))
        await asyncio.sleep(0)
        self.assertEqual(b"\xC1", self.sent[-1])

    async def test_timeout_sends_exactly_one_fallback_and_requires_success_before_21(self):
        original_timeout = DEVICE.AK_V3_LOGIN_TIMEOUT_SECONDS
        setattr(DEVICE, "AK_V3_LOGIN_TIMEOUT_SECONDS", 0.05)
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        task = asyncio.create_task(self.device._async_complete_ak_v3_login(1))
        try:
            await asyncio.sleep(0.06)
            fallback = self.device._protocol.build_login_secondary_v3()
            self.assertEqual([fallback], self.sent)
            self.assertFalse(self.device._ak_v3_startup_chain is not None)
            self.device._on_ble_notification(1, bytearray(_login_reply()))
            self.assertTrue(await task)
        finally:
            setattr(DEVICE, "AK_V3_LOGIN_TIMEOUT_SECONDS", original_timeout)
        self.assertEqual([fallback], self.sent[:1])
        self.assertEqual(b"\x21", self.sent[1][:1])

    async def test_check_password_three_sends_exactly_one_fallback(self):
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        task = asyncio.create_task(self.device._async_complete_ak_v3_login(1))
        await asyncio.sleep(0)
        self.device._on_ble_notification(1, bytearray(_login_reply(check_password=3)))
        await asyncio.sleep(0)
        fallback = self.device._protocol.build_login_secondary_v3()
        self.assertEqual([fallback], self.sent)
        self.device._on_ble_notification(1, bytearray(_login_reply()))
        self.assertTrue(await task)
        self.assertEqual(1, self.sent.count(fallback))

    async def test_short_v3_reply_immediately_sends_one_fallback(self):
        original_timeout = DEVICE.AK_V3_LOGIN_TIMEOUT_SECONDS
        setattr(DEVICE, "AK_V3_LOGIN_TIMEOUT_SECONDS", 1)
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        task = asyncio.create_task(self.device._async_complete_ak_v3_login(1))
        try:
            await asyncio.sleep(0)
            self.device._on_ble_notification(1, bytearray(_short_v3_login_reply()))
            await asyncio.sleep(0)
            fallback = self.device._protocol.build_login_secondary_v3()
            self.assertEqual([fallback], self.sent)
            self.device._on_ble_notification(1, bytearray(_login_reply()))
            self.assertTrue(await task)
        finally:
            setattr(DEVICE, "AK_V3_LOGIN_TIMEOUT_SECONDS", original_timeout)
        self.assertEqual(1, self.sent.count(fallback))

    async def test_manual_short_v3_reply_fallback_never_arms_startup_chain(self):
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        task = asyncio.create_task(
            self.device._async_complete_ak_v3_login(1, arm_startup_chain=False)
        )
        await asyncio.sleep(0)
        self.device._on_ble_notification(1, bytearray(_short_v3_login_reply()))
        await asyncio.sleep(0)
        fallback = self.device._protocol.build_login_secondary_v3()
        self.assertEqual([fallback], self.sent)
        self.device._on_ble_notification(1, bytearray(_login_reply()))
        self.assertTrue(await task)
        self.assertIsNone(self.device._ak_v3_startup_chain)
        self.assertFalse(any(frame[:1] == b"\x21" for frame in self.sent))

    async def test_manual_primary_login_success_never_arms_or_syncs_startup(self):
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        task = asyncio.create_task(
            self.device._async_complete_ak_v3_login(1, arm_startup_chain=False)
        )
        await asyncio.sleep(0)
        self.device._on_ble_notification(1, bytearray(_login_reply()))
        self.assertTrue(await task)
        self.assertEqual([], self.sent)
        self.assertIsNone(self.device._ak_v3_startup_chain)

    async def test_startup_login_arms_before_21_and_accepts_replies_during_its_write(self):
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        self.device._ak_v3_restored_identities = set()
        self.device._ak_v3_confirmation_metadata = {}
        self.device._ak_v3_slot_lifecycle = {}
        self.device._ak_v3_store = None
        self.device._device_type = DEVICE.DeviceType.SCENT_MARKETING_AK
        self.device._ble_connected = True
        self.device._ble_notify_subscribed = True
        schedules = [
            bytes([0x4A, 1, 2, 3, slot, slot, 3, 8, 0, 20, 0, 0x7F, 0, slot, 0, 15, 1, 44])
            for slot in range(1, 6)
        ]

        async def send(frame):
            self.sent.append(frame)
            if frame[:1] == b"\x21":
                self.assertIsNotNone(self.device._ak_v3_startup_chain)
                self.assertIsNotNone(self.device._ak_v3_modern_read)
                for schedule in schedules:
                    self.device._on_ble_notification(1, bytearray(schedule))
            return True

        self.device._ble_send = send
        task = asyncio.create_task(self.device._async_complete_ak_v3_login(1, arm_startup_chain=True))
        await asyncio.sleep(0)
        self.device._on_ble_notification(1, bytearray(_login_reply()))

        self.assertTrue(await task)
        await asyncio.sleep(0)
        self.assertEqual(1, sum(frame[:1] == b"\x21" for frame in self.sent))
        self.assertEqual(
            [b"\xCA\x01" + bytes([slot]) for slot in range(1, 6)],
            [frame for frame in self.sent if frame[:1] == b"\xCA"],
        )
        self.assertTrue(self.device.ak_v3_read_available("schedules", "fan_aggregate"))

    async def test_fallback_reply_before_write_completion_is_accepted(self):
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        fallback = self.device._protocol.build_login_secondary_v3()

        async def send(frame):
            self.sent.append(frame)
            if frame == fallback:
                self.device._on_ble_notification(1, bytearray(_login_reply()))
            return True

        self.device._ble_send = send
        task = asyncio.create_task(self.device._async_complete_ak_v3_login(1))
        await asyncio.sleep(0)
        self.device._on_ble_notification(1, bytearray(_short_v3_login_reply()))

        self.assertTrue(await task)
        self.assertEqual([fallback], self.sent[:1])
        self.assertEqual(b"\x21", self.sent[1][:1])

    async def test_stale_generation_reply_cannot_start_fallback(self):
        self.device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1)
        self.device._ak_v3_startup_generation = 2
        self.device._on_ble_notification(1, bytearray(_short_v3_login_reply()))

        self.assertFalse(self.device._ak_v3_login.response_event.is_set())
        self.assertEqual([], self.sent)

    async def test_late_generation_cannot_arm_or_send(self):
        self.device._ak_v3_login = DEVICE.AKV3LoginState(2)
        self.assertFalse(await self.device._async_complete_ak_v3_login(1))
        self.assertEqual([], self.sent)
        self.assertIsNone(self.device._ak_v3_startup_chain)

    async def test_trace_activation_is_bounded_and_payload_free(self):
        self.device._activate_ak_v3_startup_trace()
        self.device._record_ak_v3_startup_trace("TX", b"\x8f8888OK01")
        self.device._record_ak_v3_startup_trace("RX", b"\x42Private Device Name")
        self.device._activate_ak_v3_startup_trace()

        trace = self.device._ak_v3_startup_trace
        self.assertEqual(2, trace[0]["generation"])
        self.assertEqual("TX", trace[1]["direction"])
        self.assertEqual("RX", trace[2]["direction"])
        self.assertEqual({"timestamp", "generation", "event", "direction", "stage", "opcode", "length", "expected"}, set(trace[1]))
        self.assertNotIn("8888", str(trace))
        self.assertNotIn("Private Device Name", str(trace))

    async def test_ak_v3_keeps_subscription_while_other_protocols_keep_idle_lifecycle(self):
        self.device._protocol._v3_mode = True
        self.device._schedule_disconnect()
        self.assertIsNone(self.device._ble_disconnect_task)

        self.device._protocol._v3_mode = False
        self.device._schedule_disconnect()
        task = self.device._ble_disconnect_task
        self.assertIsNotNone(task)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_unexpected_disconnect_invalidates_once_and_starts_one_recovery(self):
        self.device._ble_disconnect_expected = False
        self.device._ble_connected = True
        self.device._ble_notify_subscribed = True
        self.device._state.power = True
        self.device._on_ble_disconnected(None)
        recovery = self.device._ble_reconnect_task
        self.device._on_ble_disconnected(None)

        self.assertIsNone(self.device._state.power)
        self.assertFalse(self.device._ble_notify_subscribed)
        self.assertIs(recovery, self.device._ble_reconnect_task)
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
