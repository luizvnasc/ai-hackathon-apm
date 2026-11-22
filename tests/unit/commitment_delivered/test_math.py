"""Pure math of commitment.py: history at a cutoff, classification, rows and statistics (no network)."""
import argparse
import datetime as dt
import unittest

from tests.unit._load import load

cd = load("commitment-delivered", "commitment.py")
UTC = dt.timezone.utc


def t(text):
    return cd.parse_dt(text)


def issue(key, period, points=1.0, created="2026-01-01T00:00:00Z", closed=None, cancelled=False,
          assignees=(), events=None, initial=None):
    if events is not None:
        events = [(t(when), p) for when, p in events]
    return cd.new_issue(key=key, title=f"Issue {key}", url=f"https://x/{key}", period=period, points=points,
                        created=created, closed=closed, cancelled=cancelled, assignees=list(assignees),
                        events=events, initial=initial)


def period(name, start, end, status="finished", closed=False):
    return {"name": name, "start": start, "end": end, "closed": closed, "status": status}


S1 = period("S1", "2026-01-05", "2026-01-18")


class DatesTest(unittest.TestCase):
    def test_parse_dt_handles_dates_z_offsets_and_naive_values(self):
        self.assertEqual(t("2026-01-05"), dt.datetime(2026, 1, 5, tzinfo=UTC))
        self.assertEqual(t("2026-01-05T10:00:00Z"), dt.datetime(2026, 1, 5, 10, tzinfo=UTC))
        self.assertEqual(t("2026-01-05T10:00:00"), dt.datetime(2026, 1, 5, 10, tzinfo=UTC))
        self.assertEqual(t("2026-01-05T23:30:00-03:00"), dt.datetime(2026, 1, 6, 2, 30, tzinfo=UTC))
        self.assertIsNone(t(None))
        self.assertIsNone(t(""))

    def test_end_of_day(self):
        self.assertEqual(cd.end_of(dt.date(2026, 1, 5)), dt.datetime(2026, 1, 5, 23, 59, 59, tzinfo=UTC))


class PeriodAtTest(unittest.TestCase):
    def test_before_creation_is_nowhere(self):
        it = issue("a", "S1", created="2026-01-10T00:00:00Z")
        self.assertIsNone(cd.period_at(it, t("2026-01-09T23:59:59Z")))
        self.assertEqual(cd.period_at(it, t("2026-01-10T00:00:00Z")), "S1")

    def test_without_history_the_current_period_holds_since_creation(self):
        it = issue("a", "S2")
        self.assertEqual(cd.period_at(it, t("2026-01-02T00:00:00Z")), "S2")
        self.assertEqual(cd.period_at(it, t("2026-12-31T00:00:00Z")), "S2")

    def test_history_is_replayed_up_to_the_cutoff(self):
        it = issue("a", "S2", initial="S0", events=[("2026-01-03T10:00:00Z", "S1"),
                                                     ("2026-01-10T10:00:00Z", None),
                                                     ("2026-01-20T10:00:00Z", "S2")])
        self.assertEqual(cd.period_at(it, t("2026-01-02T00:00:00Z")), "S0")       # initial
        self.assertEqual(cd.period_at(it, t("2026-01-03T10:00:00Z")), "S1")       # event time is inclusive
        self.assertEqual(cd.period_at(it, t("2026-01-09T00:00:00Z")), "S1")
        self.assertEqual(cd.period_at(it, t("2026-01-15T00:00:00Z")), None)       # removed
        self.assertEqual(cd.period_at(it, t("2026-02-01T00:00:00Z")), "S2")

    def test_empty_history_falls_back_to_initial(self):
        it = issue("a", "S2", initial="S1", events=[])
        self.assertEqual(cd.period_at(it, t("2026-01-15T00:00:00Z")), "S1")


