"""CLI of commitment.py: config handling, argparse defaults, the report end to end, JSON and markdown output."""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load

cd = load("commitment-delivered", "commitment.py")

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
                        cd.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


def day(text, hour=10):
    return f"{text}T{hour:02d}:00:00Z"


def issue(key, period, points, closed=None, cancelled=False, assignees=(), created="2026-01-01",
          events=None, initial=None):
    if events is not None:
        events = [(cd.parse_dt(day(d)), p) for d, p in events]
    return cd.new_issue(key=key, title=f"Issue {key}", url=f"https://plane.example.com/demo-ws/i/{key}",
                        period=period, points=points, closed=day(closed) if closed else None, cancelled=cancelled,
                        assignees=list(assignees), created=day(created, 0), updated=day(created, 0),
                        events=events, initial=initial)


PERIODS = [
    {"name": "S3", "start": "2026-02-02", "end": "2026-02-15", "closed": True},
    {"name": "S1", "start": "2026-01-05", "end": "2026-01-18", "closed": True},
    {"name": "S2", "start": "2026-01-19", "end": "2026-02-01", "closed": False},  # finished by date
    {"name": "S4", "start": "2026-02-16", "end": "2026-03-01", "closed": False},  # current on 2026-02-22
    {"name": "S5", "start": "2026-03-02", "end": "2026-03-15", "closed": False},  # upcoming
    {"name": "Backlog", "start": None, "end": None, "closed": False},
]
# Hand-computed (points, cutoff = end of the start day):
#   S1: committed a3 b2 c1 d2 f2 = 10, delivered a f = 5 (50%), removed c 1, cancelled d 2, carried b 2,
#       added e 1 (delivered), total 6 (60%), scope change 0.
#   S2: committed c1 g4 i5 = 10, delivered c g = 5 (50%), carried i 5, added h 3 (delivered), total 8, scope +30%.
#   S3: committed j6 k4 = 10, delivered 10 (100%), added l 2 (delivered), total 12, scope +20%.
#   S4 (current): committed m3 n5 = 8, delivered m 3, still open n, added o 2.
#   S5 (upcoming): p8 + q5 = 13 (r is cancelled).
ISSUES = [
    issue("a", "S1", 3.0, "2026-01-10", assignees=["alice"]),
    issue("b", "S1", 2.0, "2026-01-20", assignees=["bob"]),
    issue("c", "S2", 1.0, "2026-01-25", assignees=["carol"], events=[("2026-01-02", "S1"), ("2026-01-10", "S2")]),
    issue("d", "S1", 2.0, "2026-01-12", cancelled=True),
    issue("e", "S1", 1.0, "2026-01-15", assignees=["alice"], created="2026-01-08"),
    issue("f", "S1", 2.0, "2026-01-17", assignees=["alice", "bob"], events=[("2026-01-02", "S1")]),
    issue("g", "S2", 4.0, "2026-01-30", assignees=["carol"]),
    issue("h", "S2", 3.0, "2026-01-28", assignees=["bob"], events=[("2026-01-02", "S3"), ("2026-01-22", "S2")]),
    issue("i", "S2", 5.0, assignees=["dave"]),
    issue("j", "S3", 6.0, "2026-02-10", assignees=["alice"]),
    issue("k", "S3", 4.0, "2026-02-14", assignees=["bob"]),
    issue("l", "S3", 2.0, "2026-02-12", created="2026-02-03"),
    issue("m", "S4", 3.0, "2026-02-18", assignees=["alice"], created="2026-02-01"),
    issue("n", "S4", 5.0, created="2026-02-01"),
    issue("o", "S4", 2.0, created="2026-02-20"),
    issue("p", "S5", 8.0),
    issue("q", "S5", 5.0),
    issue("r", "S5", 3.0, "2026-02-20", cancelled=True),
    issue("s", None, 1.0),
]


def fake_source(periods, issues, kind="cycle", seen=None):
    class Fake:
        def __init__(self, *args):
            self.args, self.kind, self.name = args, kind, "Demo (DEMO)"

        def my_projects(self):
            return [{"path": "DEMO", "name": "Demo", "url": "https://plane.example.com/demo-ws/projects/p-1/issues"}]

        def periods(self):
            return [dict(p) for p in periods]

        def issues(self, since):
            if seen is not None:
                seen["since"] = since
            return [dict(i) for i in issues]
    return Fake


