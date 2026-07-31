import sys
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import brady_snake


class BradyGridTests(unittest.TestCase):
    def test_window_excludes_current_partial_week(self):
        window = brady_snake.window_for_utc(date(2024, 3, 10))
        self.assertEqual(window.start, date(2023, 3, 12))
        self.assertEqual(window.end, date(2024, 3, 9))
        self.assertEqual((window.end - window.start).days + 1, 52 * 7)

    def test_sunday_first_leap_year_mapping(self):
        model = brady_snake.build_grid(date(2024, 3, 10))
        leap_day = [
            cell
            for row in model.cells
            for cell in row
            if cell.cell_date == date(2024, 2, 29)
        ]
        self.assertEqual(len(leap_day), 1)
        self.assertEqual((leap_day[0].column, leap_day[0].row), (50, 4))
        self.assertEqual(model.cells[0][0].cell_date.weekday(), 6)
        self.assertEqual(model.cells[6][51].cell_date.weekday(), 5)

    def test_utc_cutoff_is_dst_safe(self):
        local_late = datetime(2024, 3, 10, 23, 30, tzinfo=ZoneInfo("America/Chicago"))
        utc_next_day = datetime(2024, 3, 11, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(
            brady_snake.window_for_utc(local_late),
            brady_snake.window_for_utc(utc_next_day),
        )

    def test_fixed_date_validation(self):
        with self.assertRaises(ValueError):
            brady_snake.DateWindow(date(2024, 1, 8), date(2024, 12, 28))
        with self.assertRaises(ValueError):
            brady_snake.DateWindow(date(2024, 1, 7), date(2024, 12, 27))
        with self.assertRaises(ValueError):
            brady_snake.build_grid(datetime(2024, 1, 7))

    def test_future_cells_are_empty(self):
        window = brady_snake.DateWindow(date(2024, 1, 7), date(2025, 1, 4))
        model = brady_snake.build_grid(date(2024, 1, 8), window=window)
        self.assertTrue(any(cell.future for row in model.cells for cell in row))
        self.assertTrue(all(not cell.active for row in model.cells for cell in row if cell.future))

    def test_brady_is_centered_and_borders_are_blank(self):
        pattern = brady_snake.load_pattern()
        bitmap = brady_snake.brady_bitmap(pattern)
        expected_bitmap = (
            "0000000000011110011110001110011110010001000000000000",
            "0000000000010001010001010001010001010001000000000000",
            "0000000000010001010001010001010001001010000000000000",
            "0000000000011110011110011111010001000100000000000000",
            "0000000000010001010100010001010001000100000000000000",
            "0000000000010001010010010001010001000100000000000000",
            "0000000000011110010001010001011110000100000000000000",
        )
        self.assertEqual(pattern.word_width, 29)
        self.assertEqual((52 - pattern.word_width) // 2, 11)
        self.assertEqual(sum(sum(row) for row in bitmap), 84)
        self.assertEqual(tuple("".join(map(str, row)) for row in bitmap), expected_bitmap)
        model = brady_snake.build_grid(date(2024, 3, 10))
        rendered = model.rendered_values
        self.assertEqual(len(rendered), 7)
        self.assertTrue(all(len(row) == 54 for row in rendered))
        self.assertTrue(all(row[0] == 0 and row[-1] == 0 for row in rendered))
        self.assertEqual(model.active_cell_count, 84)

    def test_artifacts_are_validated_as_a_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "dist"
            model = brady_snake.generate(output_dir, date(2024, 3, 10))
            self.assertEqual(model.pattern.version, "brady-v1")
            brady_snake.validate_output_dir(output_dir)
            self.assertGreater((output_dir / "github-contribution-grid-snake.gif").stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
