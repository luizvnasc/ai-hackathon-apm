"""The rules of scope stability, with hand-computed numbers."""
import datetime as dt
import unittest

from tests.unit.scope_stability.helpers import NEXT, OLD, analyze, issue, period, row, ss


def base(n=10):
    """n untouched issues of 5 points in the initial scope."""
    return [issue(f"B-{i}") for i in range(n)]


def kinds(r):
    return [(c["type"], c["size"]) for c in r["changes"]]


class FormulaTest(unittest.TestCase):
    def test_untouched_is_100(self):
        r = row(base())
        self.assertEqual((r["initial"], r["changed"], r["stability"], r["rating"]), (50, 0, 1.0, "stable"))

    def test_addition(self):
        r = row(base() + [issue("A", start=None, moves=[("03-05", "S")])])
        self.assertEqual((r["added"], r["changed"], r["stability"], r["final"]), (5, 5, 0.9, 55))

    def test_a_swap_is_a_change_even_with_zero_balance(self):
        r = row(base(9) + [issue("R", moves=[("03-05", None)]), issue("A", start=None, moves=[("03-05", "S")])])
        self.assertEqual((r["initial"], r["final"], r["changed"], r["stability"]), (50, 50, 10, 0.8))

    def test_floor_is_zero_and_change_rate_is_not_capped(self):
        r = row([issue("B")] + [issue(f"A{n}", start=None, moves=[("03-05", "S")]) for n in range(3)])
        self.assertEqual((r["change_rate"], r["stability"], r["rating"]), (3.0, 0.0, "unstable"))

    def test_empty_initial_scope_has_no_percentage(self):
        r = row([issue("A", start=None, moves=[("03-05", "S")])])
        self.assertEqual((r["initial"], r["stability"], r["change_rate"], r["rating"]), (0, None, None, None))

    def test_rating_limits_are_inclusive(self):
        for value, expected in ((1, "stable"), (0.9, "stable"), (1 - 0.1, "stable"), (0.899, "moderate"),
                                (0.75, "moderate"), (0.749, "unstable"), (0, "unstable"), (None, None)):
            self.assertEqual(ss.rating(value), expected, value)

    def test_delivery_does_not_matter(self):
        self.assertEqual(row([issue(closed="03-06")])["stability"], 1.0)
        self.assertEqual(row([issue()])["stability"], 1.0)


class EveryMovementTest(unittest.TestCase):
    def test_in_and_out_counts_twice(self):
        r = row(base() + [issue("T", start=None, moves=[("03-05", "S"), ("03-08", None)])])
        self.assertEqual(kinds(r), [("added", 5), ("removed", 5)])
        self.assertEqual((r["changed"], r["final"]), (10, 50))

    def test_initial_out_and_back_counts_twice(self):
        r = row(base(9) + [issue("R", moves=[("03-05", None), ("03-06", "S")])])
        self.assertEqual(kinds(r), [("removed", 5), ("added", 5)])

    def test_undo_on_the_same_day_still_counts(self):
        r = row(base() + [issue("T", start=None, moves=[("03-05T10:00:00Z", "S"), ("03-05T10:00:20Z", None)])])
        self.assertEqual(r["changed"], 10)

    def test_created_inside_the_period_is_added(self):
        self.assertEqual(kinds(row(base() + [issue("N", created="03-04")])), [("added", 5)])

    def test_moves_between_other_periods_are_ignored(self):
        self.assertEqual(row(base() + [issue("X", start="O", moves=[("03-05", "N")])])["changed"], 0)


class WindowTest(unittest.TestCase):
    def test_changes_on_the_start_day_are_planning(self):
        r = row(base() + [issue("A", start=None, moves=[("03-02T23:59:00Z", "S")]), issue("R", moves=[("03-02", None)])])
        self.assertEqual((r["initial"], r["changed"]), (55, 0))

    def test_grace_days_move_the_cutoff(self):
        items = base() + [issue("A", start=None, moves=[("03-04", "S")])]
        self.assertEqual(row(items)["changed"], 5)
        r = row(items, grace_days=2)
        self.assertEqual((r["initial"], r["changed"]), (55, 0))

    def test_last_day_counts_and_the_day_after_does_not(self):
        self.assertEqual(row(base() + [issue("A", start=None, moves=[("03-13", "S")])])["changed"], 5)
        self.assertEqual(row(base() + [issue("A", start=None, moves=[("03-14", "S")])])["changed"], 0)

    def test_days_follow_the_timezone(self):
        late = [("03-03T01:30:00Z", "S")]  # 22:30 of the start day in UTC-3
        items = base() + [issue("A", start=None, moves=late)]
        self.assertEqual(row(items)["changed"], 5)
        self.assertEqual(row(items, tz=dt.timezone(dt.timedelta(hours=-3)))["changed"], 0)

    def test_current_period_stops_today_and_is_partial(self):
        items = base() + [issue("A", start=None, moves=[("03-05", "S")]), issue("F", start=None, moves=[("03-10", "S")])]
        res = analyze(items, today=dt.date(2026, 3, 6))
        r = next(r for r in res["rows"] if r["id"] == "S")
        self.assertEqual((r["status"], r["changed"]), ("current", 5))
        self.assertIsNone(res["summary"])
        self.assertIn("still running", " ".join(res["notes"]))


