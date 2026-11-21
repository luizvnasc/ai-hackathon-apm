"""Pure math of velocity.py: period stats, unit choice, slope, summary, weekly throughput."""
import datetime as dt
import unittest

from tests.unit._load import load

vel = load("team-velocity", "velocity.py")


def issue(period="S1", points=3.0, closed=None, cancelled=False, assignees=()):
    return vel.new_issue(key="X-1", title="t", period=period, points=points, closed=closed,
                         cancelled=cancelled, assignees=list(assignees))


S1 = {"name": "S1", "start": "2026-01-05", "end": "2026-01-18"}  # 14 days = 2 weeks


class DatesTest(unittest.TestCase):
    def test_parse_date_only_is_utc_midnight(self):
        d = vel.parse_dt("2026-01-18")
        self.assertEqual(d, dt.datetime(2026, 1, 18, tzinfo=dt.timezone.utc))

    def test_parse_z_suffix(self):
        self.assertEqual(vel.parse_dt("2026-01-18T10:00:00Z").utcoffset(), dt.timedelta(0))

    def test_naive_datetime_becomes_utc(self):
        self.assertEqual(vel.parse_dt("2026-01-18T10:00:00").tzinfo, dt.timezone.utc)

    def test_empty_is_none(self):
        self.assertIsNone(vel.parse_dt(None))
        self.assertIsNone(vel.to_date(""))

    def test_to_date_keeps_the_timestamp_offset(self):
        # 23:30 at -03:00 is still Jan 18 for the team, although it is Jan 19 in UTC.
        self.assertEqual(vel.to_date("2026-01-18T23:30:00-03:00"), dt.date(2026, 1, 18))

    def test_weeks_between_inclusive_and_at_least_one_day(self):
        self.assertEqual(vel.weeks_between(dt.date(2026, 1, 5), dt.date(2026, 1, 18)), 2.0)
        self.assertEqual(vel.weeks_between(dt.date(2026, 1, 5), dt.date(2026, 1, 5)), 1 / 7)
        self.assertEqual(vel.weeks_between(dt.date(2026, 1, 5), dt.date(2026, 1, 1)), 1 / 7)


class LabelsTest(unittest.TestCase):
    def test_scoped_is_case_insensitive_and_uses_last_separator(self):
        labels = ["Points:: 5", "workflow::Done", "type::bug", "plain", "a::b::c"]
        self.assertEqual(vel.scoped(labels, "points"), ["5"])
        self.assertEqual(vel.scoped(labels, "workflow"), ["done"])
        self.assertEqual(vel.scoped(labels, "a::b"), ["c"])

    def test_num_takes_first_numeric(self):
        self.assertEqual(vel.num(["many", "2.5", "3"]), 2.5)
        self.assertIsNone(vel.num(["xl", "-1"]))
        self.assertIsNone(vel.num([]))


class UnitTest(unittest.TestCase):
    def test_explicit_unit_wins(self):
        self.assertEqual(vel.choose_unit([issue(points=None)], "points"), "points")
        self.assertEqual(vel.choose_unit([issue()], "count"), "count")

    def test_auto_points_when_every_real_issue_has_points(self):
        self.assertEqual(vel.choose_unit([issue(), issue(points=0.0)], "auto"), "points")

    def test_auto_ignores_cancelled_without_points(self):
        self.assertEqual(vel.choose_unit([issue(), issue(points=None, cancelled=True)], "auto"), "points")

    def test_auto_count_when_one_issue_lacks_points(self):
        self.assertEqual(vel.choose_unit([issue(), issue(points=None)], "auto"), "count")

    def test_auto_count_on_empty_input(self):
        self.assertEqual(vel.choose_unit([], "auto"), "count")
        self.assertEqual(vel.choose_unit([issue(cancelled=True)], "auto"), "count")

    def test_size(self):
        self.assertEqual(vel.size(issue(points=5.0), "points"), 5.0)
        self.assertEqual(vel.size(issue(points=None), "points"), 0)
        self.assertEqual(vel.size(issue(points=5.0), "count"), 1)