class EverInTest(unittest.TestCase):
    def test_without_history_uses_creation_inside_the_window(self):
        it = issue("a", "S1", created="2026-01-08T00:00:00Z")
        self.assertTrue(cd.ever_in(it, "S1", t("2026-01-05T23:59:59Z"), t("2026-01-18T23:59:59Z")))
        self.assertFalse(cd.ever_in(it, "S2", t("2026-01-05T23:59:59Z"), t("2026-01-18T23:59:59Z")))
        # `after` is exclusive, `until` inclusive
        self.assertFalse(cd.ever_in(it, "S1", t("2026-01-08T00:00:00Z"), t("2026-01-18T23:59:59Z")))
        self.assertTrue(cd.ever_in(it, "S1", t("2026-01-07T00:00:00Z"), t("2026-01-08T00:00:00Z")))

    def test_with_history_looks_at_the_join_events(self):
        it = issue("a", "S2", events=[("2026-01-03T10:00:00Z", "S1"), ("2026-01-22T10:00:00Z", "S2")])
        self.assertTrue(cd.ever_in(it, "S2", t("2026-01-19T23:59:59Z"), t("2026-02-01T23:59:59Z")))
        self.assertFalse(cd.ever_in(it, "S1", t("2026-01-05T23:59:59Z"), t("2026-01-18T23:59:59Z")))

    def test_missing_created_never_counts_as_added(self):
        it = issue("a", "S1", created=None)
        self.assertFalse(cd.ever_in(it, "S1", t("2026-01-01T00:00:00Z"), t("2026-12-31T00:00:00Z")))


class UnitTest(unittest.TestCase):
    def test_choose_unit(self):
        pointed = [issue("a", "S1", 3.0), issue("b", "S1", 0.0)]
        self.assertEqual(cd.choose_unit(pointed, "auto"), "points")   # zero points is still pointed
        self.assertEqual(cd.choose_unit(pointed + [issue("c", "S1", None)], "auto"), "count")
        self.assertEqual(cd.choose_unit([], "auto"), "count")
        self.assertEqual(cd.choose_unit([issue("c", "S1", None)], "points"), "points")
        self.assertEqual(cd.choose_unit(pointed, "count"), "count")

    def test_size(self):
        self.assertEqual(cd.size(issue("a", "S1", 3.0), "points"), 3.0)
        self.assertEqual(cd.size(issue("a", "S1", None), "points"), 0)
        self.assertEqual(cd.size(issue("a", "S1", 3.0), "count"), 1)


class DeliveredTest(unittest.TestCase):
    END = cd.end_of(dt.date(2026, 1, 18))

    def test_closed_in_the_period_by_the_end(self):
        self.assertTrue(cd.delivered(issue("a", "S1", closed="2026-01-18T23:59:59Z"), "S1", self.END))
        self.assertFalse(cd.delivered(issue("a", "S1", closed="2026-01-19T00:00:00Z"), "S1", self.END))
        self.assertFalse(cd.delivered(issue("a", "S1"), "S1", self.END))

    def test_time_zone_offset_moves_a_late_evening_close_past_the_end(self):
        # 23:30 at UTC-3 on the end day is 02:30 UTC the next day.
        self.assertFalse(cd.delivered(issue("a", "S1", closed="2026-01-18T23:30:00-03:00"), "S1", self.END))
        self.assertTrue(cd.delivered(issue("a", "S1", closed="2026-01-18T23:30:00+03:00"), "S1", self.END))

    def test_cancelled_is_never_delivered(self):
        self.assertFalse(cd.delivered(issue("a", "S1", closed="2026-01-10T00:00:00Z", cancelled=True), "S1", self.END))

    def test_closed_after_moving_out_is_not_delivered_here(self):
        it = issue("a", "S2", closed="2026-01-12T00:00:00Z",
                   events=[("2026-01-02T00:00:00Z", "S1"), ("2026-01-10T00:00:00Z", "S2")])
        self.assertFalse(cd.delivered(it, "S1", self.END))

    def test_moved_out_after_closing_still_counts(self):
        it = issue("a", "S2", closed="2026-01-12T00:00:00Z",
                   events=[("2026-01-02T00:00:00Z", "S1"), ("2026-01-14T00:00:00Z", "S2")])
        self.assertTrue(cd.delivered(it, "S1", self.END))


