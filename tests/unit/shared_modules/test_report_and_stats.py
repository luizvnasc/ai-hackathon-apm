"""shared/report.py and shared/stats.py."""
import unittest

from tests.unit._load import load_shared

report = load_shared("report")
stats = load_shared("stats")


class TableTest(unittest.TestCase):
    def test_markdown_table(self):
        self.assertEqual(report.md_table(["A", "B"], [[1, 2]]), "| A | B |\n|---|---|\n| 1 | 2 |")


class SlopeTest(unittest.TestCase):
    def test_needs_two_points(self):
        self.assertIsNone(stats.slope([]))
        self.assertIsNone(stats.slope([4]))

    def test_rising_falling_and_flat(self):
        self.assertAlmostEqual(stats.slope([8, 10, 12]), 2.0)
        self.assertAlmostEqual(stats.slope([1.0, 0.8, 0.6, 0.4]), -0.2)
        self.assertAlmostEqual(stats.slope([5, 5, 5, 5]), 0.0)

    def test_noisy_series(self):
        # x = 0..3, y = 1, 3, 2, 6: sum((x-1.5)(y-3)) = 7 and sum((x-1.5)^2) = 5
        self.assertAlmostEqual(stats.slope([1, 3, 2, 6]), 1.4)


if __name__ == "__main__":
    unittest.main()