class SlopeTest(unittest.TestCase):
    def test_needs_two_points(self):
        self.assertIsNone(vel.slope([]))
        self.assertIsNone(vel.slope([4]))

    def test_linear_series(self):
        self.assertAlmostEqual(vel.slope([8, 10, 12]), 2.0)
        self.assertAlmostEqual(vel.slope([12, 10, 8]), -2.0)
        self.assertAlmostEqual(vel.slope([5, 5, 5, 5]), 0.0)

    def test_noisy_series(self):
        # x = 0..3, y = 1, 3, 2, 6: sum((x-1.5)(y-3)) = 3 + 0 - 0.5 + 4.5 = 7; sum((x-1.5)^2) = 5
        self.assertAlmostEqual(vel.slope([1, 3, 2, 6]), 1.4)


class PeriodStatsTest(unittest.TestCase):
    def issues(self):
        return [
            issue(points=3.0, closed="2026-01-10T12:00:00Z", assignees=["a"]),
            issue(points=5.0, closed="2026-01-18T22:00:00Z", assignees=["a", "b"]),  # on the end date
            issue(points=2.0, closed="2026-01-19T00:00:01Z", assignees=["b"]),       # one second late
            issue(points=1.0),                                                        # still open
            issue(points=8.0, closed="2026-01-12T00:00:00Z", cancelled=True),         # ignored
            issue(points=4.0, closed="2026-01-11T00:00:00Z", period="S2"),            # other period
            issue(points=None, closed="2026-01-11T00:00:00Z"),                        # no points, on time
        ]

    def test_points(self):
        st = vel.period_stats(S1, self.issues(), "points", dt.date(2026, 2, 1))
        self.assertEqual(st["issues"], 5)
        self.assertEqual(st["scope"], 11)
        self.assertEqual(st["velocity"], 8)
        self.assertEqual(st["weeks"], 2.0)
        self.assertEqual(st["per_week"], 4.0)
        self.assertEqual(st["late"], 2)
        self.assertEqual(st["spilled"], 1)
        self.assertAlmostEqual(st["completion"], 8 / 11)
        self.assertEqual(st["unpointed"], 1)
        self.assertEqual(st["per_member"], {"a": 5.5, "b": 2.5, "(unassigned)": 0})

    def test_count(self):
        st = vel.period_stats(S1, self.issues(), "count", dt.date(2026, 2, 1))
        self.assertEqual((st["scope"], st["velocity"], st["late"], st["spilled"]), (5, 3, 1, 1))
        self.assertEqual(st["per_member"], {"a": 1.5, "b": 0.5, "(unassigned)": 1})

    def test_empty_period(self):
        st = vel.period_stats(S1, [], "points", dt.date(2026, 2, 1))
        self.assertEqual((st["issues"], st["scope"], st["velocity"]), (0, 0, 0))
        self.assertIsNone(st["completion"])
        self.assertEqual(st["per_member"], {})

    def test_zero_points_scope_has_no_completion(self):
        st = vel.period_stats(S1, [issue(points=0.0, closed="2026-01-10")], "points", dt.date(2026, 2, 1))
        self.assertEqual(st["velocity"], 0)
        self.assertIsNone(st["completion"])

    def test_only_cancelled(self):
        st = vel.period_stats(S1, [issue(cancelled=True, closed="2026-01-10")], "points", dt.date(2026, 2, 1))
        self.assertEqual(st["issues"], 0)