class ConfigTest(unittest.TestCase):
    def test_options_replace_url_and_workspace(self):
        with mock.patch.object(cd, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, {"PLANE_API_KEY": "plane-secret-456"})(
                "projects", "--target", "plane", "--plane-url", "https://p.example.com", "--workspace", "ws")
        self.assertEqual(code, 0, err)

    def test_report_needs_a_project(self):
        with mock.patch.object(cd, "Plane", fake_source(PERIODS, ISSUES)):
            code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane")
        self.assertNotEqual(code, 0)
        self.assertIn("needs --project", err)

    def test_api_error_ends_with_a_message_not_a_traceback(self):
        def boom(*args, **kw):
            raise cd.ApiError(401, '{"detail": "invalid token"}', "GET",
                              "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100")

        with mock.patch.object(cd, "http", side_effect=boom), mock.patch("time.sleep"):
            code, out, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("401", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)

    def test_gitlab_api_error_ends_with_a_message(self):
        def boom(*args, **kw):
            raise cd.ApiError(403, '{"message": "403 Forbidden"}', "POST", "https://gitlab.example.com/api/graphql")

        with mock.patch.object(cd, "http", side_effect=boom):
            code, out, err = Run(self, GITLAB_ENV)("report", "--target", "gitlab", "--project", "g/p")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("403", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)


