"""CLI of stale.py: config handling, argparse defaults, report and periods end to end, JSON output."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load

st = load("stale-issues", "stale.py")

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
                        st.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


def issue(key, updated, period, started=False, blocked=False, stale_mark=False, points=None, assignees=()):
    return st.new_issue(key=key, title=f"Title {key}", url=f"https://example.com/{key}", updated=updated,
                        status="open", started=started, blocked=blocked, stale_mark=stale_mark, points=points,
                        assignees=list(assignees), period=period)


PERIODS = [
    {"name": "Sprint 6", "start": "2026-03-15", "end": "2026-03-28"},
    {"name": "Sprint 5", "start": "2026-03-01", "end": "2026-03-14"},
    {"name": "Someday", "start": None, "end": None},
]
ISSUES = [
    issue("#1", "2026-02-20T10:00:00Z", "Sprint 5", started=True, points=5, assignees=["dev-a"]),
    issue("#2", "2026-01-01T00:00:00Z", "Sprint 5", points=3),                    # not started, 10 d in sprint
    issue("#3", "2026-02-01T00:00:00Z", "Sprint 5", blocked=True, points=2, assignees=["dev-a", "dev-b"]),
    issue("#4", "2026-03-10T08:00:00Z", "Sprint 5", stale_mark=True, points=1),
    issue("#5", "2026-03-05T00:00:00Z", "Sprint 5", started=True, points=8, assignees=["dev-b"]),
    issue("#6", "2026-01-01T00:00:00Z", "Sprint 6", points=1),                    # upcoming sprint: not stale
    issue("#7", "2026-01-01T00:00:00Z", None, points=2),                          # backlog, no milestone
]


def fake_source(periods, issues, kind="cycle"):
    class Fake:
        calls = []

        def __init__(self, *args):
            self.args, self.kind, self.name = args, kind, "Demo (DEMO)"

        def my_projects(self):
            return [{"path": "DEMO", "name": "Demo", "url": "https://plane.example.com/demo-ws/projects/p-1/issues"}]

        def periods(self, today):
            return [dict(p) for p in periods]

        def issues(self, period):
            Fake.calls.append(period)
            return [dict(i) for i in issues if period is None or i["period"] == period]
    return Fake


class ConfigTest(unittest.TestCase):
    def test_options_replace_url_and_workspace(self):
        with mock.patch.object(st, "Plane", fake_source(PERIODS, ISSUES)):
            code, _, err = Run(self, {"PLANE_API_KEY": "plane-secret-456"})(
                "projects", "--target", "plane", "--plane-url", "https://p.example.com", "--workspace", "ws")
        self.assertEqual(code, 0, err)

    def test_api_error_ends_with_a_message_not_a_traceback(self):
        def boom(*args, **kw):
            raise st.ApiError(401, '{"detail": "invalid token"}', "GET",
                              "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100")

        with mock.patch.object(st, "http", side_effect=boom), mock.patch.object(st.time, "sleep"):
            code, out, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("401", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)

    def test_gitlab_api_error_ends_with_a_message(self):
        with mock.patch.object(st, "http", side_effect=st.ApiError(403, "forbidden", "POST",
                                                                   "https://gitlab.example.com/api/graphql")):
            code, out, err = Run(self, GITLAB_ENV)("periods", "--target", "gitlab", "--project", "g/p")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("403", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)


class ArgparseTest(unittest.TestCase):
    def test_defaults(self):
        seen, parsed = {}, {}

        def stop(**kw):
            seen.update(kw)
            raise SystemExit(0)

        real = st.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        with mock.patch.object(st, "require_config", side_effect=stop), \
                mock.patch.object(st.argparse.ArgumentParser, "parse_args", capture):
            Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertEqual(parsed["stale_days"], 14)
        self.assertEqual(parsed["format"], "md")
        self.assertIsNone(parsed["today"])
        self.assertIsNone(parsed["period"])
        self.assertFalse(parsed["all"])
        self.assertEqual(parsed["rate"], 60)
        self.assertEqual(parsed["plane_url"], "https://plane.example.com")
        self.assertEqual(parsed["workspace"], "demo-ws")
        self.assertEqual((seen["plane"], seen["gitlab"]), (True, False))

    def test_invalid_choice_is_rejected(self):
        code, _, err = Run(self, PLANE_ENV)("report", "--target", "jira")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_period_and_all_are_exclusive(self):
        code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "D", "--all", "--period", "S")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)

    def test_report_needs_a_project(self):
        with mock.patch.object(st, "Plane", fake_source(PERIODS, ISSUES)):
            code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane")
        self.assertNotEqual(code, 0)
        self.assertIn("needs --project", err)

    def test_report_needs_a_period_or_all(self):
        with mock.patch.object(st, "Plane", fake_source(PERIODS, ISSUES)):
            code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertNotEqual(code, 0)
        self.assertIn("--period", err)
        self.assertIn("--all", err)


class ReportTest(unittest.TestCase):
    def run_cli(self, *argv, target="plane", periods=PERIODS, issues=ISSUES):
        env = PLANE_ENV if target == "plane" else GITLAB_ENV
        cls, kind = ("Plane", "cycle") if target == "plane" else ("GitLab", "milestone")
        fake = fake_source(periods, issues, kind)
        with mock.patch.object(st, cls, fake):
            code, out, err = Run(self, env)(argv[0], "--target", target, "--project", "DEMO",
                                            "--today", "2026-03-10", *argv[1:])
        self.assertEqual(code, 0, err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)
        return out, fake

    def report(self, *extra, **kw):
        return json.loads(self.run_cli("report", "--format", "json", *extra, **kw)[0])

    def test_json_report_for_a_period(self):
        res = self.report("--period", "sprint 5")
        self.assertEqual(res["period"], {"name": "Sprint 5", "start": "2026-03-01", "end": "2026-03-14"})
        self.assertEqual((res["project"], res["kind"], res["today"], res["stale_days"]),
                         ("Demo (DEMO)", "cycle", "2026-03-10", 14))
        self.assertEqual((res["open"], res["stale_count"], res["unit"], res["open_size"], res["stale_size"]),
                         (5, 3, "points", 19, 8))
        self.assertEqual([(i["key"], i["kind"]) for i in res["stale"]],
                         [("#1", "stalled"), ("#3", "blocked"), ("#4", "marked stale")])
        self.assertEqual(res["per_person"], [["dev-a", 2], ["dev-b", 1], ["(unassigned)", 1]])
        self.assertEqual([i["key"] for i in res["aging"]], ["#2"])
        self.assertEqual(res["period_left"], 4)
        self.assertTrue(all(i["next_step"] for i in res["stale"]))

    def test_whole_project(self):
        res = self.report("--all")
        self.assertIsNone(res["period"])
        self.assertEqual(res["open"], 7)
        # without a period the backlog time counts, so #2, #6 and #7 are stale too
        self.assertEqual(sorted(i["key"] for i in res["stale"]), ["#1", "#2", "#3", "#4", "#6", "#7"])
        self.assertEqual(res["by_kind"]["not started"], 3)

    def test_custom_threshold(self):
        res = self.report("--period", "Sprint 5", "--stale-days", "5")
        self.assertEqual(sorted(i["key"] for i in res["stale"]), ["#1", "#2", "#3", "#4", "#5"])
        self.assertEqual(res["stale_days"], 5)

    def test_unknown_period_has_no_dates(self):
        res = self.report("--period", "Closed sprint", target="gitlab")
        self.assertEqual(res["period"], {"name": "Closed sprint", "start": None, "end": None})
        self.assertEqual((res["open"], res["kind"]), (0, "milestone"))

    def test_markdown_report(self):
        out, _ = self.run_cli("report", "--period", "Sprint 5")
        self.assertIn("# Stale issues — Demo (DEMO)", out)
        self.assertIn("cycle **Sprint 5** (2026-03-01 → 2026-03-14) · today 2026-03-10", out)
        self.assertIn("**3 of 5 open issue(s) are stale**: 8 points (42% of the open points).", out)
        self.assertIn("| [#1](https://example.com/#1) | Title #1 | stalled |", out)
        self.assertIn("| @dev-a | 2 |", out)

    def test_periods_listing_with_stale_counts_plane(self):
        out, fake = self.run_cli("periods", "--format", "json")
        res = json.loads(out)
        self.assertEqual([p["name"] for p in res["periods"]], ["Sprint 5", "Sprint 6", "Someday"])  # by start date
        self.assertEqual([(p["open"], p["stale"]) for p in res["periods"]], [(5, 3), (1, 0), (0, 0)])
        self.assertEqual(res["whole"], {"open": 7, "stale": 6})
        self.assertEqual(res["kind"], "cycle")
        self.assertEqual(fake.calls, [None, "Sprint 5", "Sprint 6", "Someday"])  # Plane asks per cycle

    def test_periods_listing_with_stale_counts_gitlab(self):
        out, fake = self.run_cli("periods", "--format", "json", target="gitlab")
        res = json.loads(out)
        self.assertEqual([(p["name"], p["open"], p["stale"]) for p in res["periods"]],
                         [("Sprint 5", 5, 3), ("Sprint 6", 1, 0), ("Someday", 0, 0)])
        self.assertEqual(fake.calls, [None])  # GitLab loads the project once and splits by milestone

    def test_periods_markdown(self):
        out, _ = self.run_cli("periods", "--stale-days", "7")
        self.assertIn("# Open cycles — Demo (DEMO)", out)
        self.assertIn("| 1 | Sprint 5 | 2026-03-01 | 2026-03-14 | 5 | 4 |", out)

    def test_projects_command(self):
        with mock.patch.object(st, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["path"], "DEMO")
        with mock.patch.object(st, "Plane", fake_source(PERIODS, ISSUES)):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane")
        self.assertIn("# Your Plane projects", out)
        self.assertIn("| 1 | [DEMO](https://plane.example.com/demo-ws/projects/p-1/issues) | Demo |", out)

    def test_empty_project(self):
        res = self.report("--all", periods=[], issues=[])
        self.assertEqual((res["open"], res["stale_count"], res["unit"]), (0, 0, "issues"))
        out, _ = self.run_cli("periods", periods=[], issues=[])
        self.assertIn("| — | whole project |  |  | 0 | 0 |", out)

    def test_never_writes(self):
        out, fake = self.run_cli("report", "--all")
        self.assertFalse(hasattr(fake, "create") or hasattr(fake, "update"))
        for name in ("post", "put", "patch", "delete"):
            self.assertFalse(hasattr(st.Plane, name) or hasattr(st.GitLab, name))


if __name__ == "__main__":
    unittest.main()
