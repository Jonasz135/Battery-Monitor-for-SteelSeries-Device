"""Regression tests for history persistence and runtime calculations.

All history files are isolated under the project test directory.
"""

import json
import math
import os
import tempfile
import unittest
from unittest import mock

import battery_timeline as history
import device_catalog
import runtime_estimator as runtime


LINK_24 = device_catalog.LINK_24
LINK_BT = device_catalog.LINK_BT
HOUR = 3600


class TimelineRegressionTests(unittest.TestCase):
    def setUp(self):
        parent = os.path.join(os.path.dirname(__file__), ".test-tmp")
        os.makedirs(parent, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "timeline.json")
        self.legacy = os.path.join(self.directory.name, "legacy.json")

    def timeline(self):
        return history.Timeline(self.path, self.legacy)

    def write(self, data, path=None):
        with open(path or self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def online(self, timeline, start, end, level, link=LINK_24):
        state = None
        for now in range(start + 60, end + 1, 60):
            state = timeline.record("mouse", level, False, now=now, link=link)
        return state

    def charged(self):
        timeline = self.timeline()
        timeline.record("mouse", 100, True, now=0)
        timeline.record("mouse", 100, False, now=0)
        return timeline

    def test_top_level_array_does_not_crash_startup(self):
        self.write([1, 2, 3])
        self.assertEqual(self.timeline().record("mouse", 70, False, now=0).level, 70)

    def test_utf8_bom_history_keeps_existing_samples(self):
        with open(self.path, "w", encoding="utf-8-sig") as handle:
            json.dump({"version": history.FORMAT_VERSION, "devices": {"mouse": {
                "samples": [[0, 90, 0], [3600, 80, 0]], "cycles": [],
                "active": {LINK_24: 3600}, "measured_drop": {LINK_24: 10},
            }}}, handle)
        self.assertEqual(self.timeline().state("mouse").level, 80)

    def test_partial_corruption_keeps_valid_samples_and_cycles(self):
        self.write({"version": history.FORMAT_VERSION, "devices": {"mouse": {
            "samples": [[0, 90, 0], [None, 85, 0], [3600, 80, 0],
                        [math.inf, 50, 0], [7200, 500, 0], [7200, 70, "false"]],
            "cycles": [[0, 3600, 100, 80, 3600], [None, 2, 90, 70],
                       [0, 3600, 100, 80, math.inf]],
            "active": {LINK_24: 3600, LINK_BT: math.inf},
            "charge_end": math.nan, "observed_start": "false",
        }}})
        state = self.timeline().state("mouse")
        self.assertEqual([sample.level for sample in state.samples], [90, 80])
        self.assertEqual(len(state.cycles), 1)
        self.assertEqual(state.active_seconds, 3600)
        self.assertIsNone(state.charge_end)
        self.assertFalse(state.observed_start)

    def test_null_collections_and_invalid_utf8_do_not_crash(self):
        self.write({"version": history.FORMAT_VERSION, "devices": {
            "mouse": {"samples": None, "cycles": None},
        }})
        self.assertEqual(self.timeline().record("mouse", 70, False, now=0).level, 70)
        with open(self.path, "wb") as handle:
            handle.write(b"\xff\xfe")
        self.assertEqual(self.timeline().devices(), [])

    def test_legacy_invalid_entries_do_not_hide_valid_history(self):
        self.write({"mouse": [[0, 90], ["broken", 80], [3600, 80]]}, self.legacy)
        state = self.timeline().state("mouse")
        self.assertEqual(state.level, 80)
        self.assertAlmostEqual(runtime.estimate(state).rate, 10)

    def test_sensor_jitter_does_not_count_as_discharge(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 90, False, now=HOUR)
        timeline.record("mouse", 91, False, now=HOUR + 60)
        state = timeline.record("mouse", 90, False, now=HOUR + 120)
        self.assertEqual(state.link_drop, 10)

    def test_sensor_jitter_at_session_start_does_not_create_discharge(self):
        timeline = self.timeline()
        timeline.record("mouse", 80, False, now=0)
        for now in range(60, HOUR + 1, 60):
            state = timeline.record("mouse", 80 + (now // 60) % 2, False, now=now)
        self.assertEqual(state.link_drop, 0)
        self.assertIsNone(runtime.estimate(state).hours)

    def test_glitch_after_flat_hours_is_still_rejected(self):
        timeline = self.charged()
        self.online(timeline, 0, 5 * HOUR, 100)
        state = timeline.record("mouse", 10, False, now=5 * HOUR + 60)
        self.assertEqual(state.level, 100)

    def test_invalid_sensor_values_do_not_replace_last_level(self):
        timeline = self.timeline()
        timeline.record("mouse", 80, False, now=0)
        for level in [None, "unknown", math.nan, math.inf, -1, 101, True]:
            with self.subTest(level=level):
                self.assertEqual(timeline.record("mouse", level, False, now=60).level, 80)

    def test_alias_merge_keeps_new_charging_state(self):
        self.write({"version": 3, "devices": {
            "Aerox 5 Wireless": {"samples": [[0, 80, 0]], "cycles": [],
                                  "observed_start": True, "charge_end": 0,
                                  "active_seconds": 3600, "last_seen": 3600},
            "Aerox 5 WL (Bluetooth)": {"samples": [[7200, 81, 1]], "cycles": [],
                                        "observed_start": False, "charge_end": None,
                                        "active_seconds": 0, "last_seen": 7200},
        }})
        self.assertTrue(self.timeline().state("aerox_5_wireless").charging)

    def test_alias_merge_keeps_latest_battery_level_in_continuing_session(self):
        self.write({"version": 3, "devices": {
            "Aerox 5 Wireless": {"samples": [[0, 90, 0]], "cycles": [],
                                  "observed_start": True, "charge_end": 0,
                                  "active_seconds": 3600, "last_seen": 3600},
            "Aerox 5 WL (Bluetooth)": {"samples": [[7200, 80, 0]], "cycles": [],
                                        "observed_start": False, "charge_end": None,
                                        "active_seconds": 3600, "last_seen": 7200},
        }})
        state = self.timeline().state("aerox_5_wireless")
        self.assertEqual(state.level, 80)
        self.assertTrue(state.observed_start)

    def test_link_measurement_offset_is_not_a_drop(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 90, False, now=HOUR)
        timeline.record("mouse", 80, False, now=HOUR + 60, link=LINK_BT)
        state = timeline.state("mouse", link=LINK_24)
        self.assertEqual(state.link_drop, 10)
        self.assertEqual(state.active_by_link[LINK_24], HOUR + 60)

    def test_drop_during_offline_gap_does_not_accelerate_online_rate(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 95, False, now=HOUR)
        state = timeline.record("mouse", 50, False, now=24 * HOUR)
        self.assertEqual(state.level, 50)
        self.assertEqual(state.link_drop, 5)
        self.assertAlmostEqual(runtime.estimate(state).rate, 5)

    def test_mixed_cycle_preserves_separate_rates(self):
        timeline = self.charged()
        self.online(timeline, 0, 2 * HOUR, 100)
        timeline.record("mouse", 80, False, now=2 * HOUR)
        timeline.record("mouse", 80, False, now=2 * HOUR, link=LINK_BT)
        self.online(timeline, 2 * HOUR, 4 * HOUR, 80, link=LINK_BT)
        timeline.record("mouse", 70, False, now=4 * HOUR, link=LINK_BT)
        state = timeline.record("mouse", 71, True, now=4 * HOUR + 60, link=LINK_BT)
        rates = {cycle.link: cycle.rate for cycle in state.cycles}
        self.assertAlmostEqual(rates[LINK_24], 10)
        self.assertAlmostEqual(rates[LINK_BT], 5)

    def test_partial_measured_session_survives_charging_and_restart(self):
        timeline = self.timeline()
        timeline.record("mouse", 50, False, now=0)
        self.online(timeline, 0, HOUR, 50)
        state = timeline.record("mouse", 40, False, now=HOUR)
        self.assertIsNone(state.hours_since_charge)
        self.assertIsNone(state.drop_since_charge)
        timeline.record("mouse", 41, True, now=HOUR + 60)
        restarted = self.timeline()
        state = restarted.record("mouse", 100, False, now=2 * HOUR)
        result = runtime.estimate(state)
        self.assertEqual(result.source, runtime.SOURCE_HISTORY)
        self.assertAlmostEqual(result.rate, 10)
        self.assertAlmostEqual(result.hours, 10)

    def test_partial_offline_loss_does_not_become_history(self):
        timeline = self.timeline()
        timeline.record("mouse", 80, False, now=0)
        self.online(timeline, 0, HOUR, 80)
        timeline.record("mouse", 75, False, now=HOUR)
        timeline.record("mouse", 20, False, now=24 * HOUR)
        state = timeline.record("mouse", 21, True, now=24 * HOUR + 60)
        self.assertEqual(state.cycles, ())

    def test_cycle_end_uses_last_successful_poll_when_level_stays_flat(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 80, False, now=HOUR)
        self.online(timeline, HOUR, 2 * HOUR, 80)
        state = timeline.record("mouse", 81, True, now=2 * HOUR + 60)
        self.assertEqual(state.cycles[0].end, 2 * HOUR)
        self.assertAlmostEqual(state.cycles[0].hours, 2)

    def test_level_save_resets_heartbeat_write_deadline(self):
        timeline = self.charged()
        with mock.patch.object(timeline, "_save", wraps=timeline._save) as save:
            self.online(timeline, 0, 240, 100)
            timeline.record("mouse", 99, False, now=240)
            timeline.record("mouse", 99, False, now=300)
        self.assertEqual(save.call_count, 1)

    def test_failed_write_is_retried_without_another_level_change(self):
        timeline = self.timeline()
        with mock.patch.object(history.os, "replace", side_effect=OSError("busy")):
            timeline.record("mouse", 70, False, now=0)
        timeline.record("mouse", 70, False, now=60)
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(self.timeline().state("mouse").level, 70)

    def test_flush_preserves_last_heartbeats_across_restart(self):
        timeline = self.charged()
        self.online(timeline, 0, 240, 100)
        self.assertEqual(self.timeline().state("mouse").active_seconds, 0)
        self.assertTrue(timeline.flush())
        self.assertEqual(self.timeline().state("mouse").active_seconds, 240)
        with mock.patch.object(timeline, "_save", wraps=timeline._save) as save:
            self.assertTrue(timeline.flush())
        save.assert_not_called()

    def test_failed_flush_can_be_retried(self):
        timeline = self.charged()
        self.online(timeline, 0, 240, 100)
        with mock.patch.object(history.os, "replace", side_effect=OSError("busy")):
            self.assertFalse(timeline.flush())
        self.assertTrue(timeline.flush())
        self.assertEqual(self.timeline().state("mouse").active_seconds, 240)

    def test_signed_jitter_balance_survives_restart(self):
        timeline = self.timeline()
        timeline.record("mouse", 80, False, now=0)
        timeline.record("mouse", 81, False, now=60)
        state = self.timeline().record("mouse", 80, False, now=120)
        self.assertEqual(state.link_drop, 0)

    def test_out_of_order_reading_does_not_double_count_active_time(self):
        timeline = self.charged()
        timeline.record("mouse", 100, False, now=60)
        timeline.record("mouse", 99, False, now=30)
        state = timeline.record("mouse", 100, False, now=120)
        self.assertEqual(state.active_seconds, 120)
        self.assertEqual([sample.timestamp for sample in state.samples], [0])

    def test_measured_drop_survives_restart(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 95, False, now=HOUR)
        timeline.record("mouse", 50, False, now=24 * HOUR)
        state = self.timeline().state("mouse")
        self.assertEqual(state.link_drop, 5)

    def test_explicit_offline_break_excludes_short_gap_and_drop(self):
        timeline = self.charged()
        timeline.record("mouse", 100, False, now=60)
        timeline.mark_offline("mouse")
        state = timeline.record("mouse", 90, False, now=120)
        self.assertEqual(state.active_seconds, 60)
        self.assertEqual(state.link_drop, 0)
        state = timeline.record("mouse", 90, False, now=180)
        self.assertEqual(state.active_seconds, 120)

    def test_offline_break_survives_flush_restart(self):
        timeline = self.charged()
        timeline.record("mouse", 100, False, now=60)
        timeline.mark_offline("mouse")
        self.assertTrue(timeline.flush())
        state = self.timeline().record("mouse", 90, False, now=120)
        self.assertEqual(state.active_seconds, 60)
        self.assertEqual(state.link_drop, 0)

    def test_multiple_offline_breaks_between_percentage_changes(self):
        timeline = self.charged()
        timeline.record("mouse", 100, False, now=60)
        timeline.mark_offline("mouse")
        timeline.record("mouse", 100, False, now=120)
        timeline.record("mouse", 100, False, now=180)
        timeline.mark_offline("mouse")
        timeline.record("mouse", 100, False, now=240)
        state = timeline.record("mouse", 95, False, now=300)
        self.assertEqual(state.active_seconds, 180)
        self.assertEqual(state.link_drop, 5)

    def test_unplug_after_unobserved_day_does_not_claim_observed_start(self):
        timeline = self.timeline()
        timeline.record("mouse", 100, True, now=0)
        state = timeline.record("mouse", 80, False, now=24 * HOUR)
        self.assertFalse(state.observed_start)
        self.assertIsNone(state.charge_end)
        self.assertIsNone(state.hours_since_charge)

    def test_flat_charging_heartbeats_preserve_observed_unplug(self):
        timeline = self.timeline()
        timeline.record("mouse", 100, True, now=0)
        for now in range(60, HOUR + 1, 60):
            timeline.record("mouse", 100, True, now=now)
        self.assertTrue(timeline.flush())
        state = self.timeline().record("mouse", 100, False, now=HOUR + 60)
        self.assertTrue(state.observed_start)
        self.assertEqual(state.charge_end, HOUR + 60)

    def test_backward_utc_adjustment_keeps_polling_and_active_time(self):
        timeline = self.timeline()
        with mock.patch.object(history.time, "time", side_effect=[10000, 10060, 6460]), \
             mock.patch.object(history.time, "monotonic", side_effect=[100, 160, 220]):
            timeline.record("mouse", 80, False)
            timeline.record("mouse", 80, False)
            state = timeline.record("mouse", 75, False)
        self.assertEqual(state.level, 75)
        self.assertEqual(state.active_seconds, 120)
        self.assertEqual(state.link_drop, 5)
        self.assertEqual(self.timeline().state("mouse").level, 75)

    def test_forward_utc_adjustment_does_not_look_like_offline(self):
        timeline = self.timeline()
        with mock.patch.object(history.time, "time", side_effect=[10000, 10060, 13720]), \
             mock.patch.object(history.time, "monotonic", side_effect=[100, 160, 220]):
            timeline.record("mouse", 80, False)
            timeline.record("mouse", 80, False)
            state = timeline.record("mouse", 75, False)
        self.assertEqual(state.active_seconds, 120)
        self.assertEqual(state.link_drop, 5)

    def test_duplicate_poll_cannot_confirm_sensor_glitch(self):
        timeline = self.charged()
        timeline.record("mouse", 10, False, now=60)
        duplicate = timeline.record("mouse", 10, False, now=60)
        self.assertEqual(duplicate.level, 100)
        confirmed = timeline.record("mouse", 10, False, now=120)
        self.assertEqual(confirmed.level, 10)

    def test_v4_migration_reconstructs_counters_before_sample_limit(self):
        samples = [[0, 100, 0, LINK_24], [3600, 80, 0, LINK_24]]
        samples += [[7200 + index * 60, 80, 0, LINK_BT] for index in range(500)]
        self.write({"version": 4, "devices": {"mouse": {
            "samples": samples, "cycles": [], "active": {LINK_24: 3600, LINK_BT: 29940},
        }}})
        state = self.timeline().state("mouse", link=LINK_24)
        self.assertEqual(len(state.samples), history.MAX_SAMPLES)
        self.assertEqual(state.samples[0].level, 100)
        self.assertEqual(state.link_drop, 20)

    def test_gradual_unreported_charge_starts_new_session(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 80, False, now=HOUR)
        for index in range(1, 5):
            state = timeline.record("mouse", 80 + index, False, now=HOUR + index * 60)
        self.assertFalse(state.observed_start)
        self.assertIsNone(state.hours_since_charge)
        self.assertEqual(state.link_drop, 0)
        self.assertEqual(len(state.cycles), 1)
        self.assertEqual(state.cycles[0].drop, 20)
        self.assertEqual(state.cycles[0].active_seconds, HOUR)

    def test_gradual_charge_baseline_survives_restart(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 80, False, now=HOUR)
        timeline.record("mouse", 81, False, now=HOUR + 60)
        timeline.record("mouse", 82, False, now=HOUR + 120)
        state = self.timeline().record("mouse", 84, False, now=HOUR + 180)
        self.assertFalse(state.observed_start)
        self.assertEqual(state.cycles[0].drop, 20)
        self.assertEqual(state.cycles[0].active_seconds, HOUR)

    def test_reported_charging_does_not_treat_old_jitter_as_charge_time(self):
        timeline = self.charged()
        self.online(timeline, 0, HOUR, 100)
        timeline.record("mouse", 80, False, now=HOUR)
        timeline.record("mouse", 81, False, now=HOUR + 60)
        self.online(timeline, HOUR + 60, 2 * HOUR, 81)
        state = timeline.record("mouse", 81, True, now=2 * HOUR + 60)
        self.assertEqual(state.cycles[0].active_seconds, 2 * HOUR)
        self.assertEqual(state.cycles[0].drop, 19)

    def test_corrupted_charge_baseline_cannot_invent_extra_active_hours(self):
        self.write({"version": history.FORMAT_VERSION, "devices": {"mouse": {
            "samples": [[0, 100, 0, LINK_24], [3600, 80, 0, LINK_24], [3660, 81, 0, LINK_24]],
            "cycles": [], "active": {LINK_24: 3660}, "measured_drop": {LINK_24: 19},
            "last_seen": 3660,
            "rise": {"link": LINK_24, "level": 80, "last_seen": 3600,
                     "active": {LINK_24: 999999}, "measured_drop": {LINK_24: 20}},
        }}})
        state = self.timeline().record("mouse", 84, False, now=3720)
        self.assertLessEqual(state.cycles[0].active_seconds, 3660)

    def test_offline_break_does_not_backdate_charge_to_old_sensor_jitter(self):
        for explicit_offline in (False, True):
            with self.subTest(explicit_offline=explicit_offline):
                timeline = self.charged()
                self.online(timeline, 0, HOUR, 100)
                timeline.record("mouse", 80, False, now=HOUR)
                timeline.record("mouse", 81, False, now=HOUR + 60)
                self.online(timeline, HOUR + 60, 2 * HOUR, 81)
                if explicit_offline:
                    timeline.mark_offline("mouse")
                state = timeline.record("mouse", 100, False, now=24 * HOUR)
                self.assertEqual(state.cycles[-1].active_seconds, 2 * HOUR)
                self.assertEqual(state.cycles[-1].drop, 19)


class EstimatorRegressionTests(unittest.TestCase):
    def test_non_finite_numbers_never_produce_runtime(self):
        for invalid in [math.nan, math.inf, -math.inf]:
            with self.subTest(invalid=invalid):
                self.assertIsNone(runtime.rate_from(5, invalid))
                self.assertIsNone(runtime.rate_from(invalid, 1))
                self.assertIsNone(runtime.hours_per_point(invalid))
                self.assertEqual(runtime.format_hours(invalid), "")
                self.assertEqual(runtime.format_elapsed(invalid), "")
                self.assertEqual(runtime.format_per_point(invalid), "")

    def test_zero_limit_does_not_use_all_cycles(self):
        cycle = history.Cycle(0, HOUR, 100, 90, HOUR)
        self.assertIsNone(runtime.history_rate([cycle], limit=0))

    def test_invalid_cycle_does_not_dilute_history(self):
        good = history.Cycle(0, HOUR, 100, 90, HOUR)
        invalid = history.Cycle(0, HOUR, 20, 40, HOUR)
        self.assertAlmostEqual(runtime.history_rate([good, invalid]), 10)

    def test_rated_runtime_is_validated_and_capped(self):
        state = history.DeviceState((history.Sample(0, 50, False),), (), None,
                                    False, 0, {})
        for rated in [-100, math.nan, math.inf]:
            self.assertIsNone(runtime.estimate(state, rated).hours)
        self.assertEqual(runtime.estimate(state, 10000).hours, runtime.MAX_ESTIMATE_HOURS)


if __name__ == "__main__":
    unittest.main()
