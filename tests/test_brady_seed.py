import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import brady_seed


def calendar_for(window: brady_seed.DateWindow, default: int = 0) -> dict[date, int]:
    return {item_date: default for item_date in brady_seed.window_dates(window)}


class BradySeedTests(unittest.TestCase):
    def test_bitmap_matches_previous_brady_snapshot(self):
        bitmap = brady_seed.load_pattern()
        expected = (
            "0000000000011110011110001110011110010001000000000000",
            "0000000000010001010001010001010001010001000000000000",
            "0000000000010001010001010001010001001010000000000000",
            "0000000000011110011110011111010001000100000000000000",
            "0000000000010001010100010001010001000100000000000000",
            "0000000000010001010010010001010001000100000000000000",
            "0000000000011110010001010001011110000100000000000000",
        )

        self.assertEqual(tuple("".join(map(str, row)) for row in bitmap), expected)
        self.assertEqual(sum(sum(row) for row in bitmap), 84)
        self.assertEqual(len(bitmap), 7)
        self.assertTrue(all(len(row) == 52 for row in bitmap))

    def test_window_excludes_current_partial_week(self):
        window = brady_seed.window_for_utc(date(2024, 3, 10))
        self.assertEqual(window.start, date(2023, 3, 12))
        self.assertEqual(window.end, date(2024, 3, 9))
        self.assertEqual(len(brady_seed.window_dates(window)), 52 * 7)

    def test_utc_cutoff_is_dst_safe(self):
        local_late = datetime(2024, 3, 10, 23, 30, tzinfo=ZoneInfo("America/Chicago"))
        utc_next_day = datetime(2024, 3, 11, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(
            brady_seed.window_for_utc(local_late),
            brady_seed.window_for_utc(utc_next_day),
        )

    def test_bootstrap_uses_dynamic_target_above_existing_peak(self):
        window = brady_seed.window_for_utc(date(2024, 3, 10))
        calendar = calendar_for(window)
        high_date = next(iter(brady_seed.pattern_dates(window, brady_seed.load_pattern())))
        calendar[high_date] = 50

        plan = brady_seed.build_seed_plan(calendar, as_of=date(2024, 3, 10))

        self.assertEqual(plan.mode, "bootstrap")
        self.assertEqual(plan.maximum_real_count, 50)
        self.assertEqual(plan.target_count, 100)
        self.assertEqual(plan.pixel_commits, 84 * 100 - 50)
        self.assertEqual(plan.baseline_commits, 364 - 84)

    def test_existing_pixel_history_switches_to_rollover_mode(self):
        window = brady_seed.window_for_utc(date(2024, 3, 10))
        bitmap = brady_seed.load_pattern()
        active_dates = brady_seed.pattern_dates(window, bitmap)
        calendar = calendar_for(window, default=1)

        plan = brady_seed.build_seed_plan(
            calendar,
            as_of=date(2024, 3, 10),
            ledger_counts={next(iter(active_dates)): 100},
            pixel_dates=active_dates,
        )

        self.assertEqual(plan.mode, "rollover")
        self.assertIsNone(plan.target_count)
        self.assertEqual(plan.pixel_commits, 0)
        self.assertEqual(plan.baseline_commits, 0)

    def test_ledger_counts_are_not_added_again_when_calendar_api_lags(self):
        window = brady_seed.window_for_utc(date(2024, 3, 10))
        calendar = calendar_for(window, default=0)
        plan = brady_seed.build_seed_plan(
            calendar,
            as_of=date(2024, 3, 10),
            ledger_counts={item_date: 1 for item_date in calendar},
            pixel_dates=(),
        )

        self.assertEqual(plan.baseline_commits, 0)
        self.assertEqual(plan.pixel_commits, 84 * 99)


if __name__ == "__main__":
    unittest.main()
