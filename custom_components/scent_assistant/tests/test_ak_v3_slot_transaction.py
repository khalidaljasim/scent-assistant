"""Synthetic Ultra Max Tower schedule flow tests; no BLE hardware."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from types import SimpleNamespace


def _load_device_module():
    bleak = types.ModuleType("bleak")

    class BleakError(Exception):
        pass

    bleak.BleakClient = bleak.BleakScanner = object
    bleak.BleakError = BleakError
    retry = types.ModuleType("bleak_retry_connector")

    async def establish_connection(*args, **kwargs):
        raise AssertionError("synthetic tests must not connect")

    retry.establish_connection = establish_connection
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    core = types.ModuleType("homeassistant.core")
    const = types.ModuleType("homeassistant.const")
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        def __init__(self, *args, **kwargs):
            pass

    storage.Store = Store
    core.HomeAssistant = object
    core.CoreState = types.SimpleNamespace(running="running")
    core.callback = lambda func: func
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_call_later = lambda _hass, _seconds, _callback: lambda: None
    components.bluetooth = bluetooth
    ha.components = components
    ha.helpers = helpers
    helpers.storage = storage
    sys.modules.update({
        "bleak": bleak, "bleak_retry_connector": retry, "homeassistant": ha,
        "homeassistant.components": components, "homeassistant.const": const,
        "homeassistant.components.bluetooth": bluetooth,
        "homeassistant.core": core, "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.storage": storage,
    })
    root = types.ModuleType("custom_components")
    root.__path__ = ["/homeassistant/custom_components"]
    package = types.ModuleType("custom_components.scent_assistant")
    package.__path__ = ["/homeassistant/custom_components/scent_assistant"]
    cloud = types.ModuleType("custom_components.scent_assistant.protocol_cloud")
    cloud.AromaLinkCloudClient = object
    sys.modules["custom_components"] = root
    sys.modules["custom_components.scent_assistant"] = package
    sys.modules["custom_components.scent_assistant.protocol_cloud"] = cloud
    return importlib.import_module("custom_components.scent_assistant.device")


DEVICE = _load_device_module()
PROTOCOL = importlib.import_module("custom_components.scent_assistant.protocol_ble")


class Transport:
    def __init__(self, *, unchanged=False, incomplete_post=False, block_final_ack=False, disconnect_post=False):
        self.unchanged = unchanged
        self.incomplete_post = incomplete_post
        self.block_final_ack = block_final_ack
        self.disconnect_post = disconnect_post
        self.reads = self.writes = 0
        self.frames = []
        self.blocked = asyncio.Event()
        self.current = {
            (1, slot): self.schedule(slot, slot + 3) for slot in range(1, 6)
        }
        self.initial = dict(self.current)

    @staticmethod
    def schedule(slot, intensity):
        raw = bytes([0x4A, 1, 0x02, 0x03, slot, slot, 0x03,
                     8, 0, 20, 0, 0x7F, 0x01, intensity, 0, 10, 0, 120])
        return PROTOCOL.ScentMarketingAkProtocol._parse_v3_schedule(raw)

    async def send(self, frame):
        self.frames.append(frame)
        if frame[:1] == b"\x21":
            self.reads += 1
            if not (self.incomplete_post and self.reads == 2):
                for slot in range(1, 6):
                    self.device._on_ble_notification(1, bytearray(self.current[(1, slot)].raw_frame))
        elif frame == b"\xCA\x01\x05" and self.block_final_ack:
            await self.blocked.wait()
        elif frame[:1] == b"\x2A":
            self.writes += 1
            if self.disconnect_post:
                self.device._ble_connected = False
                self.device._ble_client.is_connected = False
            if not self.unchanged:
                # The device always reports 18-byte physical records, even
                # when a fixed-mode write omitted the custom-duration trailer.
                physical = frame[1:] if len(frame) == 18 else frame[1:] + self.current[(1, frame[5])].raw_frame[14:]
                self.current[(1, frame[5])] = PROTOCOL.ScentMarketingAkProtocol._parse_v3_schedule(
                    b"\x4A" + physical
                )
        return True


class AKV3SlotTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        self.device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.device._protocol._v3_mode = True
        self.device._state = PROTOCOL.DiffuserState()
        self.device._ak_v3_transaction_lock = asyncio.Lock()
        self.device._ak_v3_action_owner = None
        self.device._ak_v3_action_id = 0
        self.device._ak_v3_read_transaction_id = 0
        self.device._ak_v3_startup_generation = 1
        self.device._ak_v3_login = DEVICE.AKV3LoginState(1, accepted=True)
        self.device._ble_connected = True
        self.device._ble_notify_subscribed = True
        self.device._ble_client = SimpleNamespace(is_connected=True)
        self.device._ak_v3_modern_read = None
        self.device._ak_v3_modern_diagnostic_trace = None
        self.device._ak_v3_restored_identities = set()
        self.device._ak_v3_confirmation_metadata = {}
        self.device._ak_v3_slot_lifecycle = {}
        self.device._ak_v3_store = None
        self.device._recent_notifications = []
        self.device._recent_commands = []
        self.device._notify_state_changed = lambda: None

        async def connect(**kwargs):
            return True

        self.device._ble_connect = connect

    async def _run_flow(self, *, rollback_on_failure=True, **options):
        self.transport = Transport(**options)
        self.transport.device = self.device
        self.device._ble_send = self.transport.send
        return await self.device.async_update_ak_v3_slot(
            1, 1, intensity=12, rollback_on_failure=rollback_on_failure
        )

    async def test_baseline_then_2a_then_updated_table_returns_success(self):
        result = await self._run_flow()
        self.assertTrue(result["success"])
        self.assertEqual("success", result["status"])
        self.assertEqual(2, self.transport.reads)
        self.assertEqual(1, self.transport.writes)
        self.assertEqual(1, len([frame for frame in self.transport.frames if frame[:1] == b"\x2A"]))

    async def test_unchanged_post_write_table_is_reported(self):
        result = await self._run_flow(unchanged=True)
        self.assertFalse(result["success"])
        self.assertEqual("unchanged", result["status"])
        self.assertEqual(2, self.transport.writes)

    async def test_failed_verification_restores_immutable_baseline_and_reads_full_table(self):
        result = await self._run_flow(unchanged=True)
        self.assertFalse(result["success"])
        self.assertEqual(3, self.transport.reads)
        writes = [frame for frame in self.transport.frames if frame[:1] == b"\x2A"]
        self.assertEqual(2, len(writes))
        self.assertEqual(b"\x2A" + self.transport.initial[(1, 1)].raw_frame[1:14], writes[-1])

    async def test_incomplete_post_write_table_is_inconclusive(self):
        result = await self._run_flow(incomplete_post=True)
        self.assertFalse(result["success"])
        self.assertEqual("inconclusive", result["status"])
        self.assertEqual(1, self.transport.writes)

    async def test_disabled_rollback_sends_one_write_for_success_mismatch_and_timeout(self):
        success = await self._run_flow(rollback_on_failure=False)
        self.assertTrue(success["success"])
        self.assertFalse(success["rollback_on_failure"])
        self.assertEqual(1, self.transport.writes)

        mismatch = await self._run_flow(rollback_on_failure=False, unchanged=True)
        self.assertFalse(mismatch["success"])
        self.assertEqual("unchanged", mismatch["status"])
        self.assertEqual(1, self.transport.writes)
        self.assertIsNone(self.device._ak_v3_action_owner)
        self.assertIsNone(self.device._ak_v3_modern_read)

        timeout = await self._run_flow(rollback_on_failure=False, incomplete_post=True)
        self.assertFalse(timeout["success"])
        self.assertEqual("inconclusive", timeout["status"])
        self.assertEqual(1, self.transport.writes)
        self.assertIsNone(self.device._ak_v3_action_owner)
        self.assertIsNone(self.device._ak_v3_modern_read)

    async def test_disabled_rollback_sends_one_write_when_connection_drops_after_write(self):
        result = await self._run_flow(rollback_on_failure=False, incomplete_post=True, disconnect_post=True)
        self.assertFalse(result["success"])
        self.assertEqual("inconclusive", result["status"])
        self.assertEqual(1, self.transport.writes)
        self.assertIsNone(self.device._ak_v3_action_owner)
        self.assertIsNone(self.device._ak_v3_modern_read)

    async def test_disabled_rollback_inconclusive_restoration_never_rewrites_temporary_baseline(self):
        self.transport = Transport(unchanged=True)
        temporary = self.device._protocol._parse_v3_schedule(bytes.fromhex(
            "4a010203010103060110007f000a000f012c"
        ))
        self.transport.current[(1, 1)] = temporary
        self.transport.initial[(1, 1)] = temporary
        self.transport.device = self.device
        self.device._ble_send = self.transport.send

        result = await self.device.async_update_ak_v3_slot(
            1, 1, start_minute=0, rollback_on_failure=False
        )

        writes = [frame for frame in self.transport.frames if frame[:1] == b"\x2A"]
        self.assertFalse(result["success"])
        self.assertEqual("unchanged", result["status"])
        self.assertEqual(1, len(writes))
        self.assertEqual(0, writes[0][8])

    async def test_endpoint_two_is_rejected_without_ble_activity(self):
        self.transport = Transport()
        self.transport.device = self.device
        self.device._ble_send = self.transport.send
        result = await self.device.async_update_ak_v3_slot(2, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertEqual([], self.transport.frames)

    async def test_public_create_and_delete_are_unsupported_without_ble_activity(self):
        self.transport = Transport()
        self.transport.device = self.device
        self.device._ble_send = self.transport.send
        create = await self.device.async_create_ak_v3_slot(
            enabled=True, start_hour=8, start_minute=0, end_hour=20, end_minute=0,
            days_mask=0x7F, mode=0, intensity=8, work_seconds=10, pause_seconds=120,
        )
        delete = await self.device.async_delete_ak_v3_slot(1, 1)
        self.assertFalse(create["success"])
        self.assertFalse(delete["success"])
        self.assertEqual([], self.transport.frames)

    def test_supplied_zero_minute_builds_the_exact_restoration_frame(self):
        schedule = self.device._protocol._parse_v3_schedule(bytes.fromhex(
            "4a0102030001070501051f7f000a000f012c"
        ))
        self.assertEqual(
            bytes.fromhex("2a0102030001070500051f7f000a"),
            self.device._protocol.build_v3_schedule_update(schedule, start_minute=0),
        )

    def test_supplied_one_minute_builds_the_exact_live_frame(self):
        schedule = self.device._protocol._parse_v3_schedule(bytes.fromhex(
            "4a0102030001070500051f7f000a000f012c"
        ))
        self.assertEqual(
            bytes.fromhex("2a0102030001070501051f7f000a"),
            self.device._protocol.build_v3_schedule_update(schedule, start_minute=1),
        )

    def test_zero_hour_and_minute_values_replace_the_baseline(self):
        schedule = self.device._protocol._parse_v3_schedule(bytes.fromhex(
            "4a0102030001070501051f7f000a000f012c"
        ))
        frame = self.device._protocol.build_v3_schedule_update(
            schedule, start_hour=0, start_minute=0, end_hour=0, end_minute=0,
            mode=0, days_mask=0,
        )
        self.assertEqual((0, 0, 0, 0, 0, 0), tuple(frame[7:13]))

    def test_absent_fields_preserve_the_read_back_values(self):
        raw = bytes.fromhex("4a0102030001070501051f7f000a000f012c")
        schedule = self.device._protocol._parse_v3_schedule(raw)
        self.assertEqual(b"\x2A" + raw[1:14], self.device._protocol.build_v3_schedule_update(schedule))

    def test_delete_frame_clears_present_and_enabled_but_preserves_fan(self):
        schedule = self.device._protocol._parse_v3_schedule(bytes.fromhex(
            "4a0102030001070500051f7f000a000f012c"
        ))
        self.assertEqual(bytes.fromhex("2a01020300010400000000000000"), self.device._protocol.build_v3_schedule_delete(schedule))

    async def test_delayed_final_ack_preserves_the_table_and_cannot_leak(self):
        self.transport = Transport(block_final_ack=True)
        self.transport.device = self.device
        self.device._ble_send = self.transport.send
        original_finalize = DEVICE.AK_V3_ACK_FINALIZE_SECONDS
        DEVICE.AK_V3_ACK_FINALIZE_SECONDS = 0.01
        try:
            records = await self.device._async_read_ak_v3_modern_table(1)
        finally:
            DEVICE.AK_V3_ACK_FINALIZE_SECONDS = original_finalize
        self.assertEqual({(1, slot) for slot in range(1, 6)}, set(records))
        self.assertIsNone(self.device._ak_v3_modern_read)
        self.transport.block_final_ack = False
        next_records = await self.device._async_read_ak_v3_modern_table(1)
        self.assertEqual({(1, slot) for slot in range(1, 6)}, set(next_records))

    async def test_missing_authentication_or_subscription_never_sends_2a(self):
        self.transport = Transport()
        self.transport.device = self.device
        self.device._ble_send = self.transport.send
        self.device._ak_v3_login.accepted = False
        result = await self.device.async_update_ak_v3_slot(
            1, 1, intensity=12, rollback_on_failure=False
        )
        self.assertFalse(result["success"])
        self.assertFalse(any(frame[:1] == b"\x2a" for frame in self.transport.frames))
        self.device._ak_v3_login.accepted = True
        self.device._ble_notify_subscribed = True
        self.device._ble_client.is_connected = False
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertFalse(any(frame[:1] == b"\x2a" for frame in self.transport.frames))
        self.device._ble_client.is_connected = True
        self.device._protocol._v3_mode = False
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertFalse(any(frame[:1] == b"\x2a" for frame in self.transport.frames))
        self.device._ble_notify_subscribed = False
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertFalse(any(frame[:1] == b"\x2a" for frame in self.transport.frames))

    async def test_stale_generation_or_disconnect_before_write_never_sends_2a(self):
        self.transport = Transport()
        self.transport.device = self.device

        async def send(frame):
            self.transport.frames.append(frame)
            if frame[:1] == b"\x21":
                for slot in range(1, 6):
                    self.device._on_ble_notification(1, bytearray(self.transport.current[(1, slot)].raw_frame))
                self.device._ak_v3_startup_generation += 1
            return True

        self.device._ble_send = send
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertFalse(any(frame[:1] == b"\x2a" for frame in self.transport.frames))

    async def test_busy_slot_update_rejects_immediately_without_queueing(self):
        owner = self.device._claim_ak_v3_action("manual_refresh")
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertEqual("AK V3 action is busy", result["error"])
        self.assertEqual(owner, self.device._ak_v3_action_owner)

    async def test_startup_owned_session_rejects_slot_update_without_transport_side_effects(self):
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("startup conflict must not use transport")

        self.device._ble_connect = forbidden
        self.device._ble_send = forbidden
        conflicts = {
            "_ak_v3_startup_chain": DEVICE.AKV3StartupChain(1, post_21_sent=True),
            "_ak_v3_startup_barrier": DEVICE.AKV3StartupBarrier(1),
            "_ak_v3_modern_read": DEVICE.AKV3ModernRead(1),
            "_ak_v3_metadata_read": object(),
        }
        for attribute, value in conflicts.items():
            setattr(self.device, attribute, value)
            result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
            self.assertFalse(result["success"], attribute)
            self.assertEqual("AK V3 action is busy", result["error"], attribute)
            setattr(self.device, attribute, None)

    async def test_completed_schedule_records_do_not_override_active_startup_metadata(self):
        self.transport = Transport()
        self.transport.device = self.device
        self.device._ble_send = self.transport.send
        self.device._state.ak_v3_schedules = dict(self.transport.current)
        self.device._ak_v3_metadata_read = object()
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertFalse(result["success"])
        self.assertEqual([], self.transport.frames)
        self.device._ak_v3_metadata_read = None
        result = await self.device.async_update_ak_v3_slot(1, 1, intensity=12)
        self.assertTrue(result["success"])
        self.assertEqual(1, self.transport.writes)

    def test_stale_owner_cleanup_cannot_clear_successor(self):
        first = self.device._claim_ak_v3_action("slot_update")
        self.device._ak_v3_action_owner = ("manual_refresh", 1, 99)
        self.device._release_ak_v3_action(first)
        self.assertEqual(("manual_refresh", 1, 99), self.device._ak_v3_action_owner)

    def test_generation_advance_releases_only_its_current_owner_token(self):
        owner = self.device._claim_ak_v3_action("slot_update")
        self.device._ak_v3_startup_generation = 2
        current = self.device._advance_ak_v3_action_owner(owner)
        self.device._release_ak_v3_action(owner)
        self.assertEqual(current, self.device._ak_v3_action_owner)
        self.device._release_ak_v3_action(self.device._current_ak_v3_action_owner(owner))
        self.assertIsNone(self.device._ak_v3_action_owner)