class ArgparseTest(unittest.TestCase):
    def test_defaults(self):
        parsed, seen = {}, {}
        real = cd.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        def stop(**kw):
            seen.update(kw)
            raise SystemExit(0)

        with mock.patch.object(cd, "require_config", side_effect=stop), \
                mock.patch.object(cd.argparse.ArgumentParser, "parse_args", capture):
            Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertEqual(parsed["last"], 6)
        self.assertEqual(parsed["unit"], "auto")
        self.assertEqual(parsed["grace_days"], 0)
        self.assertEqual(parsed["format"], "md")
        self.assertEqual(parsed["rate"], 60)
        self.assertIsNone(parsed["period"])
        self.assertIsNone(parsed["today"])
        self.assertEqual(parsed["plane_url"], "https://plane.example.com")
        self.assertEqual(parsed["workspace"], "demo-ws")
        self.assertEqual(seen, {"gitlab": False, "plane": True, "gitlab_url": None,
                                "plane_url": "https://plane.example.com", "workspace": "demo-ws"})

    def test_invalid_choices_are_rejected(self):
        for argv in (("report", "--target", "jira"), ("report", "--target", "plane", "--unit", "hours"),
                     ("report", "--target", "plane", "--format", "csv"), ("delete", "--target", "plane")):
            code, _, err = Run(self, PLANE_ENV)(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("invalid choice", err)


class ReportTest(unittest.TestCase):
    def run_report(self, *extra, periods=PERIODS, issues=ISSUES, target="plane", seen=None):
        env = PLANE_ENV if target == "plane" else GITLAB_ENV
        cls, kind = ("Plane", "cycle") if target == "plane" else ("GitLab", "milestone")
        with mock.patch.object(cd, cls, fake_source(periods, issues, kind, seen)):
            code, out, err = Run(self, env)("report", "--target", target, "--project", "DEMO",
                                            "--today", "2026-02-22", *extra)
        self.assertEqual(code, 0, err)
        return out

    def report(self, *extra, **kw):
        return json.loads(self.run_report("--format", "json", *extra, **kw))

    def rows(self, res):
        return {r["name"]: r for r in res["rows"]}

    def test_json_rows(self):
        res = self.report()
        self.assertEqual((res["unit"], res["kind"], res["today"], res["grace_days"]),
                         ("points", "cycle", "2026-02-22", 0))
        self.assertEqual([r["name"] for r in res["rows"]], ["S1", "S2", "S3", "S4"])
        cols = ("committed", "committed_issues", "delivered", "say_do", "removed", "cancelled", "carried", "added",
                "added_issues", "added_delivered", "total_delivered", "vs_commitment", "scope_change", "status")
        got = {r["name"]: tuple(r[c] for c in cols) for r in res["rows"]}
        self.assertEqual(got["S1"], (10, 5, 5, 0.5, 1, 2, 2, 1, 1, 1, 6, 0.6, 0.0, "finished"))
        self.assertEqual(got["S2"][:12], (10, 3, 5, 0.5, 0, 0, 5, 3, 1, 3, 8, 0.8))
        self.assertAlmostEqual(got["S2"][12], 0.3)
        self.assertEqual(got["S3"][:12], (10, 2, 10, 1.0, 0, 0, 0, 2, 1, 2, 12, 1.2))
        self.assertAlmostEqual(got["S3"][12], 0.2)
        self.assertEqual(got["S4"], (8, 2, 3, 0.375, 0, 0, 5, 2, 1, 0, 3, 0.375, 0.25, "current"))
        s1 = self.rows(res)["S1"]
        self.assertEqual([i["key"] for i in s1["carried_items"]], ["b"])
        self.assertEqual([i["key"] for i in s1["removed_items"]], ["c"])
        self.assertEqual([i["key"] for i in s1["cancelled_items"]], ["d"])

    def test_summary(self):
        s = self.report()["summary"]
        self.assertEqual((s["n"], s["median"], s["min"], s["max"], s["latest"], s["latest_name"]),
                         (3, 0.5, 0.5, 1.0, 1.0, "S3"))
        self.assertAlmostEqual(s["mean"], 2 / 3)
        self.assertAlmostEqual(s["last3"], 2 / 3)
        self.assertAlmostEqual(s["trend"], 0.25)                   # +25 pp per period
        self.assertEqual(s["trend_word"], "rising")
        self.assertAlmostEqual(s["pooled"], 20 / 30)
        self.assertEqual((s["vs_commitment"], s["scope_change"], s["median_delivered"]), (0.8, 0.2, 5))
        self.assertEqual((s["rating_basis"], s["reliability"], s["recent_drop"]), (0.5, "overcommits", False))

    def test_per_member_over_finished_periods(self):
        members = self.report()["members"]
        self.assertEqual(members, [["alice", [10, 10]], ["bob", [7, 5]], ["carol", [6, 5]], ["dave", [5, 0]],
                                   ["(unassigned)", [2, 0]]])
        self.assertEqual(sum(c for _, (c, _) in members), 30)

    def test_next_period_overcommit_check(self):
        nxt = self.report()["next"]
        self.assertEqual(nxt, {"name": "S5", "scope": 13, "median_delivered": 5})
        out = self.run_report()
        self.assertIn("**S5** holds 13 points today", out)
        self.assertIn("committing 13 is **260%** of that", out)

    def test_notes(self):
        notes = " ".join(self.report()["notes"])
        self.assertIn("current period is still running", notes)
        self.assertIn("Points are read as they are today", notes)
        self.assertNotIn("No milestone/cycle history", notes)
        self.assertNotIn("finished period(s)", notes)

    def test_history_is_read_from_the_first_analyzed_period(self):
        seen = {}
        self.report("--last", "2", seen=seen)
        self.assertEqual(seen["since"], dt.datetime(2026, 1, 19, tzinfo=dt.timezone.utc))

    def test_grace_days(self):
        res = self.report("--grace-days", "1")
        s3 = self.rows(res)["S3"]  # l (created 02-03) is now part of the commitment
        self.assertEqual((s3["committed"], s3["delivered"], s3["added"], s3["say_do"]), (12, 12, 0, 1.0))
        self.assertEqual(self.rows(res)["S1"]["committed"], 10)
        self.assertEqual(res["grace_days"], 1)
        self.assertIn("+ 1 day(s)", self.run_report("--grace-days", "1"))

    def test_last_and_period_selection(self):
        res = self.report("--last", "2")
        self.assertEqual([r["name"] for r in res["rows"]], ["S2", "S3", "S4"])
        self.assertIn("Only 2 finished period(s)", " ".join(res["notes"]))
        res = self.report("--period", "s1", "--period", "S5", "--period", "Nope")
        self.assertEqual([r["name"] for r in res["rows"]], ["S1"])
        self.assertNotIn("next", res)  # --period skips the next-period check
        self.assertIn("Not found (or without dates): nope.", res["notes"])

    def test_count_when_an_issue_has_no_points(self):
        issues = ISSUES + [issue("t", "S2", None, "2026-01-26", assignees=["carol"])]
        res = self.report(issues=issues)
        self.assertEqual(res["unit"], "count")
        s2 = self.rows(res)["S2"]
        self.assertEqual((s2["committed"], s2["delivered"]), (4, 3))
        self.assertTrue(res["notes"][0].startswith("1 issue(s) have no points"))

    def test_plane_note_points_to_the_estimate_not_labels(self):
        # Regression: Plane users were told their issues had "no points label",
        # but Plane story points come from the Estimate field.
        issues = ISSUES + [issue("t", "S2", None, "2026-01-26")]
        note = self.report(issues=issues)["notes"][0]
        self.assertIn("Estimate", note)
        self.assertNotIn("label", note)
        note = self.report(issues=issues, target="gitlab")["notes"][0]
        self.assertIn("points::N", note)

    def test_unit_points_counts_only_pointed_work(self):
        issues = ISSUES + [issue("t", "S2", None, "2026-01-26")]
        res = self.report("--unit", "points", issues=issues)
        s2 = self.rows(res)["S2"]
        self.assertEqual((s2["committed"], s2["committed_issues"], s2["delivered"]), (10, 4, 5))

    def test_unit_count(self):
        res = self.report("--unit", "count")
        s1 = self.rows(res)["S1"]
        self.assertEqual((res["unit"], s1["committed"], s1["delivered"], s1["say_do"]), ("count", 5, 2, 0.4))

    def test_no_history_note(self):
        issues = [dict(i, events=None) for i in ISSUES]
        self.assertIn("No milestone/cycle history", " ".join(self.report(issues=issues)["notes"]))

    def test_period_without_commitment(self):
        periods = PERIODS + [{"name": "S0", "start": "2025-12-01", "end": "2025-12-14", "closed": True}]
        issues = ISSUES + [issue("z", "S0", 2.0, "2025-12-10", created="2025-12-05")]
        res = self.report(issues=issues, periods=periods)
        s0 = self.rows(res)["S0"]
        self.assertIsNone(s0["say_do"])
        self.assertEqual((s0["added"], s0["total_delivered"]), (2, 2))
        self.assertIn("No commitment at the start of: S0", " ".join(res["notes"]))
        self.assertEqual(res["summary"]["n"], 3)

    def test_empty_project(self):
        res = self.report(periods=[], issues=[])
        self.assertEqual((res["rows"], res["summary"], res["unit"]), ([], None, "count"))
        self.assertNotIn("next", res)
        self.assertIn("No cycle with dates", self.run_report(periods=[], issues=[]))

    def test_markdown_report(self):
        out = self.run_report()
        self.assertIn("# Commitment x delivered — Demo (DEMO)", out)
        self.assertIn("Unit: **points** · commitment cutoff: end of the start day · today 2026-02-22", out)
        self.assertIn("| S1 | 2026-01-05 → 2026-01-18 | finished | 10 (5) | 5 | **50%** | 1 | 2 | 2 | 1 (1) | 1 | 6 "
                      "| 60% | +0% |", out)
        self.assertIn("## Summary (finished periods)", out)
        self.assertIn("| 3 | 67% | 50% | 50% | 100% | 67% | 100% | 67% | +25 pp | 80% | +20% |", out)
        self.assertIn("Reliability: **overcommits** (rated on 50%", out)
        self.assertIn("trend: **rising**", out)
        self.assertNotIn("Recent drop", out)
        self.assertIn("| @alice | 10 | 10 | 100% |", out)
        self.assertIn("| (unassigned) | 2 | 0 | 0% |", out)
        self.assertIn("- carried over: [b](https://plane.example.com/demo-ws/i/b) Issue b (2)", out)
        self.assertIn("- cancelled: [d](https://plane.example.com/demo-ws/i/d) Issue d (2)", out)
        self.assertIn("### S4 — not delivered from the commitment", out)
        self.assertIn("- still open: [n](https://plane.example.com/demo-ws/i/n) Issue n (5)", out)

    def test_markdown_flags_a_recent_drop(self):
        issues = ISSUES + [issue("u", "S3", 40.0, assignees=["erin"])]  # S3 commits 50, delivers 10
        s = self.report(issues=issues)["summary"]
        self.assertAlmostEqual(s["latest"], 0.2)
        self.assertTrue(s["recent_drop"])  # 20% < 70% and 30 pp under the median of 50%
        out = self.run_report(issues=issues)
        self.assertIn("**Recent drop:** S3 kept only 20% of its commitment, 30 pp under the median of 50%. "
                      "The rating was lowered one level.", out)

    def test_periods_command_json(self):
        with mock.patch.object(cd, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("periods", "--target", "plane", "--project", "DEMO",
                                                  "--today", "2026-02-22", "--format", "json")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual(res["kind"], "cycle")
        self.assertEqual([(p["name"], p["status"]) for p in res["periods"]],
                         [("S1", "finished"), ("S2", "finished"), ("S3", "finished"),
                          ("S4", "current"), ("S5", "upcoming")])

    def test_periods_command_markdown(self):
        with mock.patch.object(cd, "GitLab", fake_source(PERIODS, ISSUES, "milestone")):
            code, out, err = Run(self, GITLAB_ENV)("periods", "--target", "gitlab", "--project", "g/p",
                                                   "--today", "2026-02-22")
        self.assertEqual(code, 0, err)
        self.assertIn("# Milestones — Demo (DEMO)", out)
        self.assertIn("| 4 | S4 | 2026-02-16 | 2026-03-01 | current |", out)

    def test_projects_command(self):
        with mock.patch.object(cd, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane", "--format", "json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)[0]["path"], "DEMO")
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane")
        self.assertIn("# Your Plane projects", out)
        self.assertIn("| 1 | [DEMO](https://plane.example.com/demo-ws/projects/p-1/issues) | Demo |", out)
        self.assertNotIn("plane-secret-456", out)


if __name__ == "__main__":
    unittest.main()
