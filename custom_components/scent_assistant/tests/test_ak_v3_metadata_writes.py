"""Golden-frame tests for confirmed Ultra Max Tower AK V3 metadata writes."""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


class AKV3MetadataWritesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.protocol._v3_mode = True

    def test_fragrance_name_records_are_fixed_width_and_ordered(self):
        self.assertEqual(
            b"\x28Mountain Mist" + b"\x00" * 3,
            self.protocol.build_v3_oil_names(["Mountain Mist"]),
        )
        frame = self.protocol.build_v3_oil_names(["A", "B"])
        self.assertEqual(33, len(frame))
        self.assertEqual(b"A" + b"\x00" * 15, frame[1:17])
        self.assertEqual(b"B" + b"\x00" * 15, frame[17:33])

    def test_utf8_boundaries_reject_overflow_without_splitting_characters(self):
        self.assertEqual(17, len(self.protocol.build_v3_oil_names(["é" * 8])))
        with self.assertRaises(ValueError):
            self.protocol.build_v3_oil_names(["é" * 9])
        with self.assertRaises(ValueError):
            self.protocol.build_v3_device_name("é" * 9)

    def test_amount_frame_preserves_status_and_encodes_big_endian_values(self):
        self.assertEqual(
            bytes.fromhex("2b7e03e802c1"),
            self.protocol.build_v3_oil_amounts(0x7E, [(1000, 705)]),
        )
        with self.assertRaises(ValueError):
            self.protocol.build_v3_oil_amounts(0x7E, [(1000, 1001)])

    def test_calculation_frame_encodes_flow_and_days(self):
        self.assertEqual(
            bytes.fromhex("3000020d002f0000"),
            self.protocol.build_v3_oil_calculations([(5.25, 47)]),
        )
        with self.assertRaises(ValueError):
            self.protocol.build_v3_oil_calculations([(655.36, 47)])
        with self.assertRaises(ValueError):
            self.protocol.build_v3_oil_calculations([(5.251, 47)])

    def test_oil_status_byte_is_read_from_the_4b_response(self):
        updates = self.protocol.parse_notification(bytes.fromhex("4b7e03e802c1"))
        self.assertEqual(0x7E, updates["oil_status_byte"])
        self.assertEqual(1000, updates["oil_max_ml"])
        self.assertEqual(705, updates["oil_current_ml"])

    def test_device_metadata_and_password_frames(self):
        self.assertEqual(b"\x22PREUltra Max Tower", self.protocol.build_v3_device_name("Ultra Max Tower", b"PRE"))
        self.assertEqual(b"\x23MiCasa", self.protocol.build_v3_device_label("MiCasa"))
        self.assertEqual(b"\x0F1234OK01", self.protocol.build_v3_password_change("1234"))
        with self.assertRaises(ValueError):
            self.protocol.build_v3_password_change("123")

    def test_readable_prefixed_name_is_unlimited_but_editable_suffix_is_validated(self):
        updates = self.protocol.parse_notification(b"\x42SA_Ultra Max Tower")
        self.assertEqual("SA_Ultra Max Tower", updates["device_name"])
        self.assertEqual(b"SA_", updates["device_name_append_prefix"])
        self.assertNotIn("device_label", updates)
        self.assertNotIn("oil_names", updates)
        self.assertEqual(
            b"\x22SA_Ultra Max Tower",
            self.protocol.build_v3_device_name("Ultra Max Tower", b"SA_"),
        )
        with self.assertRaises(ValueError):
            self.protocol.build_v3_device_name("A" * 17, b"SA_")

    def test_label_and_fragrance_responses_cannot_cross(self):
        label = self.protocol.parse_notification(b"\x43MiCasa")
        fragrance = self.protocol.parse_notification(
            b"\x48Mountain Mist" + b"\x00" * 3 + b"Cedar" + b"\x00" * 11
        )
        self.assertEqual({"device_label": "MiCasa"}, label)
        self.assertEqual(["Mountain Mist", "Cedar"], fragrance["oil_names"])
        self.assertNotIn("oil_names", label)
        self.assertNotIn("device_label", fragrance)

    def test_empty_or_unavailable_fragrance_is_not_substituted_from_label(self):
        label = self.protocol.parse_notification(b"\x43MiCasa")
        self.assertNotIn("oil_names", label)

    def test_text_entities_do_not_apply_write_limits_to_readable_state(self):
        source = Path("/homeassistant/custom_components/scent_assistant/text.py").read_text()
        self.assertNotIn("_attr_native_max", source)

    def test_password_change_encoder_is_distinct_from_the_fixed_ak_login(self):
        self.assertEqual(bytes.fromhex("8f38383838"), self.protocol.build_login_primary())
        self.assertNotEqual(
            self.protocol.build_login_primary(),
            self.protocol.build_v3_password_change("1234"),
        )

    def test_login_password_and_v3_fallback_use_the_stored_four_char_value(self):
        protocol = PROTOCOL.ScentMarketingAkProtocol("2468")
        self.assertEqual(b"\x8F2468", protocol.build_login_primary())
        self.assertEqual(b"\x8F2468OK01", protocol.build_login_secondary_v3())

    def test_capabilities_gate_lamp_and_decode_full_oil_records(self):
        updates = self.protocol.parse_notification(b"\x8FOK_V3.0" + b"\x00" * 5 + b"\x9f\xd0\x00\x00\x03")
        self.assertTrue(updates["ak_v3_has_oil"])
        self.assertTrue(updates["ak_v3_has_battery"])
        self.assertTrue(updates["ak_v3_has_custom_mode"])
        self.assertTrue(updates["ak_v3_has_aromas"])
        self.assertTrue(updates["ak_v3_has_fan"])
        self.assertTrue(updates["ak_v3_has_round_battery"])
        self.assertTrue(updates["ak_v3_has_lamp"])
        self.assertTrue(updates["ak_v3_has_global_control"])
        self.assertTrue(updates["ak_v3_reply_chaining"])
        self.assertEqual(3, updates["ak_v3_lamp_type"])
        self.assertEqual(True, self.protocol.parse_notification(b"\x51\x00\x00\x01")["light_on"])
        self.assertEqual(0x7E, self.protocol.parse_notification(bytes.fromhex("4b7e03e802c1"))["battery"])
        oil = self.protocol.parse_notification(bytes.fromhex("5001020d002f02bc00012c000000"))
        self.assertEqual(5.25, oil["oil_consumption_mlh"])
        self.assertEqual(700, oil["oil_old_calibration_ml"])
        self.assertEqual([(True, 5.25, 47, 700)], oil["oil_calculation_records"])
        self.assertNotIn("oil_days_remaining", oil)

    def test_custom_schedule_retains_nonzero_grade_and_durations(self):
        self.protocol._v3_capabilities_13 = 0x04
        schedule = self.protocol._parse_v3_schedule(
            bytes.fromhex("4a010203010107080014007f010700180128")
        )
        self.assertEqual(
            bytes.fromhex("2a010203010107080014007f010700180128"),
            self.protocol.build_v3_schedule_update(schedule),
        )

    def test_52_identity_is_diagnostic_only(self):
        self.assertEqual(
            {"ak_v3_protocol_identity": "AA:BB:CC:DD:EE:FF"},
            self.protocol.parse_notification(b"\x52AA:BB:CC:DD:EE:FF\x00"),
        )

    def test_aggregate_fan_and_diffusion_frames_preserve_each_other(self):
        self.assertEqual(bytes.fromhex("2a01020300"), self.protocol.build_v3_aggregate_control(fan=True, diffusion=True))
        self.assertEqual(bytes.fromhex("2a01020100"), self.protocol.build_fan(False))
        self.assertEqual(bytes.fromhex("2a01020000"), self.protocol.build_v3_diffusion(False))

    def test_fixed_schedule_update_omits_custom_duration_trailer(self):
        schedule = self.protocol._parse_v3_schedule(bytes.fromhex("4a0102030001070500051f7f000a000f012c"))
        self.assertEqual(
            bytes.fromhex("2a0102030001070500051f7f000a"),
            self.protocol.build_v3_schedule_update(schedule),
        )
        self.protocol._v3_capabilities_13 = 0x04
        self.assertEqual(18, len(self.protocol.build_v3_schedule_update(schedule, mode=1)))

    def test_password_change_has_no_staged_runtime_service(self):
        root = Path("/homeassistant/custom_components/scent_assistant")
        self.assertNotIn("SERVICE_AK_V3_CHANGE_PASSWORD", (root / "__init__.py").read_text())
        self.assertNotIn("change_ak_v3_password:", (root / "services.yaml").read_text())

    async def test_oil_transaction_orders_frames_and_refreshes_supported_state(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._protocol = self.protocol
        device._state = PROTOCOL.DiffuserState(
            oil_status_byte=0x7E,
            start_hour=8,
            start_minute=0,
            end_hour=20,
            end_minute=0,
            schedule_custom_mode=True,
            work_seconds=15,
            pause_seconds=300,
        )
        sent, sleeps = [], []

        async def connect(**_kwargs):
            return True

        async def send(frame):
            sent.append(frame)
            return True

        async def sleep(seconds):
            sleeps.append(seconds)

        device._ble_connect = connect
        device._ble_send = send
        device._schedule_disconnect = lambda: None
        original_sleep = DEVICE.asyncio.sleep
        DEVICE.asyncio.sleep = sleep
        try:
            self.assertTrue(await device._async_write_ak_v3_oil(1000, 705, 5.25, 47))
        finally:
            DEVICE.asyncio.sleep = original_sleep
        self.assertEqual(bytes.fromhex("2b7e03e802c1"), sent[0])
        self.assertEqual(bytes.fromhex("3000020d002f0000"), sent[1])
        self.assertEqual([0.2, 0.15, 0.15], sleeps)
        self.assertEqual([b"\xc8", b"\xce"], sent[2:])

    async def test_calibration_commits_only_after_matching_fresh_readback(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._protocol = self.protocol
        device._state = PROTOCOL.DiffuserState(
            oil_status_byte=1, oil_max_ml=1000, oil_current_ml=705,
            oil_consumption_mlh=5.25, oil_old_calibration_ml=800,
            start_hour=8, end_hour=20, schedule_custom_mode=True,
            work_seconds=15, pause_seconds=300,
        )
        device._state.ak_v3_schedules = {
            (1, slot): self.protocol._parse_v3_schedule(bytes([
                0x4A, 1, 0x02, 0x03, slot, slot, 0x03,
                8, 0, 20, 0, 0x7F, 0x01, 1, 0, 15, 1, 44,
            ]))
            for slot in range(1, 6)
        }
        sent, reads, sleeps = [], [], []

        async def connect(**_kwargs):
            return True

        async def read_baseline():
            reads.append(True)
            if len(reads) == 1:
                return (1000, 705, 5.25, 47, 800)
            record = self.protocol.parse_notification(b"\x50" + sent[1][1:])["oil_calculation_records"][0]
            return (1000, 650, record[1], record[2], 650)

        async def send(frame):
            sent.append(frame)
            return True

        async def sleep(seconds):
            sleeps.append(seconds)

        device._ble_connect = connect
        device._async_read_ak_v3_oil_baseline = read_baseline
        device._ble_send = send
        device._schedule_disconnect = lambda: None
        original_sleep = DEVICE.asyncio.sleep
        DEVICE.asyncio.sleep = sleep
        try:
            self.assertTrue(await device.async_calibrate_ak_v3_oil(650))
        finally:
            DEVICE.asyncio.sleep = original_sleep
        self.assertEqual(2, len(reads))
        self.assertEqual([0.2], sleeps)
        self.assertEqual(bytes.fromhex("2b0103e8028a"), sent[0])
        self.assertEqual(0x30, sent[1][0])
        self.assertEqual(650, device._state.oil_old_calibration_ml)

    def test_empty_disabled_and_enabled_schedule_states_remain_distinct(self):
        empty = self.protocol._parse_v3_schedule(bytes([0x4A, 1, 2, 3, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
        disabled = self.protocol._parse_v3_schedule(bytes([0x4A, 1, 2, 3, 0, 1, 1, 8, 0, 20, 0, 0x7F, 0, 8, 0, 15, 1, 44]))
        enabled = self.protocol._parse_v3_schedule(bytes([0x4A, 1, 2, 3, 1, 1, 3, 8, 0, 20, 0, 0x7F, 0, 8, 0, 15, 1, 44]))
        self.assertTrue(empty.is_empty)
        self.assertFalse(disabled.enabled)
        self.assertTrue(disabled.present)
        self.assertTrue(enabled.enabled)

    def test_identity_is_not_derived_from_writable_device_name(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._ble_address = "00:11:22:33:44:55"
        device._cloud_device_id = None
        device._state = PROTOCOL.DiffuserState(device_name="Ultra Max Tower")
        before = device.unique_id
        device._state.device_name = "Renamed"
        self.assertEqual(before, device.unique_id)

    async def test_password_frames_are_redacted_from_diagnostics_and_command_history(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._protocol = self.protocol
        device._recent_commands = []
        device._ak_v3_modern_diagnostic_trace = None

        class Client:
            is_connected = True

            async def write_gatt_char(self, *_args, **_kwargs):
                return None

        device._ble_client = Client()
        await device._ble_send(self.protocol.build_v3_password_change("1234"))
        self.assertEqual(["0f0000000000000000"], device._recent_commands)

    async def test_existing_gw_password_frames_are_also_redacted(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._protocol = PROTOCOL.ScentMarketingGwProtocol()
        device._recent_commands = []
        device._ak_v3_modern_diagnostic_trace = None

        class Client:
            is_connected = True

            async def write_gatt_char(self, *_args, **_kwargs):
                return None

        device._ble_client = Client()
        await device._ble_send(device._protocol.build_password("1234"))
        self.assertNotIn("31323334", device._recent_commands[0])

    async def test_login_frames_retain_only_direction_opcode_and_length(self):
        device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        device._protocol = self.protocol
        device._recent_commands = []
        device._ak_v3_modern_diagnostic_trace = None

        class Client:
            is_connected = True

            async def write_gatt_char(self, *_args, **_kwargs):
                return None

        device._ble_client = Client()
        await device._ble_send(self.protocol.build_login_primary())
        self.assertEqual(
            [{"direction": "TX", "opcode": "8F", "length": 5}],
            device.recent_commands,
        )