class ClassifyTest(unittest.TestCase):
    TODAY = dt.date(2026, 3, 1)

    def issues(self):
        return [
            issue("done", "S1", closed="2026-01-10T00:00:00Z"),
            issue("late", "S1", closed="2026-01-20T00:00:00Z"),
            issue("open", "S1"),
            issue("moved", "S2", events=[("2026-01-02T00:00:00Z", "S1"), ("2026-01-10T00:00:00Z", "S2")]),
            issue("dropped", None, events=[("2026-01-02T00:00:00Z", "S1"), ("2026-01-10T00:00:00Z", None)]),
            issue("cx", "S1", closed="2026-01-12T00:00:00Z", cancelled=True),
            issue("cx-late", "S1", closed="2026-01-25T00:00:00Z", cancelled=True),
            issue("new", "S1", created="2026-01-08T00:00:00Z", closed="2026-01-15T00:00:00Z"),
            issue("new-open", "S1", created="2026-01-09T00:00:00Z"),
            issue("pulled-in", "S1", events=[("2026-01-02T00:00:00Z", "S0"), ("2026-01-07T00:00:00Z", "S1")],
                  closed="2026-01-17T00:00:00Z"),
            issue("future", "S1", created="2026-01-20T00:00:00Z"),
            issue("elsewhere", "S9"),
        ]

    def keys(self, rows):
        return {k: sorted(i["key"] for i in v) for k, v in rows.items()}

    def test_groups(self):
        rows = self.keys(cd.classify(S1, self.issues(), 0, self.TODAY))
        self.assertEqual(rows["committed"], ["cx", "cx-late", "done", "dropped", "late", "moved", "open"])
        self.assertEqual(rows["delivered"], ["done"])
        self.assertEqual(rows["cancelled"], ["cx"])
        self.assertEqual(rows["removed"], ["dropped", "moved"])
        self.assertEqual(rows["carried"], ["cx-late", "late", "open"])  # cancelled after the end is carried over
        self.assertEqual(rows["added"], ["new", "new-open", "pulled-in"])
        self.assertEqual(rows["added_done"], ["new", "pulled-in"])

    def test_grace_days_move_the_cutoff(self):
        rows = self.keys(cd.classify(S1, self.issues(), 3, self.TODAY))  # cutoff: end of 2026-01-08
        self.assertIn("new", rows["committed"])
        self.assertIn("pulled-in", rows["committed"])
        self.assertEqual(rows["added"], ["new-open"])  # created 01-09, after the cutoff
        self.assertIn("dropped", rows["committed"])  # still in S1 on 01-08, removed 01-10
        self.assertIn("dropped", rows["removed"])
        rows = self.keys(cd.classify(S1, self.issues(), 5, self.TODAY))  # cutoff: end of 2026-01-10
        self.assertNotIn("dropped", rows["committed"])
        self.assertNotIn("moved", rows["committed"])

    def test_current_period_stops_today(self):
        cur = period("S1", "2026-01-05", "2026-01-18", status="current")
        rows = self.keys(cd.classify(cur, self.issues(), 0, dt.date(2026, 1, 12)))
        self.assertEqual(rows["delivered"], ["done"])
        self.assertEqual(rows["cancelled"], ["cx"])
        self.assertEqual(rows["added_done"], [])  # "new" closes on 01-15, after today
        self.assertIn("new", rows["added"])
        self.assertNotIn("pulled-in", rows["added_done"])

    def test_empty_input(self):
        rows = cd.classify(S1, [], 0, self.TODAY)
        self.assertTrue(all(v == [] for v in rows.values()))


class PeriodRowTest(unittest.TestCase):
    def groups(self):
        a = issue("a", "S1", 4.0, assignees=["alice", "bob"])
        b = issue("b", "S1", 2.0, assignees=["alice"])
        c = issue("c", "S1", 4.0)
        d = issue("d", "S1", 1.0, assignees=["carol"])
        e = issue("e", "S1", 3.0)
        return {"committed": [a, b, c, d], "delivered": [a, b], "removed": [c], "cancelled": [d], "carried": [],
                "added": [e], "added_done": [e]}

    def test_points(self):
        r = cd.period_row(S1, self.groups(), "points")
        self.assertEqual((r["committed"], r["committed_issues"], r["delivered"]), (11, 4, 6))
        self.assertAlmostEqual(r["say_do"], 6 / 11)
        self.assertEqual((r["removed"], r["cancelled"], r["carried"]), (4, 1, 0))
        self.assertEqual((r["added"], r["added_issues"], r["added_delivered"], r["total_delivered"]), (3, 1, 3, 9))
        self.assertAlmostEqual(r["vs_commitment"], 9 / 11)
        self.assertAlmostEqual(r["scope_change"], (3 - 4) / 11)
        self.assertEqual(r["per_member"], {"alice": [4.0, 4.0], "bob": [2.0, 2.0], "(unassigned)": [4.0, 0.0],
                                           "carol": [1.0, 0.0]})
        # removed and cancelled are listed apart, so the JSON never shows a cancelled issue as removed
        self.assertEqual([i["key"] for i in r["removed_items"]], ["c"])
        self.assertEqual([i["key"] for i in r["cancelled_items"]], ["d"])
        self.assertEqual(r["removed_items"][0], {"key": "c", "title": "Issue c", "url": "https://x/c", "size": 4.0})

    def test_count(self):
        r = cd.period_row(S1, self.groups(), "count")
        self.assertEqual((r["committed"], r["delivered"], r["total_delivered"]), (4, 2, 3))
        self.assertEqual(r["say_do"], 0.5)
        self.assertEqual(r["per_member"]["alice"], [1.5, 1.5])

    def test_zero_commitment_has_no_ratios(self):
        g = {k: [] for k in ("committed", "delivered", "removed", "cancelled", "carried", "added", "added_done")}
        g["added"] = g["added_done"] = [issue("e", "S1", 0.0)]
        r = cd.period_row(S1, g, "points")
        self.assertIsNone(r["say_do"])
        self.assertIsNone(r["vs_commitment"])
        self.assertIsNone(r["scope_change"])
        self.assertEqual(r["per_member"], {})


