"""APK-derived AK V3 weekday oil-days regression tests."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from test_ak_v3_slot_transaction import DEVICE, PROTOCOL


def schedule(slot, start, end, days, *, mode=1, intensity=1, work=60, pause=60):
    raw = bytes([
        0x4A, 1, 0x02, 0x03, slot, slot, 0x03,
        start // 60, start % 60, end // 60, end % 60, days,
        mode, intensity, work >> 8, work & 0xFF, pause >> 8, pause & 0xFF,
    ])
    return PROTOCOL.ScentMarketingAkProtocol._parse_v3_schedule(raw)


class AKV3DaysCalculationTest(unittest.TestCase):
    def setUp(self):
        self.device = DEVICE.ScentDiffuserDevice.__new__(DEVICE.ScentDiffuserDevice)
        self.device._protocol = PROTOCOL.ScentMarketingAkProtocol()
        self.device._protocol._v3_mode = True
        self.device._device_type = DEVICE.DeviceType.SCENT_MARKETING_AK
        self.device._state = PROTOCOL.DiffuserState(
            oil_current_ml=34,
            oil_consumption_mlh=1,
            grade_table=[(60, 60)],
            oil_days_remaining=34,
        )
        self.device._ak_v3_startup_generation = 2
        self.device._ak_v3_current_fields = set()
        self.device._ak_v3_retained_generation = None
        self.device._ak_v3_retained_fields = set()
        self.device._ble_connected = True
        self.device._ble_notify_subscribed = True

    def _set_schedules(self, records):
        self.device._state.ak_v3_schedules = {(1, item.slot_id): item for item in records}
        self.device._state.ak_v3_empty_schedules = {}

    def _daily_one_ml_schedules(self, *, mode=1):
        return [
            schedule(1, 0, 120, 0x7F, mode=mode),
            *[schedule(slot, 0, 0, 0) for slot in range(2, 6)],
        ]

    def _mark_oil_days_inputs_current(self, *, grade_table=False):
        fields = ["oil_current_ml", "oil_consumption_mlh", "schedules"]
        if grade_table:
            fields.append("grade_table")
        self.device._retain_ak_v3_fields(*fields)

    def test_apk_repeat_buckets_are_saturday_through_sunday(self):
        self._set_schedules([
            schedule(1, 0, 60, 0x01),
            schedule(2, 60, 120, 0x02),
            schedule(3, 120, 180, 0x04),
            schedule(4, 180, 240, 0x08),
            schedule(5, 240, 300, 0x10),
        ])
        daily = self.device._ak_v3_apk_weekly_consumption(1)
        self.assertEqual([0.0, 0.0, 0.5, 0.5, 0.5, 0.5, 0.5], daily)

    def test_apk_overlap_mutates_later_matching_segment_in_order(self):
        self._set_schedules([
            schedule(1, 120, 240, 0x7F, work=1, pause=1),
            schedule(2, 60, 300, 0x7F, work=1, pause=1),
            schedule(3, 0, 0, 0),
            schedule(4, 0, 0, 0),
            schedule(5, 0, 0, 0),
        ])
        # The later 01:00-05:00 segment is shortened to 01:00-03:00.
        self.assertEqual(2.0, self.device._ak_v3_apk_weekly_consumption(1)[0])

    def test_apk_midnight_split_keeps_before_and_after_segments_separate(self):
        self._set_schedules([
            schedule(1, 23 * 60, 60, 0x01, work=1, pause=1),
            schedule(2, 0, 0, 0),
            schedule(3, 0, 0, 0),
            schedule(4, 0, 0, 0),
            schedule(5, 0, 0, 0),
        ])
        self.assertEqual(1.0, self.device._ak_v3_apk_weekly_consumption(1)[6])

    def test_apk_truncates_each_segment_duty_before_hour_conversion(self):
        self._set_schedules([
            schedule(1, 0, 1, 0x7F, work=1, pause=2),
            schedule(2, 0, 0, 0),
            schedule(3, 0, 0, 0),
            schedule(4, 0, 0, 0),
            schedule(5, 0, 0, 0),
        ])
        self.assertEqual(20 / 3600, self.device._ak_v3_apk_weekly_consumption(1)[0])

    def test_apk_counts_the_current_bucket_before_subtracting_it(self):
        self._set_schedules([
            schedule(1, 0, 60, 0x20),
            schedule(2, 0, 0, 0),
            schedule(3, 0, 0, 0),
            schedule(4, 0, 0, 0),
            schedule(5, 0, 0, 0),
        ])
        with patch.object(DEVICE, "datetime", wraps=datetime) as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 28, 12, 0)
            self.assertEqual(1, self.device._estimate_oil_days(1, 2))

    def test_incomplete_schedules_or_grade_data_are_unavailable(self):
        self._set_schedules([schedule(1, 0, 60, 0x7F)])
        self.assertIsNone(self.device._estimate_oil_days(100, 1))
        self._set_schedules([schedule(slot, 0, 60, 0x7F, mode=0) for slot in range(1, 6)])
        self.device._state.grade_table = None
        self.assertIsNone(self.device._estimate_oil_days(100, 1))

    def test_equal_days_refresh_promotes_freshness_and_survives_teardown(self):
        self._set_schedules(self._daily_one_ml_schedules())
        self._mark_oil_days_inputs_current()

        self.assertTrue(self.device._recompute_oil_days())
        self.assertEqual(34, self.device.state.oil_days_remaining)
        self.assertIn("oil_days_remaining", self.device._ak_v3_current_fields)

        self.device._ble_connected = False
        self.device._ble_notify_subscribed = False
        self.assertTrue(self.device.ak_v3_read_available(
            "oil_current_ml", "oil_consumption_mlh", "schedules", "oil_days_remaining"
        ))

    def test_changed_days_remain_a_supported_freshness_update(self):
        self._set_schedules(self._daily_one_ml_schedules())
        self._mark_oil_days_inputs_current()
        self.device._state.oil_days_remaining = 17

        self.assertTrue(self.device._recompute_oil_days())
        self.assertEqual(34, self.device.state.oil_days_remaining)
        self.assertIn("oil_days_remaining", self.device._ak_v3_current_fields)

    def test_equal_days_with_missing_current_flow_is_not_promoted(self):
        self._set_schedules(self._daily_one_ml_schedules())
        self.device._retain_ak_v3_fields("oil_current_ml", "schedules")

        self.assertFalse(self.device._recompute_oil_days())
        self.assertNotIn("oil_days_remaining", self.device._ak_v3_current_fields)

    def test_partial_schedules_cannot_promote_days_freshness(self):
        self._set_schedules(self._daily_one_ml_schedules()[:4])
        self.device._retain_ak_v3_fields("oil_current_ml", "oil_consumption_mlh")

        self.assertTrue(self.device._recompute_oil_days())
        self.assertIsNone(self.device.state.oil_days_remaining)
        self.assertNotIn("oil_days_remaining", self.device._ak_v3_current_fields)

    def test_level_mode_requires_a_current_grade_table(self):
        self._set_schedules(self._daily_one_ml_schedules(mode=0))
        self._mark_oil_days_inputs_current()

        self.assertFalse(self.device._recompute_oil_days())
        self.assertNotIn("oil_days_remaining", self.device._ak_v3_current_fields)

    def test_custom_mode_does_not_require_a_grade_table(self):
        self._set_schedules(self._daily_one_ml_schedules(mode=1))
        self.device._state.grade_table = None
        self._mark_oil_days_inputs_current()

        self.assertTrue(self.device._recompute_oil_days())
        self.assertIn("oil_days_remaining", self.device._ak_v3_current_fields)

    def test_stale_generation_values_cannot_promote_days_freshness(self):
        self._set_schedules(self._daily_one_ml_schedules())
        self.device._ak_v3_retained_generation = 1
        self.device._ak_v3_retained_fields = {
            "oil_current_ml", "oil_consumption_mlh", "schedules", "oil_days_remaining"
        }

        self.assertFalse(self.device._recompute_oil_days())
        self.assertNotIn("oil_days_remaining", self.device._ak_v3_current_fields)
