"""shared/dates.py and shared/labels.py."""
import datetime as dt
import unittest

from tests.unit._load import load_shared

dates = load_shared("dates")
labels = load_shared("labels")
UTC = dt.timezone.utc


class ParseTest(unittest.TestCase):
    def test_empty_is_none(self):
        for value in (None, ""):
            self.assertIsNone(dates.parse_dt(value))
            self.assertIsNone(dates.to_date(value))

    def test_date_alone_is_midnight_utc(self):
        self.assertEqual(dates.parse_dt("2026-03-01"), dt.datetime(2026, 3, 1, tzinfo=UTC))

    def test_z_is_utc(self):
        moment = dates.parse_dt("2026-03-01T10:00:00Z")
        self.assertEqual(moment, dt.datetime(2026, 3, 1, 10, tzinfo=UTC))
        self.assertEqual(moment.utcoffset(), dt.timedelta(0))

    def test_time_without_offset_is_utc(self):
        self.assertEqual(dates.parse_dt("2026-03-01T10:00:00"), dt.datetime(2026, 3, 1, 10, tzinfo=UTC))

    def test_offset_is_kept(self):
        moment = dates.parse_dt("2026-03-01T23:30:00-03:00")
        self.assertEqual(moment, dt.datetime(2026, 3, 2, 2, 30, tzinfo=UTC))
        self.assertEqual(moment.utcoffset(), dt.timedelta(hours=-3))

    def test_day_is_the_one_in_the_written_offset(self):
        self.assertEqual(dates.to_date("2026-03-01T23:30:00-03:00"), dt.date(2026, 3, 1))


class LabelTest(unittest.TestCase):
    def test_value_is_after_the_last_separator_in_lower_case(self):
        self.assertEqual(labels.label_value("a::b:: In Progress "), "in progress")

    def test_label_without_scope_is_its_own_value(self):
        self.assertEqual(labels.label_value("Blocked"), "blocked")
        self.assertEqual(labels.label_value(None), "")

    def test_scope_ignores_case_and_spaces(self):
        names = ["Area::Backend", "area :: Front End ", "type::bug", "area"]
        self.assertEqual(labels.scoped(names, "area"), ["backend", "front end"])

    def test_scope_ends_at_the_last_separator(self):
        names = ["x::area::ops", "area::web"]
        self.assertEqual(labels.scoped(names, "x::area"), ["ops"])
        self.assertEqual(labels.scoped(names, "area"), ["web"])

    def test_number(self):
        for value, expected in (("3", 3.0), (" 0.5 ", 0.5), (0, 0.0), (2.5, 2.5)):
            self.assertEqual(labels.number(value), expected)

    def test_what_is_not_a_number(self):
        for value in (None, "", "XL", "-1", "2h", "1.", False):
            self.assertIsNone(labels.number(value), value)

    def test_points_take_the_first_numeric_value(self):
        self.assertEqual(labels.label_points(["points::many", "points::2.5", "points::3"]), 2.5)

    def test_points_prefer_points_then_sp_then_estimate(self):
        self.assertEqual(labels.label_points(["estimate::1", "sp::2", "points::3"]), 3.0)
        self.assertEqual(labels.label_points(["estimate::1", "sp::2", "points::XL"]), 2.0)
        self.assertEqual(labels.label_points(["estimate::1"]), 1.0)

    def test_no_points(self):
        self.assertIsNone(labels.label_points([]))
        self.assertIsNone(labels.label_points(["points::XL", "sp::-1", "5"]))

    def test_cancelled_words(self):
        for word in ("canceled", "cancelled", "wontfix", "won't-fix", "duplicate"):
            self.assertTrue(labels.has_cancelled_label([f"workflow::{word}"]), word)
            self.assertTrue(labels.has_cancelled_label([f"Status::{word.upper()}"]), word)

    def test_cancelled_needs_a_workflow_or_status_scope(self):
        self.assertFalse(labels.has_cancelled_label(["duplicate", "type::duplicate", "workflow::done"]))

    def test_blocked_and_stale_words(self):
        words = {"blocked", "on-hold", "on hold", "onhold", "impeded", "bloqueado", "bloqueada"}
        self.assertEqual(labels.BLOCKED_VALUES, words)
        self.assertEqual(labels.STALE_VALUES, {"stale"})


if __name__ == "__main__":
    unittest.main()
