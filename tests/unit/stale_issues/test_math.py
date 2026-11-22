"""Pure analysis of stale.py: the stale rule, kinds, idle days, shares, per person, aging, next steps."""
import datetime as dt
import unittest

from tests.unit._load import load

st = load("stale-issues", "stale.py")

TODAY = dt.date(2026, 3, 10)
NOW = dt.datetime(2026, 3, 10, 23, 59, tzinfo=dt.timezone.utc)  # analyze() measures to the end of today
SPRINT = {"name": "Sprint 5", "start": "2026-03-01", "end": "2026-03-14"}


def issue(key, updated, started=False, blocked=False, stale_mark=False, points=None, assignees=(), created=None):
    return st.new_issue(key=key, title=key, url=f"https://example.com/{key}", updated=updated, created=created,
                        status="open", started=started, blocked=blocked, stale_mark=stale_mark, points=points,
                        assignees=list(assignees))


def sample():
    return [
        issue("A", "2026-02-20T10:00:00Z", started=True, points=5, assignees=["alice"]),          # idle 18.6, stalled
        issue("B", "2026-01-01T00:00:00Z", points=3),                                             # not started, clock at 03-01
        issue("C", "2026-02-01T00:00:00Z", blocked=True, points=2, assignees=["alice", "bob"]),   # idle 38, blocked
        issue("D", "2026-03-10T08:00:00Z", stale_mark=True, points=1),                            # marked stale today
        issue("E", "2026-03-05T00:00:00Z", started=True, points=8, assignees=["bob"]),            # idle 6, fresh
    ]


class IdleDaysTest(unittest.TestCase):
    def test_days_since_last_update(self):
        self.assertAlmostEqual(st.idle_days(issue("x", "2026-03-01T23:59:00Z"), NOW), 9.0)

    def test_falls_back_to_created(self):
        it = issue("x", None, created="2026-03-03T23:59:00Z")
        self.assertAlmostEqual(st.idle_days(it, NOW), 7.0)

    def test_no_dates_is_none(self):
        self.assertIsNone(st.idle_days(issue("x", None), NOW))

    def test_time_zone_offsets_are_honoured(self):
        # 02:59 at +03:00 is 23:59 UTC the day before
        self.assertAlmostEqual(st.idle_days(issue("x", "2026-02-25T02:59:00+03:00"), NOW), 14.0)

    def test_naive_timestamps_are_utc(self):
        self.assertAlmostEqual(st.idle_days(issue("x", "2026-02-24T23:59:00"), NOW), 14.0)

    def test_date_only_is_midnight_utc(self):
        self.assertAlmostEqual(st.idle_days(issue("x", "2026-03-10"), NOW) * 1440, 23 * 60 + 59)

    def test_never_negative(self):
        self.assertEqual(st.idle_days(issue("x", "2026-03-12T00:00:00Z"), NOW), 0.0)

    def test_not_started_clock_starts_at_period_start(self):
        start = st.parse_dt("2026-03-01")
        it = issue("x", "2026-01-01T00:00:00Z")
        self.assertAlmostEqual(st.idle_days(it, NOW, start) * 1440, 9 * 1440 + 23 * 60 + 59)
        self.assertGreater(st.idle_days(it, NOW), 60)  # without a period the backlog time counts

    def test_not_started_updated_after_period_start_uses_update(self):
        start = st.parse_dt("2026-03-01")
        self.assertAlmostEqual(st.idle_days(issue("x", "2026-03-05T23:59:00Z"), NOW, start), 5.0)

    def test_upcoming_period_means_zero_idle(self):
        start = st.parse_dt("2026-03-15")
        self.assertEqual(st.idle_days(issue("x", "2026-01-01T00:00:00Z"), NOW, start), 0.0)

    def test_period_start_does_not_reset_started_or_blocked_work(self):
        start = st.parse_dt("2026-03-01")
        self.assertGreater(st.idle_days(issue("x", "2026-01-01T00:00:00Z", started=True), NOW, start), 60)
        self.assertGreater(st.idle_days(issue("x", "2026-01-01T00:00:00Z", blocked=True), NOW, start), 60)


class KindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(st.kind_of(issue("x", None, started=True)), "stalled")
        self.assertEqual(st.kind_of(issue("x", None, blocked=True)), "blocked")
        self.assertEqual(st.kind_of(issue("x", None, stale_mark=True)), "marked stale")
        self.assertEqual(st.kind_of(issue("x", None)), "not started")

    def test_precedence_blocked_over_started_over_mark(self):
        self.assertEqual(st.kind_of(issue("x", None, started=True, blocked=True, stale_mark=True)), "blocked")
        self.assertEqual(st.kind_of(issue("x", None, started=True, stale_mark=True)), "stalled")


