"""CLI of velocity.py: config handling, argparse defaults, report math end to end, JSON output."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load

vel = load("team-velocity", "velocity.py")

GITLAB_ENV = {"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "glpat-secret-123"}
PLANE_ENV = {"PLANE_URL": "https://plane.example.com", "PLANE_WORKSPACE": "demo-ws", "PLANE_API_KEY": "plane-secret-456"}
SECRETS = ("glpat-secret-123", "plane-secret-456")


class Run:
    """Runs main(argv) in an empty temp dir (no .env, no .mcp.json) with only `env` set."""

    def __init__(self, test, env=None):
        self.test, self.env = test, env or {}

    def __call__(self, *argv, files=None):
        out, err, code = io.StringIO(), io.StringIO(), 0
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (files or {}).items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        vel.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


def issue(key, period, points, closed=None, cancelled=False, assignees=()):
    return vel.new_issue(key=key, title=key, period=period, points=points, closed=closed,
                         cancelled=cancelled, assignees=list(assignees))


PERIODS = [
    {"name": "S3", "start": "2026-02-02", "end": "2026-02-15", "closed": True},
    {"name": "S1", "start": "2026-01-05", "end": "2026-01-18", "closed": True},
    {"name": "S2", "start": "2026-01-19", "end": "2026-02-01", "closed": False},  # finished by date
    {"name": "S4", "start": "2026-02-16", "end": "2026-03-01", "closed": False},  # current
    {"name": "S5", "start": "2026-03-02", "end": "2026-03-15", "closed": False},  # upcoming
    {"name": "Backlog", "start": None, "end": None, "closed": False},
]
ISSUES = [
    issue("a", "S1", 3.0, "2026-01-10T10:00:00Z", assignees=["alice"]),
    issue("b", "S1", 5.0, "2026-01-18T20:00:00Z", assignees=["bob"]),
    issue("c", "S1", 2.0, "2026-01-20T10:00:00Z", assignees=["bob"]),
    issue("d", "S1", 1.0),
    issue("e", "S1", 8.0, "2026-01-12T10:00:00Z", cancelled=True),
    issue("f", "S2", 5.0, "2026-01-25T10:00:00Z", assignees=["alice", "bob"]),
    issue("g", "S2", 5.0, "2026-01-30T10:00:00Z", assignees=["carol"]),
    issue("h", "S3", 8.0, "2026-02-10T10:00:00Z", assignees=["alice"]),
    issue("i", "S3", 4.0, "2026-02-14T10:00:00Z", assignees=["bob"]),
    issue("j", "S4", 3.0, "2026-02-18T10:00:00Z", assignees=["alice"]),
    issue("k", "S4", 5.0),
    issue("l", "S4", 2.0),
    issue("m", "S5", 8.0),
    issue("n", "S5", 5.0),
]


def fake_source(periods, issues, kind="cycle"):
    class Fake:
        def __init__(self, *args):
            self.args, self.kind, self.name = args, kind, "Demo (DEMO)"

        def my_projects(self):
            return [{"path": "DEMO", "name": "Demo", "url": "https://plane.example.com/demo-ws/projects/p-1/issues"}]

        def periods(self):
            return [dict(p) for p in periods]

        def issues(self):
            return [dict(i) for i in issues]
    return Fake


class ConfigTest(unittest.TestCase):
    def test_report_needs_a_project(self):
        with mock.patch.object(vel, "Plane", fake_source(PERIODS, ISSUES)):
            code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane")
        self.assertNotEqual(code, 0)
        self.assertIn("needs --project", err)

    def test_api_error_ends_with_a_message_not_a_traceback(self):
        def boom(*args, **kw):
            raise vel.ApiError(401, '{"detail": "invalid token"}', "GET",
                               "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100")

        with mock.patch.object(vel, "http", side_effect=boom), mock.patch("time.sleep"):
            code, out, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("401", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)


class ProjectsTableTest(unittest.TestCase):
    def test_lists_the_projects(self):
        with mock.patch.object(vel, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane")
        self.assertEqual(code, 0, err)
        self.assertIn("| 1 | [DEMO](https://plane.example.com/demo-ws/projects/p-1/issues) | Demo |", out)


class ArgparseTest(unittest.TestCase):
    def test_defaults(self):
        seen = {}

        def stop(**kw):
            seen.update(kw)
            raise SystemExit(0)

        parsed = {}
        real = vel.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        with mock.patch.object(vel, "require_config", side_effect=stop), \
                mock.patch.object(vel.argparse.ArgumentParser, "parse_args", capture):
            Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertEqual(parsed["last"], 6)
        self.assertEqual(parsed["unit"], "auto")
        self.assertEqual(parsed["by"], "period")
        self.assertEqual(parsed["weeks"], 8)
        self.assertEqual(parsed["format"], "md")
        self.assertIsNone(parsed["today"])
        self.assertEqual(parsed["plane_url"], "https://plane.example.com")
        self.assertEqual(parsed["workspace"], "demo-ws")
        self.assertEqual(seen["scope"], "read_api")

    def test_invalid_choice_is_rejected(self):
        code, _, err = Run(self, PLANE_ENV)("report", "--target", "jira")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


class ReportTest(unittest.TestCase):
    def run_report(self, *extra, periods=PERIODS, issues=ISSUES, target="plane"):
        env = PLANE_ENV if target == "plane" else GITLAB_ENV
        cls = "Plane" if target == "plane" else "GitLab"
        with mock.patch.object(vel, cls, fake_source(periods, issues)):
            code, out, err = Run(self, env)("report", "--target", target, "--project", "DEMO",
                                            "--today", "2026-02-22", *extra)
        self.assertEqual(code, 0, err)
        return out

    def report(self, *extra, **kw):
        return json.loads(self.run_report("--format", "json", *extra, **kw))

    def test_json_parses_and_has_the_expected_rows(self):
        res = self.report()
        self.assertEqual(res["unit"], "points")
        self.assertEqual(res["basis"], "period")
        self.assertEqual(res["today"], "2026-02-22")
        self.assertEqual([r["name"] for r in res["rows"]], ["S1", "S2", "S3"])
        self.assertEqual([r["velocity"] for r in res["rows"]], [8, 10, 12])
        self.assertEqual([r["per_week"] for r in res["rows"]], [4, 5, 6])
        self.assertEqual(res["rows"][0]["late"], 2)
        self.assertEqual(res["rows"][0]["spilled"], 1)

    def test_summary_stability_and_trend(self):
        res = self.report()
        s = res["summary"]
        self.assertEqual((s["n"], s["mean"], s["median"], s["median_per_week"], s["min_last3_per_week"]),
                         (3, 10, 10, 5, 4))
        self.assertAlmostEqual(s["trend"], 2.0)
        self.assertEqual(res["stability"], "stable")   # cv = 0.163
        self.assertEqual(res["trend_word"], "rising")  # slope 2 > 10% of mean 10

    def test_recommendation_for_the_next_period(self):
        nxt = self.report()["next"]
        self.assertEqual(nxt["name"], "S5")
        self.assertEqual(nxt["weeks"], 2.0)
        self.assertEqual(nxt["recommended"], 10)   # median 5/week x 2 weeks
        self.assertEqual(nxt["conservative"], 8)   # lowest of last 3 per week (4) x 2
        self.assertEqual(nxt["scope"], 13)

    def test_current_period(self):
        cur = self.report()["current"]
        self.assertEqual(cur["name"], "S4")
        self.assertEqual((cur["scope"], cur["done"]), (10, 3))
        self.assertAlmostEqual(cur["elapsed_pct"], 0.5)  # 7 of 14 days
        self.assertAlmostEqual(cur["pace"], 3.0)         # 3 points in 1 week
        self.assertAlmostEqual(cur["expected_total"], 8)  # 3 + 5/week x 1 week left
        self.assertAlmostEqual(cur["gap"], 2)
        self.assertEqual(cur["verdict"], "off track")    # 2 > 15% of 10

    def test_per_member(self):
        res = self.report()
        self.assertEqual(res["members"], ["alice", "bob", "carol"])
        self.assertEqual(res["member_totals"], {"alice": 13.5, "bob": 11.5, "carol": 5})
        self.assertEqual(res["total"], 30)

    def test_last_limits_history(self):
        res = self.report("--last", "2")
        self.assertEqual([r["name"] for r in res["rows"]], ["S2", "S3"])
        self.assertIn("Only 2 period(s)", " ".join(res["notes"]))

    def test_count_unit_when_an_issue_has_no_points(self):
        issues = ISSUES + [issue("o", "S2", None, "2026-01-26T10:00:00Z", assignees=["carol"])]
        res = self.report(issues=issues)
        self.assertEqual(res["unit"], "count")
        self.assertEqual([r["velocity"] for r in res["rows"]], [2, 3, 2])
        self.assertIn("1 issue(s) have no points", res["notes"][0])

    def test_plane_note_points_to_the_estimate_not_labels(self):
        # Regression: the note used to tell Plane users their issues had "no points label",
        # but Plane story points come from the Estimate field.
        issues = ISSUES + [issue("o", "S2", None, "2026-01-26T10:00:00Z")]
        note = self.report(issues=issues)["notes"][0]
        self.assertIn("Estimate", note)
        self.assertNotIn("label", note)
        note = self.report(issues=issues, target="gitlab")["notes"][0]
        self.assertIn("points::N", note)

    def test_unit_points_counts_only_pointed_work(self):
        issues = ISSUES + [issue("o", "S2", None, "2026-01-26T10:00:00Z")]
        res = self.report("--unit", "points", issues=issues)
        self.assertEqual([r["velocity"] for r in res["rows"]], [8, 10, 12])
        self.assertEqual(res["rows"][1]["unpointed"], 1)

    def test_no_finished_period_falls_back_to_weekly(self):
        # Regression: the fallback kept the unit chosen from the (empty) finished periods, so it
        # always counted issues, while `--by week` on the same data used points.
        periods = [p for p in PERIODS if p["name"] in ("S4", "S5")]
        res = self.report("--weeks", "3", periods=periods)
        self.assertEqual(res["basis"], "week")
        self.assertEqual(res["unit"], "points")
        self.assertEqual([r["start"] for r in res["rows"]], ["2026-01-26", "2026-02-02", "2026-02-09"])
        self.assertEqual([r["velocity"] for r in res["rows"]], [5, 0, 12])
        self.assertIn("weekly throughput instead", " ".join(res["notes"]))
        self.assertEqual(res, {**self.report("--weeks", "3", "--by", "week", periods=periods), "notes": res["notes"]})
        self.assertNotIn("next", res)
        self.assertNotIn("current", res)

    def test_by_week(self):
        res = self.report("--by", "week", "--weeks", "2")
        self.assertEqual(res["basis"], "week")
        self.assertEqual(len(res["rows"]), 2)

    def test_empty_project(self):
        res = self.report(periods=[], issues=[])
        self.assertEqual(res["basis"], "week")
        self.assertEqual(res["unit"], "count")
        self.assertEqual(len(res["rows"]), 8)
        self.assertEqual(res["summary"]["mean"], 0)

    def test_markdown_report(self):
        out = self.run_report()
        self.assertIn("# Team velocity — Demo (DEMO)", out)
        self.assertIn("## Summary", out)
        self.assertIn("Stability: **stable** · trend: **rising**", out)
        self.assertIn("plan about **10 points**", out)
        self.assertIn("**3 over** the recommendation", out)
        self.assertIn("**off track** (about 2 points short).", out)
        self.assertIn("| @alice |", out)

    def test_markdown_without_rows(self):
        res = {"project": "P", "kind": "cycle", "unit": "count", "basis": "period", "today": "2026-02-22",
               "rows": [], "notes": []}
        self.assertIn("No finished cycle", vel.render(res))

    def test_periods_command_json(self):
        with mock.patch.object(vel, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("periods", "--target", "plane", "--project", "DEMO",
                                                  "--today", "2026-02-22", "--format", "json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual([(p["name"], p["status"]) for p in res["periods"]],
                         [("S1", "finished"), ("S2", "finished"), ("S3", "finished"),
                          ("S4", "current"), ("S5", "upcoming")])

    def test_projects_command_json(self):
        with mock.patch.object(vel, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["path"], "DEMO")
        self.assertNotIn("plane-secret-456", out)


if __name__ == "__main__":
    unittest.main()