class RolloverTest(unittest.TestCase):
    def test_open_issue_to_a_later_period_on_the_last_day_does_not_count(self):
        r = row(base(9) + [issue("R", moves=[("03-13", "N")])])
        self.assertEqual((r["changed"], r["stability"], r["final"]), (0, 1.0, 50))
        self.assertEqual([(c["key"], c["size"]) for c in r["rollover"]], [("R", 5)])

    def test_after_the_end_is_listed_as_rollover(self):
        r = row(base(9) + [issue("R", moves=[("03-15", "N")])])
        self.assertEqual((r["changed"], len(r["rollover"])), (0, 1))

    def test_to_the_backlog_on_the_last_day_counts(self):
        self.assertEqual(kinds(row([issue(moves=[("03-13", None)])])), [("removed", 5)])

    def test_to_an_earlier_period_on_the_last_day_counts(self):
        self.assertEqual(kinds(row([issue(moves=[("03-13", "O")])])), [("removed", 5)])

    def test_to_an_undated_or_unknown_period_counts(self):
        self.assertEqual(kinds(row([issue(moves=[("03-13", "?")])])), [("removed", 5)])

    def test_before_the_last_day_counts(self):
        self.assertEqual(kinds(row([issue(moves=[("03-12", "N")])])), [("removed", 5)])

    def test_a_closed_issue_is_not_rollover(self):
        self.assertEqual(kinds(row([issue(moves=[("03-13", "N")], closed="03-06")])), [("removed", 5)])

    def test_issue_closed_later_was_open_when_it_rolled_over(self):
        self.assertEqual(row([issue(moves=[("03-13", "N")], closed="03-20")])["changed"], 0)

    def test_work_added_and_rolled_over_counts_only_the_addition(self):
        r = row(base() + [issue("A", start=None, moves=[("03-05", "S"), ("03-13", "N")])])
        self.assertEqual((kinds(r), r["final"]), ([("added", 5)], 55))


class ReestimateTest(unittest.TestCase):
    def test_difference_counts_in_its_own_column(self):
        r = row([issue(first_points=3.0, estimates=[("03-05", 8.0)])])
        self.assertEqual((r["initial"], r["reestimated"], r["added"], r["changed"], r["final"]), (3, 5, 0, 5, 8))
        self.assertEqual((r["changes"][0]["before"], r["changes"][0]["after"]), (3, 8))

    def test_every_change_counts_in_both_directions(self):
        r = row([issue(first_points=3.0, estimates=[("03-05", 8.0), ("03-06", 3.0)])])
        self.assertEqual((r["reestimated"], r["final"]), (10, 3))

    def test_first_estimate_is_not_a_change_and_sizes_the_past(self):
        r = row([issue(first_points=None, estimates=[("03-05", 5.0)])])
        self.assertEqual((r["initial"], r["changed"]), (5, 0))

    def test_clearing_and_setting_again_is_one_change(self):
        r = row([issue(first_points=3.0, estimates=[("03-05", None), ("03-06", 8.0)])])
        self.assertEqual(r["reestimated"], 5)

    def test_before_the_cutoff_only_sets_the_initial_size(self):
        r = row([issue(first_points=3.0, estimates=[("03-02", 8.0)])])
        self.assertEqual((r["initial"], r["changed"]), (8, 0))

    def test_outside_the_period_is_ignored(self):
        r = row(base() + [issue("X", start=None, first_points=3.0, estimates=[("03-05", 8.0)])])
        self.assertEqual(r["changed"], 0)

    def test_sizes_are_those_of_the_moment(self):
        r = row(base() + [issue("A", start=None, first_points=2.0, moves=[("03-04", "S"), ("03-08", None)],
                                estimates=[("03-06", 3.0)])])
        self.assertEqual(kinds(r), [("added", 2), ("re-estimated", 1), ("removed", 3)])

    def test_estimate_at_the_same_instant_is_applied_before_the_move(self):
        r = row(base() + [issue("A", start=None, first_points=2.0, moves=[("03-04", "S")], estimates=[("03-04", 8.0)])])
        self.assertEqual(kinds(r), [("added", 8)])

    def test_when_counting_issues_it_is_listed_but_not_summed(self):
        r = row([issue(first_points=3.0, estimates=[("03-05", 8.0)])], unit="count")
        self.assertEqual((r["initial"], r["changed"], r["changes"][0]["type"]), (1, 0, "re-estimated"))


