"""Pure math of assign.py: participation points, recency decay, area/type scores, fit, load, availability."""
import datetime as dt
import unittest

from tests.unit._load import load

asg = load("assign-issues", "assign.py")

TODAY = dt.datetime(2026, 3, 1, 23, 59, tzinfo=dt.timezone.utc)
DEVS = [{"name": "alice"}, {"name": "bob"}, {"name": "carol"}]


def issue(key, areas=(), types=(), assignees=(), author=None, commenters=(), open=False, cancelled=False,
          closed="2026-03-01T23:59:00Z", period=None, points=None, **kw):
    return asg.new_issue(key=key, areas=list(areas), types=list(types), assignees=list(assignees), author=author,
                         commenters=list(commenters), open=open, cancelled=cancelled,
                         closed=None if open else closed, updated=closed, period=period, points=points, **kw)


# Hand-computed with half-life 0 (no decay):
#   I1 closed  backend/feature  assignee alice 3, author bob 1, commenter carol 1
#   I2 open    backend+frontend/bug  assignee bob 1.5 (0.75 per area), author alice 1 (0.5 per area)
#   I3 closed cancelled frontend/feature  carol assignee 3 + author 1 = 4, halved = 2
#   I4 closed  no area/bug  assignee alice 3, commenter dave (not a developer)
HISTORY = [
    issue("I1", ["backend"], ["feature"], ["alice"], "bob", ["carol"]),
    issue("I2", ["backend", "frontend"], ["bug"], ["bob"], "alice", open=True),
    issue("I3", ["frontend"], ["feature"], ["carol"], "carol", cancelled=True),
    issue("I4", [], ["bug"], ["alice"], None, ["dave"]),
]


class BuildScoresTest(unittest.TestCase):
    def setUp(self):
        self.s = asg.build_scores(HISTORY, DEVS, TODAY, 0)

    def test_area_scores_relative_to_the_top_developer(self):
        backend = {d: self.s["area"][d]["backend"] for d in ("alice", "bob", "carol")}
        self.assertAlmostEqual(backend["alice"], 100)        # 3 + 0.5 = 3.5 (top)
        self.assertAlmostEqual(backend["bob"], 50)           # 1 + 0.75 = 1.75
        self.assertAlmostEqual(backend["carol"], 100 / 3.5)  # 1
        frontend = {d: self.s["area"][d]["frontend"] for d in ("alice", "bob", "carol")}
        self.assertAlmostEqual(frontend["alice"], 25)        # 0.5 / 2
        self.assertAlmostEqual(frontend["bob"], 37.5)        # 0.75 / 2
        self.assertAlmostEqual(frontend["carol"], 100)       # cancelled: 4 x 0.5 = 2

    def test_type_scores(self):
        t = self.s["type"]
        self.assertAlmostEqual(t["alice"]["feature"], 100)   # 3
        self.assertAlmostEqual(t["carol"]["feature"], 100)   # 1 + 2
        self.assertAlmostEqual(t["bob"]["feature"], 100 / 3)
        self.assertAlmostEqual(t["alice"]["bug"], 100)       # 1 + 3
        self.assertAlmostEqual(t["bob"]["bug"], 37.5)        # 1.5
        self.assertEqual(t["carol"]["bug"], 0)

    def test_every_developer_gets_every_key_and_non_developers_are_ignored(self):
        for d in ("alice", "bob", "carol"):
            self.assertEqual(sorted(self.s["area"][d]), ["backend", "frontend"])
        self.assertNotIn("dave", self.s["area"])
        self.assertEqual(self.s["areas"], ["backend", "frontend"])
        self.assertEqual(self.s["types"], ["bug", "feature"])

    def test_evidence_counts(self):
        ev = self.s["evidence"]
        self.assertEqual(ev["alice"]["backend"], {"done": 1, "open": 0, "other": 1})
        self.assertEqual(ev["bob"]["backend"], {"done": 0, "open": 1, "other": 1})
        self.assertEqual(ev["carol"]["frontend"], {"done": 1, "open": 0, "other": 0})
        self.assertEqual(ev["carol"]["backend"], {"done": 0, "open": 0, "other": 1})

    def test_assignee_who_is_also_author_adds_both(self):
        s = asg.build_scores([issue("X", ["a"], [], ["alice"], "alice"), issue("Y", ["a"], [], ["bob"])],
                             DEVS, TODAY, 0)
        self.assertAlmostEqual(s["area"]["bob"]["a"], 75)  # 3 vs alice 3 + 1

    def test_duplicate_assignee_counts_once(self):
        s = asg.build_scores([issue("X", ["a"], [], ["alice", "alice"]), issue("Y", ["a"], [], ["bob"])],
                             DEVS, TODAY, 0)
        self.assertAlmostEqual(s["area"]["alice"]["a"], 100)
        self.assertAlmostEqual(s["area"]["bob"]["a"], 100)

    def test_empty_input(self):
        s = asg.build_scores([], DEVS, TODAY, 180)
        self.assertEqual(s["area"], {"alice": {}, "bob": {}, "carol": {}})
        self.assertEqual(s["areas"], [])
        self.assertEqual(asg.build_scores(HISTORY, [], TODAY, 0)["area"], {})

    def test_area_only_non_developers_took_part_in_counts_as_no_history(self):
        s = asg.build_scores([issue("X", ["ops"], [], ["dave"])], DEVS, TODAY, 0)
        self.assertEqual(s["areas"], ["ops"])
        self.assertNotIn("ops", s["area"]["alice"])
        self.assertEqual(asg.fit(s, "alice", issue("Q", ["ops"], [])), (0.0, None, None))

    def test_area_with_zero_points_everywhere_is_zero_not_a_division_error(self):
        s = asg.build_scores([issue("X", ["ops"], [], ["alice"], closed="2026-03-01T23:59:00Z"),
                              issue("Y", ["web"], [], ["bob"])], DEVS, TODAY, 0)
        self.assertEqual(s["area"]["carol"], {"ops": 0, "web": 0})


