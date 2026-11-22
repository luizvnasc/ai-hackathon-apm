"""The rules of quality.py: what "to fix" is, which period a pass belongs to, and the quality of a period."""
import argparse
import datetime as dt
import unittest

from tests.unit._load import load

dq = load("delivery-quality", "quality.py")
UTC = dt.timezone.utc
FIX = {"tofix"}


def at(day, hour=12):
    return dt.datetime(2026, 3, day, hour, tzinfo=UTC)


def issue(key="#1", closed=None, period="S1", moves=(), initial=None, fixes=(), cancelled=False):
    return dq.new_issue(key=key, title=f"Task {key}", url=f"https://x/{key}", closed=closed, period=period,
                        initial=initial, moves=list(moves), fixes=list(fixes), cancelled=cancelled)


def period(name="S1", start="2026-03-02", end="2026-03-15", status="finished"):
    return {"name": name, "start": start, "end": end, "status": status, "closed": status == "finished"}


class FixNameTest(unittest.TestCase):
    def test_spelling_does_not_matter(self):
        for name in ("to fix", "To-Fix", "TOFIX", "to_fix", " to  fix "):
            self.assertEqual(dq.simplified(name), "tofix", name)

    def test_flow_scopes(self):
        for label in ("workflow::to-fix", "Status::To Fix", "state :: tofix", "to-fix"):
            self.assertTrue(dq.is_fix_label(label, FIX), label)

    def test_other_scopes_are_not_a_step_of_the_flow(self):
        for label in ("type::to-fix", "area::to fix", "workflow::fixed", "workflow::to-test", "bug"):
            self.assertFalse(dq.is_fix_label(label, FIX), label)

    def test_other_names(self):
        names = {dq.simplified(n) for n in dq.fix_names_arg("Devolvida, rework")}
        self.assertTrue(dq.is_fix_label("workflow::devolvida", names))
        self.assertFalse(dq.is_fix_label("workflow::to-fix", names))

    def test_no_name_is_refused(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            dq.fix_names_arg(" , ")

    def test_plane_state_and_label(self):
        self.assertTrue(dq.went_to_fix({"field": "state", "new_value": "To Fix"}, FIX))
        self.assertTrue(dq.went_to_fix({"field": "labels", "new_value": "to-fix"}, FIX))

    def test_plane_changes_that_are_not_a_pass(self):
        for activity in ({"field": "state", "new_value": "In Progress"},
                         {"field": "labels", "new_value": ""},  # the label was removed
                         {"field": "name", "new_value": "to fix"},
                         {"field": "state", "new_value": None}):
            self.assertFalse(dq.went_to_fix(activity, FIX), activity)


class PeriodAtTest(unittest.TestCase):
    def test_without_history_it_is_where_it_is_now(self):
        self.assertEqual(dq.period_at(issue(period="S2"), at(1)), "S2")

    def test_follows_the_moves(self):
        moved = issue(period="S2", initial=None, moves=[(at(2), "S1"), (at(9), "S2")])
        self.assertIsNone(dq.period_at(moved, at(1)))
        self.assertEqual(dq.period_at(moved, at(5)), "S1")
        self.assertEqual(dq.period_at(moved, at(9)), "S2")

    def test_initial_period_before_the_first_move(self):
        moved = issue(period=None, initial="S1", moves=[(at(5), None)])
        self.assertEqual(dq.period_at(moved, at(4)), "S1")
        self.assertIsNone(dq.period_at(moved, at(6)))


class DeliveredTest(unittest.TestCase):
    def test_closed_in_the_period(self):
        self.assertTrue(dq.was_delivered(issue(closed=at(10)), "S1"))

    def test_open_or_cancelled_is_not_delivered(self):
        self.assertFalse(dq.was_delivered(issue(), "S1"))
        self.assertFalse(dq.was_delivered(issue(closed=at(10), cancelled=True), "S1"))

    def test_closed_after_moving_out_belongs_to_the_new_period(self):
        moved = issue(closed=at(12), period="S2", initial="S1", moves=[(at(10), "S2")])
        self.assertFalse(dq.was_delivered(moved, "S1"))
        self.assertTrue(dq.was_delivered(moved, "S2"))

    def test_only_passes_made_while_in_the_period(self):
        moved = issue(period="S2", initial="S1", moves=[(at(10), "S2")], fixes=[at(5), at(11), at(12)])
        self.assertEqual(dq.passes_in(moved, "S1"), [at(5)])
        self.assertEqual(dq.passes_in(moved, "S2"), [at(11), at(12)])


class HistoryNeededTest(unittest.TestCase):
    SINCE = at(1)

    def test_changed_since_the_periods_began(self):
        self.assertTrue(dq.needs_history(issue(period="old"), at(2), self.SINCE, {"S1"}))

    def test_untouched_and_elsewhere_has_nothing_to_read(self):
        self.assertFalse(dq.needs_history(issue(period="old"), dt.datetime(2026, 2, 1, tzinfo=UTC), self.SINCE, {"S1"}))

    def test_in_an_analyzed_period_it_is_always_read(self):
        # Planned into S1 and sent to fix before S1 started: that pass belongs to S1
        self.assertTrue(dq.needs_history(issue(period="S1"), dt.datetime(2026, 2, 1, tzinfo=UTC), self.SINCE, {"S1"}))

    def test_no_update_date(self):
        self.assertTrue(dq.needs_history(issue(period="old"), None, self.SINCE, {"S1"}))


class MeasureTest(unittest.TestCase):
    def measure(self, issues):
        return dq.measure(period(), issues, UTC)

    def test_quality_is_the_share_that_never_went_to_fix(self):
        row = self.measure([issue("#1", closed=at(10), fixes=[at(5)]), issue("#2", closed=at(10)),
                            issue("#3", closed=at(11)), issue("#4", closed=at(12))])
        self.assertEqual((row["delivered"], row["went_to_fix"], row["quality"], row["rating"]), (4, 1, 0.75, "attention"))

    def test_an_issue_counts_once_and_every_pass_is_listed(self):
        row = self.measure([issue("#1", closed=at(10), fixes=[at(3), at(5), at(7)]), issue("#2", closed=at(10))])
        self.assertEqual((row["went_to_fix"], row["passes"], row["quality"]), (1, 3, 0.5))
        self.assertEqual(row["returned"][0]["passes"], ["2026-03-03", "2026-03-05", "2026-03-07"])

    def test_pass_after_the_end_counts_while_the_issue_stays(self):
        row = self.measure([issue("#1", closed=at(20), fixes=[at(18)])])
        self.assertEqual((row["delivered"], row["went_to_fix"]), (1, 1))

    def test_still_open_is_listed_and_not_counted(self):
        row = self.measure([issue("#1", fixes=[at(5)]), issue("#2", closed=at(10))])
        self.assertEqual((row["delivered"], row["went_to_fix"], row["quality"]), (1, 0, 1.0))
        self.assertEqual([i["key"] for i in row["still_open"]], ["#1"])

    def test_cancelled_is_neither_counted_nor_listed(self):
        row = self.measure([issue("#1", closed=at(10), cancelled=True, fixes=[at(5)])])
        self.assertEqual((row["delivered"], row["quality"], row["returned"], row["still_open"]), (0, None, [], []))

    def test_days_of_the_passes_are_the_projects(self):
        night = dt.datetime(2026, 3, 6, 1, tzinfo=UTC)  # still the 5th at -03:00
        row = dq.measure(period(), [issue(closed=at(10), fixes=[night])], dt.timezone(dt.timedelta(hours=-3)))
        self.assertEqual(row["returned"][0]["passes"], ["2026-03-05"])


class RatingTest(unittest.TestCase):
    def test_limits(self):
        cases = ((1.0, "good"), (0.9, "good"), (1 - 3 / 30, "good"), (0.89, "attention"), (0.75, "attention"),
                 (0.74, "poor"), (0.0, "poor"), (None, None))
        for quality, expected in cases:
            self.assertEqual(dq.rating(quality), expected, quality)

    def test_a_percentage_never_contradicts_its_rating(self):
        self.assertEqual(dq.pct(0.8996), "89%")
        self.assertEqual(dq.pct(0.666), "67%")
        self.assertEqual(dq.pct(None), "—")


class HistoryTest(unittest.TestCase):
    def rows(self, *qualities, current=None):
        rows = [{"status": "finished", "quality": q} for q in qualities]
        return rows + ([{"status": "current", "quality": current}] if current is not None else [])

    def test_median_and_trend_of_finished_periods(self):
        summary = dq.summarize(self.rows(0.75, 2 / 3, 2 / 3, 1.0, current=0.1))
        self.assertEqual((summary["n"], summary["latest"], summary["rating"]), (4, 1.0, "poor"))
        self.assertAlmostEqual(summary["median"], 0.7083, places=3)
        self.assertEqual(summary["trend_word"], "improving")

    def test_trend_words(self):
        self.assertEqual(dq.trend_word(None), "not enough periods")
        self.assertEqual(dq.trend_word(0.04), "steady")
        self.assertEqual(dq.trend_word(-0.06), "getting worse")

    def test_no_finished_delivery(self):
        self.assertIsNone(dq.summarize(self.rows(None, current=0.5)))


class SelectTest(unittest.TestCase):
    PERIODS = [period("S1"), period("S2"), period("S3"), period("S4", status="current"), period("S5", status="upcoming")]

    def test_default_is_the_last_finished_plus_the_current(self):
        self.assertEqual([p["name"] for p in dq.select(self.PERIODS, None, 2)], ["S2", "S3", "S4"])

    def test_by_name_ignoring_case(self):
        self.assertEqual([p["name"] for p in dq.select(self.PERIODS, [" s1 "], 6)], ["S1"])

    def test_unknown_or_upcoming_stops_with_the_valid_names(self):
        for name in ("nope", "S5"):
            with self.assertRaises(SystemExit) as stop:
                dq.select(self.PERIODS, [name], 6)
            self.assertIn("Available: S1, S2, S3, S4", stop.exception.code)

    def test_status(self):
        periods = [{"name": "a", "start": "2026-03-01", "end": "2026-03-10", "closed": False},
                   {"name": "b", "start": "2026-03-11", "end": "2026-03-20", "closed": False},
                   {"name": "c", "start": "2026-03-21", "end": "2026-03-30", "closed": False},
                   {"name": "undated", "start": None, "end": None, "closed": False}]
        marked = dq.mark_status(periods, dt.date(2026, 3, 15))
        self.assertEqual([(p["name"], p["status"]) for p in marked], [("a", "finished"), ("b", "current"), ("c", "upcoming")])


class NotesTest(unittest.TestCase):
    def test_no_pass_anywhere_asks_for_the_name(self):
        rows = [dq.measure(period(), [issue(closed=at(10))], UTC)]
        notes = dq.notes_for(rows, dq.summarize(rows), [], "milestone")
        self.assertTrue(any("--fix-names" in n for n in notes))

    def test_missing_history_and_empty_period(self):
        rows = [dq.measure(period(), [], UTC)]
        notes = dq.notes_for(rows, None, [dict(issue(), missing_history=True)], "cycle")
        self.assertTrue(any("Nothing delivered in S1" in n for n in notes))
        self.assertTrue(any("1 issue(s) have no cycle history" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