class StaleRuleTest(unittest.TestCase):
    def stale_keys(self, issues, period=None, stale_days=14):
        return [i["key"] for i in st.analyze(issues, period, stale_days, TODAY)["stale"]]

    def test_threshold_is_strictly_more_than_n_days(self):
        exactly = issue("exact", "2026-02-24T23:59:00Z")   # 14.0 days
        over = issue("over", "2026-02-24T23:58:00Z")       # 14.0007 days
        self.assertEqual(self.stale_keys([exactly, over]), ["over"])

    def test_threshold_with_time_zone(self):
        self.assertEqual(self.stale_keys([issue("tz", "2026-02-25T02:59:00+03:00")]), [])
        self.assertEqual(self.stale_keys([issue("tz", "2026-02-25T02:58:00+03:00")]), ["tz"])

    def test_custom_threshold(self):
        it = issue("x", "2026-03-02T23:58:00Z")  # 8.0 days
        self.assertEqual(self.stale_keys([it], stale_days=7), ["x"])
        self.assertEqual(self.stale_keys([dict(it)], stale_days=14), [])

    def test_stale_mark_is_stale_whatever_the_activity(self):
        self.assertEqual(self.stale_keys([issue("m", "2026-03-10T22:00:00Z", stale_mark=True)]), ["m"])

    def test_zero_threshold_disables_the_idle_rule(self):
        issues = [issue("old", "2020-01-01T00:00:00Z"), issue("m", "2026-03-10T00:00:00Z", stale_mark=True)]
        res = st.analyze(issues, None, 0, TODAY)
        self.assertEqual([i["key"] for i in res["stale"]], ["m"])
        self.assertEqual(res["aging"], [])

    def test_issue_without_dates_is_not_stale(self):
        self.assertEqual(self.stale_keys([issue("x", None)]), [])

    def test_not_started_waits_for_the_period_start(self):
        backlog = issue("b", "2025-10-01T00:00:00Z")
        self.assertEqual(self.stale_keys([dict(backlog)], SPRINT), [])                      # 10 days in the sprint
        self.assertEqual(self.stale_keys([dict(backlog)]), ["b"])                           # whole project
        started_feb24 = {"name": "S", "start": "2026-02-24", "end": "2026-03-14"}           # 14.999 days
        started_feb25 = {"name": "S", "start": "2026-02-25", "end": "2026-03-14"}           # 13.999 days
        self.assertEqual(self.stale_keys([dict(backlog)], started_feb24), ["b"])
        self.assertEqual(self.stale_keys([dict(backlog)], started_feb25), [])

    def test_period_without_start_counts_from_the_update(self):
        self.assertEqual(self.stale_keys([issue("b", "2025-10-01T00:00:00Z")], {"name": "S", "start": None,
                                                                              "end": None}), ["b"])


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.res = st.analyze(sample(), SPRINT, 14, TODAY, "milestone")

    def test_counts_and_order(self):
        r = self.res
        self.assertEqual((r["open"], r["stale_count"]), (5, 3))
        self.assertEqual([i["key"] for i in r["stale"]], ["A", "C", "D"])  # stalled, blocked, marked stale
        self.assertEqual([i["kind"] for i in r["stale"]], ["stalled", "blocked", "marked stale"])
        self.assertEqual(r["by_kind"], {"stalled": 1, "blocked": 1, "not started": 0, "marked stale": 1})
        self.assertEqual(r["unassigned"], 1)
        self.assertEqual(r["period_left"], 4)

    def test_share_of_open_points(self):
        r = self.res
        self.assertEqual(r["unit"], "points")
        self.assertEqual((r["open_size"], r["stale_size"]), (19, 8))

    def test_share_falls_back_to_issues_when_any_issue_has_no_points(self):
        issues = sample()
        issues[4]["points"] = None
        r = st.analyze(issues, SPRINT, 14, TODAY)
        self.assertEqual(r["unit"], "issues")
        self.assertEqual((r["open_size"], r["stale_size"]), (5, 3))

    def test_zero_points_still_count_as_pointed(self):
        issues = [issue("z", "2026-01-01T00:00:00Z", started=True, points=0)]
        r = st.analyze(issues, None, 14, TODAY)
        self.assertEqual((r["unit"], r["open_size"], r["stale_size"]), ("points", 0, 0))

    def test_idle_days_and_next_step_are_set(self):
        a = self.res["stale"][0]
        self.assertAlmostEqual(a["idle_days"] * 1440, 18 * 1440 + 13 * 60 + 59)
        self.assertEqual(a["next_step"], "ask @alice for status: finish, split, or hand it over")

    def test_within_a_kind_longest_idle_first(self):
        issues = [issue("short", "2026-02-20T00:00:00Z", started=True),
                  issue("long", "2026-01-20T00:00:00Z", started=True)]
        self.assertEqual([i["key"] for i in st.analyze(issues, None, 14, TODAY)["stale"]], ["long", "short"])

    def test_per_person(self):
        self.assertEqual(self.res["per_person"], [("alice", 2), ("bob", 1), ("(unassigned)", 1)])

    def test_getting_stale_is_idle_more_than_half_the_threshold(self):
        # B: not started, clock from 03-01 -> 10 days (> 7); E: 6 days (<= 7)
        self.assertEqual([i["key"] for i in self.res["aging"]], ["B"])

    def test_empty_input(self):
        r = st.analyze([], SPRINT, 14, TODAY)
        self.assertEqual((r["open"], r["stale_count"], r["unit"], r["open_size"], r["stale_size"]),
                         (0, 0, "issues", 0, 0))
        self.assertEqual(r["per_person"], [])
        self.assertEqual(r["aging"], [])

    def test_whole_project_has_no_period_left(self):
        r = st.analyze(sample(), None, 14, TODAY)
        self.assertIsNone(r["period_left"])
        self.assertIn("B", [i["key"] for i in r["stale"]])  # backlog time counts without a period

    def test_period_already_ended(self):
        r = st.analyze(sample(), {"name": "S", "start": "2026-02-20", "end": "2026-03-08"}, 14, TODAY)
        self.assertEqual(r["period_left"], -2)