class RecencyTest(unittest.TestCase):
    def scores(self, bob_closed, half_life=180):
        issues = [issue("A", ["a"], [], ["alice"]), issue("B", ["a"], [], ["bob"], closed=bob_closed)]
        return asg.build_scores(issues, DEVS, TODAY, half_life)["area"]

    def test_one_half_life_counts_half(self):
        self.assertAlmostEqual(self.scores("2025-09-02T23:59:00Z")["bob"]["a"], 50)  # 180 days before TODAY

    def test_two_half_lives_count_a_quarter(self):
        self.assertAlmostEqual(self.scores("2025-03-06T23:59:00Z")["bob"]["a"], 25)  # 360 days

    def test_half_life_zero_disables_decay(self):
        self.assertAlmostEqual(self.scores("2020-01-01T00:00:00Z", half_life=0)["bob"]["a"], 100)

    def test_future_dates_do_not_boost(self):
        self.assertAlmostEqual(self.scores("2027-01-01T00:00:00Z")["bob"]["a"], 100)

    def test_time_zone_offsets_are_respected(self):
        # 2025-09-03T02:59+03:00 is 2025-09-02T23:59Z: exactly one half-life
        self.assertAlmostEqual(self.scores("2025-09-03T02:59:00+03:00")["bob"]["a"], 50)

    def test_open_issue_uses_updated_and_missing_dates_count_fully(self):
        issues = [issue("A", ["a"], [], ["alice"]),
                  asg.new_issue(key="B", areas=["a"], assignees=["bob"], open=True, updated=None, closed=None)]
        s = asg.build_scores(issues, DEVS, TODAY, 180)["area"]
        self.assertAlmostEqual(s["bob"]["a"], 50)  # open assignee 1.5 vs done 3, no decay


class FitTest(unittest.TestCase):
    def setUp(self):
        self.s = asg.build_scores(HISTORY, DEVS, TODAY, 0)

    def test_eighty_percent_area_twenty_percent_type(self):
        f, area, typ = asg.fit(self.s, "alice", issue("Q", ["backend", "frontend"], ["feature"]))
        self.assertAlmostEqual(area, 62.5)
        self.assertAlmostEqual(typ, 100)
        self.assertAlmostEqual(f, 70)

    def test_unknown_area_uses_the_type_only(self):
        f, area, typ = asg.fit(self.s, "bob", issue("Q", ["mobile"], ["bug"]))
        self.assertIsNone(area)
        self.assertAlmostEqual(f, 37.5)

    def test_no_type_uses_the_area_only(self):
        f, area, typ = asg.fit(self.s, "carol", issue("Q", ["frontend"], []))
        self.assertIsNone(typ)
        self.assertAlmostEqual(f, 100)

    def test_nothing_known_is_zero(self):
        self.assertEqual(asg.fit(self.s, "alice", issue("Q", [], [])), (0.0, None, None))
        self.assertEqual(asg.fit(self.s, "alice", issue("Q", ["mobile"], ["chore"])), (0.0, None, None))


