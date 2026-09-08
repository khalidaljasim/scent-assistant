"""AK V2 parser isolation contracts retained after the V3 collector split."""
from __future__ import annotations

import unittest

from test_ak_v3_slot_transaction import PROTOCOL


class AKV2ParsingIsolationTest(unittest.TestCase):
    """V2 replies remain independent from each other and from V3 state."""

    def setUp(self):
        self.protocol = PROTOCOL.ScentMarketingAkProtocol()

    def test_v2_83_enabled_schedule_sets_only_schedule_and_power_fields(self):
        result = self.protocol.parse_notification(bytes([0x83, 0x11, 8, 0, 20, 0, 0x7F, 9]))
        self.assertEqual(8, result["start_hour"])
        self.assertEqual(9, result["intensity"])
        self.assertTrue(result["schedule_enabled"])
        self.assertTrue(result["power"])
        self.assertNotIn("firmware_version", result)
        self.assertNotIn("ak_v3_schedule", result)

    def test_v2_83_disabled_schedule_does_not_create_power_or_schedule_state(self):
        result = self.protocol.parse_notification(bytes([0x83, 0x01, 8, 0, 20, 0, 0x7F, 9]))
        self.assertEqual({}, result)

    def test_v2_83_short_frame_is_ignored(self):
        self.assertEqual({}, self.protocol.parse_notification(b"\x83\x11\x08"))

    def test_v2_89_model_reply_does_not_parse_as_schedule_or_firmware(self):
        result = self.protocol.parse_notification(b"\x89\x05")
        self.assertNotIn("schedule_index", result)
        self.assertNotIn("firmware_version", result)
        self.assertNotIn("ak_v3_schedule", result)

    def test_v2_86_firmware_reply_does_not_create_v3_metadata(self):
        result = self.protocol.parse_notification(b"\x86V2.00\x00")
        self.assertEqual("V2.00", result["firmware_version"])
        self.assertNotIn("ak_v3_schedule", result)
        self.assertNotIn("device_name", result)

    def test_v2_86_empty_firmware_reply_is_ignored(self):
        self.assertEqual({}, self.protocol.parse_notification(b"\x86\x00"))

    def test_v2_frames_cannot_flip_the_v3_mode_flag(self):
        self.protocol._v3_mode = True
        self.protocol.parse_notification(bytes([0x83, 0x11, 8, 0, 20, 0, 0x7F, 9]))
        self.protocol.parse_notification(b"\x89\x05")
        self.protocol.parse_notification(b"\x86V2.00")
        self.assertTrue(self.protocol.is_v3)