class NextStepTest(unittest.TestCase):
    def step(self, assignees=(), kind="not started", left=None, where="the cycle", **kw):
        return st.next_step(issue("x", None, assignees=assignees, **kw), kind, left, where)

    def test_stalled(self):
        self.assertIn("ask @a, @b for status", self.step(["a", "b"], "stalled"))
        self.assertIn("nobody is assigned", self.step([], "stalled"))

    def test_blocked_points_to_blocked_issues(self):
        self.assertIn("blocked-issues", self.step(["a"], "blocked"))

    def test_marked_stale(self):
        self.assertEqual(self.step([], "marked stale"), "confirm it is still wanted: close it or move it out of the cycle")

    def test_not_started_near_the_end(self):
        self.assertIn("ends soon", self.step(["a"], left=7))
        self.assertNotIn("ends soon", self.step(["a"], left=8))

    def test_not_started_with_and_without_owner(self):
        self.assertIn("confirm with @a", self.step(["a"]))
        self.assertIn("unassigned and untouched", self.step([]))
        self.assertTrue(self.step([], where="the plan").endswith("move it out of the plan"))

    def test_whole_project_says_the_plan(self):
        res = st.analyze([issue("m", "2026-03-10T00:00:00Z", stale_mark=True)], None, 14, TODAY)
        self.assertTrue(res["stale"][0]["next_step"].endswith("the plan"))


class RenderTest(unittest.TestCase):
    def full(self, issues=None, period=SPRINT, today=TODAY):
        res = st.analyze(sample() if issues is None else issues, period, 14, today, "milestone")
        res.update(project="Demo Project", kind="milestone", period=period, today=today.isoformat(), stale_days=14)
        return st.render(res)

    def test_markdown_report(self):
        out = self.full()
        self.assertIn("# Stale issues — Demo Project", out)
        self.assertIn("milestone **Sprint 5** (2026-03-01 → 2026-03-14)", out)
        self.assertIn("The milestone ends in **4 day(s)**.", out)
        self.assertIn("**3 of 5 open issue(s) are stale**: 8 points (42% of the open points).", out)
        self.assertIn("By kind: stalled 1, blocked 1, marked stale 1 · unassigned 1.", out)
        self.assertIn("| [A](https://example.com/A) | A | stalled | open | 19 d | @alice | — | 5 |", out)
        self.assertIn("## Stale issues per person", out)
        self.assertIn("| (unassigned) | 1 |", out)
        self.assertIn("## Getting stale (idle for more than 7 days)", out)
        self.assertIn("- [B](https://example.com/B) B — open, idle 10 d", out)

    def test_ended_period(self):
        out = self.full(period={"name": "S", "start": None, "end": "2026-03-08"})
        self.assertIn("The milestone ended 2 day(s) ago.", out)
        self.assertIn("(? → 2026-03-08)", out)

    def test_issue_share_and_empty_report(self):
        issues = sample()
        issues[0]["points"] = None
        self.assertIn("**3 of 5 open issue(s) are stale** (60% of the open issues).", self.full(issues))
        out = self.full([], period=None)
        self.assertIn("**whole project**", out)
        self.assertIn("**0 of 0 open issue(s) are stale**.", out)
        self.assertNotIn("By kind", out)

    def test_zero_open_points_does_not_divide_by_zero(self):
        out = self.full([issue("z", "2026-01-01T00:00:00Z", started=True, points=0)], period=None)
        self.assertIn("**1 of 1 open issue(s) are stale**: 0 points.", out)

    def test_render_periods(self):
        res = {"project": "Demo", "kind": "cycle", "stale_days": 7.5,
               "periods": [{"name": "Sprint 5", "start": "2026-03-01", "end": None, "open": 4, "stale": 1}],
               "whole": {"open": 9, "stale": 3}}
        out = st.render_periods(res)
        self.assertIn("# Open cycles — Demo", out)
        self.assertIn("| # | Cycle | Start | End | Open issues | Stale (> 7.5 d) |", out)
        self.assertIn("| 1 | Sprint 5 | 2026-03-01 | — | 4 | 1 |", out)
        self.assertIn("| — | whole project |  |  | 9 | 3 |", out)


if __name__ == "__main__":
    unittest.main()
