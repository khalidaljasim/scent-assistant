"""Response-ownership boundaries for the direct-push AK V3 4A collector."""
from __future__ import annotations

import asyncio
import unittest

from test_ak_v3_modern_read import AKV3ModernReadTest, _frame


class AKV3CollectorBoundariesTest(AKV3ModernReadTest):
    """Each collector owns one ordered, generation-local five-slot stream."""

    async def test_duplicate_slot_is_not_acked_twice_or_advanced(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._on_ble_notification(1, bytearray(_frame(1)))
        self.device._on_ble_notification(1, bytearray(_frame(1)))
        await self._drain()
        transaction = self.device._ak_v3_modern_read
        self.assertEqual(2, transaction.expected_slot)
        self.assertEqual([b"\xCA\x01\x01"], self.sent)

    async def test_malformed_4a_never_acks_or_advances(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._on_ble_notification(1, bytearray(_frame(1)[:-1]))
        await self._drain()
        self.assertEqual(1, self.device._ak_v3_modern_read.expected_slot)
        self.assertEqual([], self.sent)

    async def test_wrong_slot_never_acks_before_the_expected_record(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._on_ble_notification(1, bytearray(_frame(3)))
        await self._drain()
        self.assertEqual([], self.sent)
        self.assertEqual({}, self.device._ak_v3_modern_read.records)

    async def test_wrong_endpoint_never_claims_the_collector_endpoint(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._ak_v3_modern_read.expected_endpoint = 1
        self.device._on_ble_notification(1, bytearray(_frame(1, endpoint=2)))
        await self._drain()
        self.assertIsNone(self.device._ak_v3_modern_read.endpoint)
        self.assertEqual([], self.sent)

    async def test_stale_collector_frames_cannot_feed_a_rearmed_generation(self):
        await self.device._async_start_ak_v3_modern_read()
        stale = self.device._ak_v3_modern_read
        await self.device._async_start_ak_v3_modern_read()
        current = self.device._ak_v3_modern_read
        self.assertIsNot(stale, current)
        self.assertGreater(current.transaction_id, stale.transaction_id)
        self.device._on_ble_notification(1, bytearray(_frame(2)))
        await self._drain()
        self.assertEqual({}, current.records)
        self.assertEqual([], self.sent)

    async def test_out_of_order_stream_can_resume_at_the_expected_slot(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._on_ble_notification(1, bytearray(_frame(2)))
        self.device._on_ble_notification(1, bytearray(_frame(1)))
        self.device._on_ble_notification(1, bytearray(_frame(2)))
        await self._drain()
        self.assertEqual({1, 2}, set(self.device._ak_v3_modern_read.records))
        self.assertEqual([b"\xCA\x01\x01", b"\xCA\x01\x02"], self.sent)

    async def test_capability_ca_ack_has_exact_endpoint_and_slot(self):
        await self.device._async_start_ak_v3_modern_read()
        self.device._on_ble_notification(1, bytearray(_frame(1, endpoint=7)))
        await self._drain()
        self.assertEqual([b"\xCA\x07\x01"], self.sent)

    async def test_stop_cancels_pending_ack_tasks_and_releases_owner(self):
        blocked = asyncio.Event()

        async def send(_frame):
            await blocked.wait()
            return True

        self.device._ble_send = send
        await self.device._async_start_ak_v3_modern_read()
        transaction = self.device._ak_v3_modern_read
        self.device._on_ble_notification(1, bytearray(_frame(1)))
        await self._drain()
        self.assertTrue(transaction.send_tasks)
        self.assertTrue(await self.device._async_stop_ak_v3_modern_read(transaction))
        self.assertIsNone(self.device._ak_v3_modern_read)
        self.assertFalse(transaction.send_tasks)

    async def test_ack_timeout_keeps_complete_records_and_cleanup_is_repeatable(self):
        blocked = asyncio.Event()

        async def send(_frame):
            await blocked.wait()
            return True

        self.device._ble_send = send
        await self.device._async_start_ak_v3_modern_read()
        transaction = self.device._ak_v3_modern_read
        for slot in range(1, 6):
            self.device._on_ble_notification(1, bytearray(_frame(slot)))
        await self._drain()
        self.assertTrue(transaction.records_complete_event.is_set())
        self.assertEqual("complete", transaction.completion_result)
        await self.device._async_stop_ak_v3_modern_read(transaction)
        self.assertFalse(await self.device._async_stop_ak_v3_modern_read(transaction))

    async def test_disconnected_collector_cleanup_does_not_emit_prohibited_v2_frames(self):
        await self.device._async_start_ak_v3_modern_read()
        transaction = self.device._ak_v3_modern_read
        await self.device._async_stop_ak_v3_modern_read(transaction)
        self.assertFalse(any(frame[:1] in (b"\x83", b"\x89", b"\x86") for frame in self.sent))