class SummarizeTest(unittest.TestCase):
    def rows(self, velocities, weeks=2.0, completion=1.0):
        return [{"velocity": v, "per_week": v / weeks, "completion": completion} for v in velocities]

    def test_empty(self):
        self.assertIsNone(vel.summarize([]))

    def test_one_period(self):
        s = vel.summarize(self.rows([6]))
        self.assertEqual((s["n"], s["mean"], s["median"], s["stdev"], s["cv"]), (1, 6, 6, 0.0, 0.0))
        self.assertIsNone(s["trend"])
        self.assertEqual(s["last3"], 6)

    def test_three_periods(self):
        rows = self.rows([8, 10, 12])
        rows[0]["completion"] = 8 / 11
        s = vel.summarize(rows)
        self.assertEqual((s["mean"], s["median"], s["min"], s["max"]), (10, 10, 8, 12))
        self.assertAlmostEqual(s["stdev"], (8 / 3) ** 0.5)
        self.assertAlmostEqual(s["cv"], (8 / 3) ** 0.5 / 10)
        self.assertAlmostEqual(s["trend"], 2.0)
        self.assertEqual(s["median_per_week"], 5.0)
        self.assertEqual(s["min_last3_per_week"], 4.0)
        self.assertAlmostEqual(s["completion"], (8 / 11 + 2) / 3)

    def test_last3_uses_the_most_recent(self):
        s = vel.summarize(self.rows([100, 1, 2, 3]))
        self.assertEqual(s["last3"], 2)
        self.assertEqual(s["min_last3_per_week"], 0.5)

    def test_zero_mean_has_no_cv(self):
        s = vel.summarize(self.rows([0, 0]))
        self.assertIsNone(s["cv"])

    def test_completion_skips_none(self):
        rows = self.rows([4, 6])
        rows[0]["completion"] = None
        self.assertEqual(vel.summarize(rows)["completion"], 1.0)
        rows[1]["completion"] = None
        self.assertIsNone(vel.summarize(rows)["completion"])


class WeeklyTest(unittest.TestCase):
    def test_full_weeks_before_the_current_one(self):
        today = dt.date(2026, 2, 18)  # Wednesday; current week starts Mon Feb 16
        issues = [
            issue(points=3.0, closed="2026-02-09T00:00:00Z"),   # Monday of the last full week
            issue(points=2.0, closed="2026-02-15T23:59:00Z"),   # Sunday of the last full week
            issue(points=5.0, closed="2026-02-16T08:00:00Z"),   # current week: left out
            issue(points=4.0, closed="2026-02-03T00:00:00Z"),   # the week before
            issue(points=9.0, closed="2026-02-10T00:00:00Z", cancelled=True),
            issue(points=1.0),                                   # open
            issue(points=7.0, closed="2026-01-01T00:00:00Z"),   # older than the window
        ]
        rows = vel.weekly(issues, "points", today, 2)
        self.assertEqual([r["start"] for r in rows], ["2026-02-02", "2026-02-09"])
        self.assertEqual([r["end"] for r in rows], ["2026-02-08", "2026-02-15"])
        self.assertEqual([r["velocity"] for r in rows], [4.0, 5.0])
        self.assertEqual([r["issues"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["per_week"], rows[0]["velocity"])
        self.assertTrue(all(r["weeks"] == 1.0 and r["scope"] is None for r in rows))

    def test_on_a_monday_the_current_week_is_excluded(self):
        rows = vel.weekly([issue(closed="2026-02-16T08:00:00Z")], "count", dt.date(2026, 2, 16), 1)
        self.assertEqual(rows[0]["start"], "2026-02-09")
        self.assertEqual(rows[0]["velocity"], 0)

    def test_empty_and_zero_weeks(self):
        self.assertEqual(len(vel.weekly([], "count", dt.date(2026, 2, 18), 3)), 3)
        self.assertEqual(vel.weekly([], "count", dt.date(2026, 2, 18), 0), [])


class FormatTest(unittest.TestCase):
    def test_f1(self):
        self.assertEqual(vel.f1(None), "—")
        self.assertEqual(vel.f1(3.02), "3")
        self.assertEqual(vel.f1(2.5), "2.5")
        self.assertEqual(vel.f1(0.727, pct=True), "73%")

    def test_md_table(self):
        self.assertEqual(vel.md_table(["A", "B"], [[1, 2]]), "| A | B |\n|---|---|\n| 1 | 2 |")


if __name__ == "__main__":
    unittest.main()
