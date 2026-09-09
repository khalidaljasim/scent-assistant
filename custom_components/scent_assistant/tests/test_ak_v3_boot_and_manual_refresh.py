"""AK V3 startup and isolated manual-refresh transport tests."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


def _schedule(slot: int) -> bytes:
    return bytes([0x4A, 1, 2, 3, slot, slot, 3, 8, 0, 20, 0, 0x7F, 1, slot, 0, 10, 0, 120])


class AKV3BootAndManualRefreshTest(unittest.IsolatedAsyncioTestCase):
    def _device(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        device._protocol._v3_mode = True
        device._state = PROTOCOL.DiffuserState()
        device._ble_address = "00:11:22:33:44:55"
        device._ble_name = "synthetic"
        device._ble_connected = True
        device._ble_notify_subscribed = True
        device._ble_client = SimpleNamespace(is_connected=True)
        device._ble_lock = asyncio.Lock()
        device._ak_v3_manual_refresh_active = False
        device._ak_v3_action_owner = None
        device._ak_v3_action_id = 0
        device._ak_v3_manual_refresh_generation = None
        device._ak_v3_manual_refresh_result = None
        device._ak_v3_manual_refresh_at = None
        device._ak_v3_manual_refresh_transaction_id = 0
        device._ak_v3_manual_refresh_trace = []
        device._ak_v3_manual_refresh_trace_active = False
        device._ak_v3_startup_chain = None
        device._ak_v3_startup_barrier = None
        device._ak_v3_modern_read = None
        device._ak_v3_modern_diagnostic_trace = None
        device._ak_v3_metadata_read = None
        device._ak_v3_startup_generation = 1
        device._ak_v3_entity_platforms_ready = True
        device._ak_v3_login = DEVICE.AKV3LoginState(1)
        device._ak_v3_login.accepted = True
        device._ak_v3_read_transaction_id = 0
        device._ak_v3_restored_identities = set()
        device._ak_v3_confirmation_metadata = {}
        device._ak_v3_slot_lifecycle = {}
        device._ak_v3_store = None
        device._recent_notifications = []
        device._recent_commands = []
        device._state_callbacks = []
        device._notify_state_changed = lambda: None
        return device

    def _install_fresh_session(self, device, events, sent):
        replies = {
            b"\xC1": b"\x42Tower", b"\xC2": b"\x43Dining", b"\xC3": b"\x44" + b"F" * 16,
            b"\xC4": b"\x45Model", b"\xC5": b"\x46\x01\x00\x05\x00\x10\x00\x06\x00\x20",
            b"\xC6": b"\x47\x00\x0f\x01\x2c", b"\xC7": b"\x48Oil" + b"\x00" * 13,
            b"\xC8": b"\x4b\x00\x03\x52\x02\x8d", b"\xCB": b"\x4c", b"\xCC": b"\x4d\x03",
            b"\xCD": b"\x4e", b"\xCE": b"\x50" + b"\x00" * 7, b"\xD0": b"\x51\x00\x00\x01", b"\xD1": b"\x52",
        }

        async def send(frame):
            sent.append(frame)
            if frame[:1] == b"\x21":
                device._on_ble_notification(1, bytearray(b"\x41"))
                for slot in range(1, 6):
                    device._on_ble_notification(1, bytearray(_schedule(slot)))
            elif frame in replies:
                device._on_ble_notification(1, bytearray(replies[frame]))
            return True

        async def connect(**kwargs):
            events.append(("connect", kwargs))
            device._ble_connected = True
            device._ble_notify_subscribed = True
            device._ble_client = SimpleNamespace(is_connected=True)
            device._ak_v3_login = DEVICE.AKV3LoginState(1)
            device._ak_v3_login.accepted = True
            device._ak_v3_login.response_event.set()
            device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1)
            await device._async_complete_ak_v3_login(1)
            return True

        async def close():
            events.append(("close", None))
            device._ble_connected = False
            device._ble_notify_subscribed = False
            device._ble_client = None
            device._ak_v3_login = None

        device._ble_send = send
        device._ble_connect = connect
        device._async_close_manual_refresh_session = close

    async def test_manual_refresh_closes_existing_session_then_fresh_logins(self):
        device = self._device()
        events, sent = [], []
        self._install_fresh_session(device, events, sent)
        self.assertTrue(await device.async_refresh_ak_v3_state())
        self.assertEqual(["close", "connect", "close"], [event[0] for event in events])
        self.assertEqual(b"\x21", sent[0][:1])
        self.assertFalse(any(frame[:1] in (b"\x83", b"\x89", b"\x86") for frame in sent))
        self.assertEqual([b"\xCA\x01" + bytes([slot]) for slot in range(1, 6)], [frame for frame in sent if frame[:1] == b"\xCA"])

    async def test_manual_refresh_terminal_callback_observes_released_owner(self):
        device = self._device()
        events, sent, terminal_states = [], [], []
        self._install_fresh_session(device, events, sent)
        del device._notify_state_changed

        def capture_terminal_state():
            if device._ak_v3_manual_refresh_at is not None:
                terminal_states.append(
                    (
                        device._ak_v3_action_owner,
                        device.ak_v3_manual_refresh_available,
                        device._ak_v3_manual_refresh_result,
                        device._ak_v3_manual_refresh_at,
                    )
                )

        device.register_state_callback(capture_terminal_state)

        self.assertTrue(await device.async_refresh_ak_v3_state())
        self.assertEqual(
            [(None, True, "success", device._ak_v3_manual_refresh_at)],
            terminal_states,
        )
        trace_events = [item["event"] for item in device._ak_v3_manual_refresh_trace]
        self.assertEqual(1, trace_events.count("cleanup_complete"))
        self.assertEqual(1, trace_events.count("final_notification"))

    async def test_manual_refresh_rejects_busy_or_non_ak_requests_without_connecting(self):
        device = self._device()
        device._ak_v3_manual_refresh_active = True
        self.assertFalse(await device.async_refresh_ak_v3_state())
        device._ak_v3_manual_refresh_active = False
        device._protocol = PROTOCOL.ScentMarketingGwProtocol()
        self.assertFalse(await device.async_refresh_ak_v3_state())

    def test_retained_schedule_and_fan_fields_survive_an_intentional_disconnect(self):
        device = self._device()
        device._device_type = DEVICE.DeviceType.SCENT_MARKETING_AK
        device._ak_v3_current_fields = {"schedules", "fan_aggregate"}
        device._ak_v3_retained_generation = device._ak_v3_startup_generation
        device._ak_v3_retained_fields = {"schedules", "fan_aggregate"}
        device._ble_connected = False
        device._ble_notify_subscribed = False

        self.assertTrue(device.ak_v3_read_available("schedules", "fan_aggregate"))

    async def test_startup_waits_for_metadata_and_direct_collector(self):
        device = self._device()
        chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        barrier = DEVICE.AKV3StartupBarrier(1, chain=chain)
        barrier.armed.set()
        device._ak_v3_startup_chain = chain
        device._ak_v3_startup_barrier = barrier
        device._arm_ak_v3_modern_collector()
        device._ak_v3_entity_platforms_ready = True
        device._ble_send = lambda _frame: asyncio.sleep(0, result=True)
        task = asyncio.create_task(device._async_start_ak_v3_startup_reads())
        for slot in range(1, 6):
            device._on_ble_notification(1, bytearray(_schedule(slot)))
        chain.completed.set()
        await task
        self.assertTrue(barrier.released.is_set())
        self.assertIsNone(device._ak_v3_startup_barrier)

    async def test_passive_setup_releases_barrier_before_slot_update_baseline(self):
        device = self._device()
        events, sent, terminal_barriers = [], [], []
        self._install_fresh_session(device, events, sent)
        device._async_restore_ak_v3_schedules = lambda: asyncio.sleep(0)
        device._notify_state_changed = lambda: terminal_barriers.append(device._ak_v3_startup_barrier)

        await device.async_setup()

        self.assertIsNone(device._ak_v3_startup_barrier)
        self.assertIsNone(terminal_barriers[-1])

        async def stop_before_write(frame):
            sent.append(frame)
            if frame[:1] == b"\x21":
                for slot in range(1, 6):
                    device._on_ble_notification(1, bytearray(_schedule(slot)))
                return True
            if frame[:1] == b"\x2A":
                return False
            return True

        device._ble_send = stop_before_write
        result = await device.async_update_ak_v3_slot(1, 5, start_minute=1)
        self.assertEqual("AK V3 schedule write was not sent", result["error"])
        self.assertIn(b"\x21", [frame[:1] for frame in sent])
        self.assertEqual(1, len([frame for frame in sent if frame[:1] == b"\x2A"]))
        self.assertIsNone(device._ak_v3_action_owner)

    async def test_cancelled_startup_releases_only_its_barrier(self):
        device = self._device()
        chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        barrier = DEVICE.AKV3StartupBarrier(1, chain=chain)
        barrier.armed.set()
        device._ak_v3_startup_chain = chain
        device._ak_v3_startup_barrier = barrier
        device._arm_ak_v3_modern_collector()
        task = asyncio.create_task(device._async_start_ak_v3_startup_reads())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(barrier.released.is_set())
        self.assertTrue(barrier.failed)
        self.assertIsNone(device._ak_v3_startup_barrier)

    async def test_timed_out_startup_releases_only_its_barrier(self):
        device = self._device()
        chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        barrier = DEVICE.AKV3StartupBarrier(1, chain=chain)
        barrier.armed.set()
        device._ak_v3_startup_chain = chain
        device._ak_v3_startup_barrier = barrier
        device._arm_ak_v3_modern_collector()
        device._ble_send = lambda _frame: asyncio.sleep(0, result=True)
        original_timeout = getattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS")
        setattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS", 0)
        try:
            await device._async_start_ak_v3_startup_reads()
        finally:
            setattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS", original_timeout)
        self.assertTrue(barrier.released.is_set())
        self.assertFalse(barrier.failed)
        self.assertIsNone(device._ak_v3_startup_barrier)

    def test_stale_barrier_cleanup_cannot_clear_replacement(self):
        device = self._device()
        stale = DEVICE.AKV3StartupBarrier(1)
        replacement = DEVICE.AKV3StartupBarrier(2)
        device._ak_v3_startup_barrier = replacement

        self.assertFalse(device._release_ak_v3_startup_barrier(stale, failed=True))
        self.assertIs(device._ak_v3_startup_barrier, replacement)
        self.assertFalse(replacement.released.is_set())

    def test_stale_disconnect_callback_cannot_clear_newer_barrier(self):
        device = self._device()
        stale_client = SimpleNamespace(is_connected=False)
        device._ble_client = SimpleNamespace(is_connected=True)
        barrier = DEVICE.AKV3StartupBarrier(1)
        device._ak_v3_startup_barrier = barrier

        device._on_ble_disconnected(stale_client)

        self.assertIs(device._ak_v3_startup_barrier, barrier)
        self.assertFalse(barrier.released.is_set())

    async def test_current_disconnect_releases_its_owned_barrier(self):
        device = self._device()
        client = SimpleNamespace(is_connected=True)
        device._ble_client = client
        device._ble_disconnect_expected = False
        device._ble_reconnect_task = None
        device._ak_v3_current_fields = set()
        device._ak_v3_retained_generation = None
        device._ak_v3_retained_fields = set()
        device._clear_device_derived_state = lambda: None
        device._async_reconnect_after_disconnect = lambda: asyncio.sleep(0)
        barrier = DEVICE.AKV3StartupBarrier(1)
        device._ak_v3_startup_barrier = barrier

        device._on_ble_disconnected(client)
        await asyncio.sleep(0)

        self.assertTrue(barrier.released.is_set())
        self.assertTrue(barrier.failed)
        self.assertIsNone(device._ak_v3_startup_barrier)

    async def test_startup_does_not_begin_while_slot_action_owns_the_session(self):
        device = self._device()
        owner = device._claim_ak_v3_action("slot_update")
        device._ak_v3_startup_chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(
            1, chain=device._ak_v3_startup_chain
        )
        device._ak_v3_startup_barrier.armed.set()
        sent = []

        async def send(frame):
            sent.append(frame)
            return True

        device._ble_send = send
        await device._async_start_ak_v3_startup_reads()
        self.assertEqual([], sent)
        self.assertIs(owner, device._ak_v3_action_owner)

    async def test_async_setup_requests_explicit_passive_startup_intent(self):
        device = self._device()
        calls = []
        started = []

        async def connect(**kwargs):
            calls.append(kwargs)
            device._protocol._v3_mode = True
            return True

        async def start_reads():
            started.append(True)

        device._ble_connect = connect
        device._async_start_ak_v3_startup_reads = start_reads
        device._async_restore_ak_v3_schedules = lambda: asyncio.sleep(0)

        await device.async_setup()

        self.assertEqual(
            [{"read_ak_state": False, "keep_connected": True, "startup_ak_v3_session": True}],
            calls,
        )
        self.assertEqual([True], started)

    async def test_startup_connection_refuses_a_preclaimed_slot_owner_before_transport(self):
        device = self._device()
        owner = device._claim_ak_v3_action("slot_update")
        device._ble_address = "00:11:22:33:44:55"

        self.assertFalse(await device._ble_connect(startup_ak_v3_session=True))
        self.assertIs(owner, device._ak_v3_action_owner)
        self.assertIsNone(device._ak_v3_startup_chain)
        self.assertIsNone(device._ak_v3_modern_read)

    async def test_manual_refresh_connection_failure_has_one_terminal_notification(self):
        device = self._device()
        device._ble_connected = False
        device._ble_client = None

        async def connect(**_kwargs):
            return False

        device._ble_connect = connect
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual("failed:connect", device._ak_v3_manual_refresh_result)
        self.assertEqual(1, sum(item["event"] == "final_notification" for item in device._ak_v3_manual_refresh_trace))

    async def test_manual_refresh_authentication_without_subscribed_session_is_closed(self):
        device = self._device()
        device._ble_connected = False
        device._ble_client = None
        closed = []

        async def connect(**_kwargs):
            device._ble_connected = True
            device._ble_client = SimpleNamespace(is_connected=True)
            return True

        async def close():
            closed.append(True)
            device._ble_connected = False
            device._ble_client = None

        device._ble_connect = connect
        device._async_close_manual_refresh_session = close
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual("failed:schedule_1", device._ak_v3_manual_refresh_result)
        self.assertEqual([True], closed)

    async def test_manual_refresh_refuses_to_interleave_with_an_active_startup_chain(self):
        device = self._device()
        device._ak_v3_startup_chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual("unavailable_or_busy", device._ak_v3_manual_refresh_result)
        self.assertIsNotNone(device._ak_v3_startup_chain)

    async def test_manual_refresh_refuses_to_interleave_with_an_owned_schedule_collector(self):
        device = self._device()
        device._ak_v3_startup_chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1, chain=device._ak_v3_startup_chain)
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual("unavailable_or_busy", device._ak_v3_manual_refresh_result)
        self.assertIsNotNone(device._ak_v3_startup_chain)
        self.assertIsNotNone(device._ak_v3_startup_barrier)

    async def test_manual_refresh_rejection_preserves_partial_metadata_and_schedule_ownership(self):
        device = self._device()
        chain = DEVICE.AKV3StartupChain(1, post_21_sent=True)
        chain.accepted_frames[0x41] = b"\x41"
        transaction = DEVICE.AKV3ModernRead(1)
        device._ak_v3_startup_chain = chain
        device._ak_v3_startup_barrier = DEVICE.AKV3StartupBarrier(1, chain=chain)
        device._ak_v3_modern_read = transaction
        original_timeout = DEVICE.AK_V3_TRANSACTION_READ_SECONDS
        setattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS", 0.01)
        try:
            self.assertFalse(await device.async_refresh_ak_v3_state())
        finally:
            setattr(DEVICE, "AK_V3_TRANSACTION_READ_SECONDS", original_timeout)
        self.assertEqual("unavailable_or_busy", device._ak_v3_manual_refresh_result)
        self.assertEqual(b"\x41", chain.accepted_frames[0x41])
        self.assertIs(device._ak_v3_modern_read, transaction)

    async def test_manual_refresh_cancellation_releases_transaction_owner_and_lock(self):
        device = self._device()
        device._ble_connected = False
        device._ble_client = None
        started = asyncio.Event()

        async def connect(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        device._ble_connect = connect
        task = asyncio.create_task(device.async_refresh_ak_v3_state())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(device._ak_v3_manual_refresh_active)
        self.assertIsNone(device._ak_v3_modern_read)
        self.assertFalse(device._ble_lock.locked())

    async def test_manual_refresh_connection_exception_still_emits_one_terminal_notification(self):
        device = self._device()
        device._ble_connected = False
        device._ble_client = None

        async def connect(**_kwargs):
            raise OSError("synthetic")

        device._ble_connect = connect
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual("failed:connect", device._ak_v3_manual_refresh_result)
        events = [item["event"] for item in device._ak_v3_manual_refresh_trace]
        self.assertEqual(1, events.count("connection_exception"))
        self.assertEqual(1, events.count("final_notification"))
        self.assertTrue(device.ak_v3_manual_refresh_available)

    async def test_cached_ble_device_is_passed_to_retry_connector(self):
        device = self._device()
        device._ble_connected = False
        device._ble_client = None
        device._hass = object()
        device._ble_last_failure_ts = 0
        device._ble_has_synced_time = True
        cached = SimpleNamespace(details={"source": "bluetooth_proxy"})
        captured = []

        def lookup(hass, address, *, connectable):
            self.assertIs(device._hass, hass)
            self.assertEqual(device._ble_address, address)
            self.assertTrue(connectable)
            return cached

        async def connect(_client_class, candidate, *_args, **_kwargs):
            # Match the connector's real requirement rather than accepting a
            # string through a permissive mock.
            self.assertIs(cached, candidate)
            self.assertEqual({"source": "bluetooth_proxy"}, candidate.details)
            captured.append(candidate)
            raise OSError("synthetic")

        original_lookup = getattr(DEVICE.bluetooth, "async_ble_device_from_address", None)
        original_connect = DEVICE.establish_connection
        setattr(DEVICE.bluetooth, "async_ble_device_from_address", lookup)
        setattr(DEVICE, "establish_connection", connect)
        try:
            self.assertFalse(await device._ble_connect(read_ak_state=False))
        finally:
            setattr(DEVICE, "establish_connection", original_connect)
            if original_lookup is None:
                del DEVICE.bluetooth.async_ble_device_from_address
            else:
                setattr(DEVICE.bluetooth, "async_ble_device_from_address", original_lookup)
        self.assertEqual([cached], captured)

    async def test_missing_cached_ble_device_uses_direct_address_fallback(self):
        device = self._device()
        device._ble_connected = False
        device._ble_client = None
        device._hass = object()
        device._ble_last_failure_ts = 0
        device._ble_has_synced_time = True
        direct_targets = []

        class DirectClient:
            def __init__(self, target, **_kwargs):
                direct_targets.append(target)
                self.is_connected = False

            async def connect(self):
                raise OSError("synthetic")

        async def unexpected_connector(*_args, **_kwargs):
            raise AssertionError("address fallback must not use the retry connector")

        original_lookup = getattr(DEVICE.bluetooth, "async_ble_device_from_address", None)
        original_client = DEVICE.BleakClient
        original_connect = DEVICE.establish_connection
        setattr(DEVICE.bluetooth, "async_ble_device_from_address", lambda *_args, **_kwargs: None)
        setattr(DEVICE, "BleakClient", DirectClient)
        setattr(DEVICE, "establish_connection", unexpected_connector)
        try:
            self.assertFalse(await device._ble_connect(read_ak_state=False))
        finally:
            setattr(DEVICE, "BleakClient", original_client)
            setattr(DEVICE, "establish_connection", original_connect)
            if original_lookup is None:
                del DEVICE.bluetooth.async_ble_device_from_address
            else:
                setattr(DEVICE.bluetooth, "async_ble_device_from_address", original_lookup)
        self.assertEqual([device._ble_address], direct_targets)

    async def test_manual_refresh_rejection_does_not_allocate_a_transaction_or_transport(self):
        device = self._device()
        device._ak_v3_manual_refresh_active = True
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual(0, device._ak_v3_manual_refresh_transaction_id)
        self.assertEqual([], device._ak_v3_manual_refresh_trace)

    async def test_manual_refresh_rejects_slot_owner_without_tearing_down_its_session(self):
        device = self._device()
        owner = device._claim_ak_v3_action("slot_update")
        self.assertFalse(await device.async_refresh_ak_v3_state())
        self.assertEqual(owner, device._ak_v3_action_owner)
        self.assertTrue(device._ble_connected)

    async def test_simultaneous_manual_refreshes_allow_one_owner_without_queueing(self):
        device = self._device()
        started = asyncio.Event()

        async def connect(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        device._ble_connected = False
        device._ble_client = None
        device._ble_connect = connect
        first = asyncio.create_task(device.async_refresh_ak_v3_state())
        await started.wait()
        self.assertFalse(await device.async_refresh_ak_v3_state())
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