class LoadsTest(unittest.TestCase):
    def test_points_when_every_open_issue_of_the_period_has_them(self):
        issues = [issue("X1", assignees=["alice"], open=True, period="S1", points=3),
                  issue("X2", assignees=["alice", "bob"], open=True, period="S1", points=5),
                  issue("X3", open=True, period="S1", points=2),
                  issue("X4", assignees=["bob"], period="S1", points=8),             # closed: ignored
                  issue("X5", assignees=["carol"], open=True, period="S2", points=None)]  # other period
        load, unit = asg.loads(issues, DEVS, "S1")
        self.assertEqual(unit, "points")
        self.assertEqual({d: v["load"] for d, v in load.items()}, {"alice": 8, "bob": 5, "carol": 0})
        self.assertEqual({d: v["availability"] for d, v in load.items()}, {"alice": 0, "bob": 37.5, "carol": 100})
        self.assertEqual(load["alice"]["keys"], ["X1", "X2"])
        self.assertEqual(load["alice"]["issues"], 2)

    def test_issues_when_one_open_issue_has_no_points(self):
        issues = [issue("X1", assignees=["alice"], open=True, period="S1", points=3),
                  issue("X2", assignees=["alice", "bob"], open=True, period="S1", points=5),
                  issue("X3", open=True, period="S1", points=None)]
        load, unit = asg.loads(issues, DEVS, "S1")
        self.assertEqual(unit, "issues")
        self.assertEqual({d: v["load"] for d, v in load.items()}, {"alice": 2, "bob": 1, "carol": 0})
        self.assertEqual({d: v["availability"] for d, v in load.items()}, {"alice": 0, "bob": 50, "carol": 100})
        self.assertEqual(load["alice"]["points"], 8)

    def test_whole_project_when_period_is_none(self):
        issues = [issue("X1", assignees=["bob"], open=True, period="S1", points=1),
                  issue("X2", assignees=["bob"], open=True, period=None, points=1)]
        load, _ = asg.loads(issues, DEVS, None)
        self.assertEqual(load["bob"]["load"], 2)

    def test_nobody_loaded_means_everyone_available(self):
        load, unit = asg.loads([], DEVS, "S1")
        self.assertEqual(unit, "issues")
        self.assertTrue(all(v["availability"] == 100 for v in load.values()))
        load, unit = asg.loads([issue("X", open=True, period="S1", points=0)], DEVS, "S1")
        self.assertEqual(unit, "points")  # zero points is still a value
        self.assertTrue(all(v["availability"] == 100 for v in load.values()))


class CurrentPeriodTest(unittest.TestCase):
    PERIODS = [{"name": "S1", "start": "2026-02-16", "end": "2026-03-01"},
               {"name": "S2", "start": "2026-03-02", "end": "2026-03-15"},
               {"name": "Backlog", "start": None, "end": None}]

    def test_boundaries_are_inclusive(self):
        self.assertEqual(asg.current_period(self.PERIODS, dt.date(2026, 2, 16)), "S1")
        self.assertEqual(asg.current_period(self.PERIODS, dt.date(2026, 3, 1)), "S1")
        self.assertEqual(asg.current_period(self.PERIODS, dt.date(2026, 3, 2)), "S2")

    def test_none_or_several_is_none(self):
        self.assertIsNone(asg.current_period(self.PERIODS, dt.date(2026, 1, 1)))
        overlap = self.PERIODS + [{"name": "X", "start": "2026-02-20T00:00:00Z", "end": "2026-02-28"}]
        self.assertIsNone(asg.current_period(overlap, dt.date(2026, 2, 25)))
        self.assertIsNone(asg.current_period([], dt.date(2026, 2, 25)))


class FindMemberTest(unittest.TestCase):
    DEVS = [{"name": "alice", "display": "Alice Doe", "email": "alice@example.com"},
            {"name": "bob", "display": "Bob Roe", "email": None}]
    ALL = DEVS + [{"name": "olga", "display": "Olga Owner"}, {"name": "alicia", "display": None}]

    def test_exact_name_display_or_email_case_insensitive(self):
        self.assertEqual(asg.find_member(self.DEVS, self.ALL, "@Alice")["name"], "alice")
        self.assertEqual(asg.find_member(self.DEVS, self.ALL, "bob roe")["name"], "bob")
        self.assertEqual(asg.find_member(self.DEVS, self.ALL, "ALICE@example.com")["name"], "alice")

    def test_unique_substring_and_fallback_to_all_members(self):
        self.assertEqual(asg.find_member(self.DEVS, self.ALL, "roe")["name"], "bob")
        self.assertEqual(asg.find_member(self.DEVS, self.ALL, "olga")["name"], "olga")

    def test_ambiguous_or_unknown_is_none(self):
        self.assertIsNone(asg.find_member(self.DEVS, self.ALL, "zed"))
        self.assertIsNone(asg.find_member([], self.ALL, "ali"))  # alice and alicia


if __name__ == "__main__":
    unittest.main()