class CancelledTest(unittest.TestCase):
    def test_counts_as_out_of_scope(self):
        r = row(base(9) + [issue("C", cancelled="03-06", closed="03-06")])
        self.assertEqual((kinds(r), r["removed"], r["cancelled"], r["final"]), ([("cancelled", 5)], 0, 5, 45))

    def test_removing_it_afterwards_does_not_count_again(self):
        r = row(base(9) + [issue("C", cancelled="03-06", closed="03-06", moves=[("03-08", None)])])
        self.assertEqual(r["changed"], 5)

    def test_cancelled_before_the_cutoff_is_not_scope(self):
        r = row(base() + [issue("C", cancelled="03-01", closed="03-01")])
        self.assertEqual((r["initial"], r["changed"]), (50, 0))

    def test_cancelled_after_the_end_is_ignored(self):
        self.assertEqual(row([issue(cancelled="03-20", closed="03-20")])["changed"], 0)

    def test_added_then_cancelled_counts_twice(self):
        r = row(base() + [issue("A", start=None, moves=[("03-04", "S")], cancelled="03-06", closed="03-06")])
        self.assertEqual(r["changed"], 10)


class ThirdsTest(unittest.TestCase):
    def test_changes_by_third_of_the_period(self):
        days = ("03-03", "03-05", "03-06", "03-09", "03-10", "03-13")  # 12 days: 02-05, 06-09, 10-13
        r = row(base() + [issue(d, start=None, points=1.0, moves=[(d, "S")]) for d in days])
        self.assertEqual(r["thirds"], [2, 2, 2])


class UnitTest(unittest.TestCase):
    def test_auto_uses_points_when_all_have_them(self):
        self.assertEqual(analyze(base())["unit"], "points")

    def test_auto_counts_issues_otherwise_and_says_so(self):
        res = analyze(base() + [issue("U", points=None)])
        self.assertEqual(res["unit"], "issues")
        self.assertIn("1 issue(s) have no points", " ".join(res["notes"]))

    def test_issues_outside_the_periods_do_not_decide_the_unit(self):
        self.assertEqual(analyze(base() + [issue("U", start=None, points=None)])["unit"], "points")

    def test_forced_points_count_unpointed_as_zero(self):
        res = analyze(base() + [issue("U", points=None)], unit="points")
        self.assertEqual(next(r for r in res["rows"] if r["id"] == "S")["initial"], 50)
        self.assertIn("count as 0", " ".join(res["notes"]))


class SummaryTest(unittest.TestCase):
    def rows(self, *values):
        return [{"name": f"P{n}", "status": "finished", "stability": v} for n, v in enumerate(values)]

    def test_statistics(self):
        s = ss.summarize(self.rows(0.91, 0.82, 0.43))
        self.assertEqual((s["n"], s["median"], s["rating"]), (3, 0.82, "moderate"))
        self.assertEqual((s["latest"], s["latest_rating"], s["trend_word"]), (0.43, "unstable", "getting less stable"))

    def test_trend_dead_band(self):
        self.assertEqual(ss.summarize(self.rows(0.8, 0.84))["trend_word"], "flat")
        self.assertEqual(ss.summarize(self.rows(0.5, 0.9))["trend_word"], "getting more stable")
        self.assertEqual(ss.summarize(self.rows(0.5))["trend_word"], "not enough periods")

    def test_periods_without_initial_scope_and_current_stay_out(self):
        rows = self.rows(None, 0.8) + [{"name": "C", "status": "current", "stability": 1.0}]
        self.assertEqual(ss.summarize(rows)["n"], 1)
        self.assertIsNone(ss.summarize([]))


class SelectionTest(unittest.TestCase):
    def test_last_finished_plus_current_without_upcoming(self):
        res = analyze(base(), today=dt.date(2026, 3, 6), last=1)
        self.assertEqual([r["id"] for r in res["rows"]], ["O", "S"])

    def test_named_period_is_case_insensitive(self):
        self.assertEqual([r["id"] for r in analyze(base(), period=["sprint"])["rows"]], ["S"])

    def test_unknown_period_stops_with_the_list(self):
        with self.assertRaises(SystemExit) as cm:
            analyze(base(), period=["Nope"])
        self.assertIn("Sprint", str(cm.exception.code))

    def test_upcoming_named_period_is_explained(self):
        res = analyze(base(), today=dt.date(2026, 3, 6), period=["Next"])
        self.assertEqual(res["rows"], [])
        self.assertIn("has not started", " ".join(res["notes"]))

    def test_periods_without_dates_are_not_analyzed(self):
        res = analyze(base(), periods=[period(), {**NEXT, "start": None}, OLD])
        self.assertNotIn("N", [r["id"] for r in res["rows"]])

    def test_missing_history_is_disclosed(self):
        self.assertIn("no milestone history", " ".join(analyze([issue(missing_history=True)])["notes"]))


if __name__ == "__main__":
    unittest.main()