def row(name, say_do, committed=10.0, vs=None, scope=0.0):
    delivered = None if say_do is None else say_do * committed
    return {"name": name, "say_do": say_do, "committed": committed if say_do is not None else 0,
            "delivered": delivered if delivered is not None else 0,
            "vs_commitment": vs if vs is not None else say_do, "scope_change": scope if say_do is not None else None}


class StatisticsTest(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(cd.summarize([]))
        self.assertIsNone(cd.summarize([row("S1", None)]))

    def test_one_period(self):
        s = cd.summarize([row("S1", 0.95)])
        self.assertEqual((s["n"], s["median"], s["last3"], s["latest"]), (1, 0.95, 0.95, 0.95))
        self.assertIsNone(s["trend"])
        self.assertEqual(s["trend_word"], "not enough periods")
        self.assertEqual(s["reliability"], "reliable")
        self.assertFalse(s["recent_drop"])

    def test_mean_median_trend_pooled(self):
        rows = [row("S1", 0.5, 10, vs=0.6, scope=0.0), row("S2", 0.5, 10, vs=0.8, scope=0.3),
                row("S3", 1.0, 10, vs=1.2, scope=0.2)]
        s = cd.summarize(rows)
        self.assertAlmostEqual(s["mean"], 2 / 3)
        self.assertEqual((s["median"], s["min"], s["max"]), (0.5, 0.5, 1.0))
        self.assertAlmostEqual(s["last3"], 2 / 3)
        self.assertAlmostEqual(s["trend"], 0.25)
        self.assertEqual(s["trend_word"], "rising")
        self.assertAlmostEqual(s["pooled"], 20 / 30)
        self.assertEqual(s["vs_commitment"], 0.8)
        self.assertEqual(s["scope_change"], 0.2)
        self.assertEqual(s["median_delivered"], 5)
        self.assertEqual(s["rating_basis"], 0.5)
        self.assertEqual(s["reliability"], "overcommits")
        self.assertEqual(s["latest_name"], "S3")

    def test_pooled_weighs_big_periods_more(self):
        s = cd.summarize([row("S1", 1.0, 2), row("S2", 0.5, 18)])
        self.assertEqual(s["mean"], 0.75)
        self.assertAlmostEqual(s["pooled"], 11 / 20)

    def test_trend_words(self):
        self.assertEqual(cd.summarize([row("a", 0.9), row("b", 0.92), row("c", 0.94)])["trend_word"], "flat")
        self.assertEqual(cd.summarize([row("a", 1.0), row("b", 0.9), row("c", 0.8)])["trend_word"], "falling")

    def test_reliability_levels(self):
        self.assertEqual(cd.summarize([row("a", 0.9), row("b", 0.9)])["reliability"], "reliable")
        self.assertEqual(cd.summarize([row("a", 0.89), row("b", 0.89)])["reliability"], "fairly reliable")
        self.assertEqual(cd.summarize([row("a", 0.7), row("b", 0.7)])["reliability"], "fairly reliable")
        self.assertEqual(cd.summarize([row("a", 0.69), row("b", 0.69)])["reliability"], "overcommits")

    def test_rating_uses_the_lower_of_median_and_last3(self):
        rows = [row("a", 1.0), row("b", 1.0), row("c", 1.0), row("d", 0.8), row("e", 0.8), row("f", 0.8)]
        s = cd.summarize(rows)
        self.assertAlmostEqual(s["median"], 0.9)
        self.assertAlmostEqual(s["rating_basis"], 0.8)
        self.assertEqual(s["reliability"], "fairly reliable")
        self.assertFalse(s["recent_drop"])

    def test_recent_drop_lowers_the_rating_one_level(self):
        rows = [row("a", 1.0), row("b", 1.0), row("c", 1.0), row("d", 1.0), row("e", 0.95), row("f", 0.6)]
        s = cd.summarize(rows)
        self.assertEqual(s["median"], 1.0)
        self.assertAlmostEqual(s["rating_basis"], 0.85)   # last-3 average
        self.assertTrue(s["recent_drop"])                 # 0.6 < 0.7 and 40 pp under the median
        self.assertEqual(s["reliability"], "overcommits")  # fairly reliable, lowered once
        self.assertEqual(s["latest_name"], "f")

    def test_no_recent_drop_with_one_period_or_a_small_gap(self):
        self.assertFalse(cd.summarize([row("a", 0.3)])["recent_drop"])
        self.assertFalse(cd.summarize([row("a", 0.8), row("b", 0.65)])["recent_drop"])  # only 15 pp under

    def test_latest_ignores_a_trailing_period_without_commitment(self):
        # Regression: latest_name came from the last row while latest came from the last say/do,
        # so a finished period with only added work got the previous period's say/do.
        rows = [row("S1", 1.0), row("S2", 1.0), row("S3", 0.4), row("S4", None)]
        s = cd.summarize(rows)
        self.assertEqual((s["latest_name"], s["latest"]), ("S3", 0.4))
        self.assertEqual(s["n"], 3)
        # A period with nothing committed delivered nothing of a commitment; it must not pull the median down.
        self.assertEqual(s["median_delivered"], 10)


class StatusAndSelectTest(unittest.TestCase):
    PERIODS = [
        {"name": "S3", "start": "2026-02-02", "end": "2026-02-15", "closed": True},
        {"name": "S1", "start": "2026-01-05", "end": "2026-01-18", "closed": True},
        {"name": "S2", "start": "2026-01-19", "end": "2026-02-01", "closed": False},
        {"name": "S4", "start": "2026-02-16", "end": "2026-03-01", "closed": False},
        {"name": "S5", "start": "2026-03-02", "end": "2026-03-15", "closed": False},
        {"name": "Early", "start": "2026-02-16", "end": "2026-03-01", "closed": True},
        {"name": "Backlog", "start": None, "end": None, "closed": False},
    ]

    def marked(self, today="2026-02-22"):
        return cd.mark_status([dict(p) for p in self.PERIODS], dt.date.fromisoformat(today))

    def test_mark_status(self):
        got = [(p["name"], p["status"]) for p in self.marked()]
        self.assertEqual(got, [("S1", "finished"), ("S2", "finished"), ("S3", "finished"), ("S4", "current"),
                               ("Early", "finished"), ("S5", "upcoming")])

    def test_boundaries_are_inclusive(self):
        status = {p["name"]: p["status"] for p in self.marked("2026-03-01")}
        self.assertEqual(status["S4"], "current")  # end day
        status = {p["name"]: p["status"] for p in self.marked("2026-03-02")}
        self.assertEqual((status["S4"], status["S5"]), ("finished", "current"))

    def args(self, period=None, last=6):
        return argparse.Namespace(period=period, last=last)

    def test_select_defaults_to_last_finished_plus_current(self):
        chosen, notes = cd.select(self.marked(), self.args(last=2))
        self.assertEqual([p["name"] for p in chosen], ["S3", "Early", "S4"])
        self.assertEqual(notes, [])

    def test_select_by_name_skips_upcoming_and_reports_unknown(self):
        chosen, notes = cd.select(self.marked(), self.args(period=[" s1 ", "S5", "Nope"]))
        self.assertEqual([p["name"] for p in chosen], ["S1"])
        self.assertEqual(notes, ["Not found (or without dates): nope."])


if __name__ == "__main__":
    unittest.main()
