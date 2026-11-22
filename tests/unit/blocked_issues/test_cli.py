"""CLI of blocked.py: config handling, argparse, scope (milestone/cycle vs whole project), JSON output."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load
from tests.unit.blocked_issues.test_loaders import PlaneRouter, fixture, plane_routes

blk = load("blocked-issues", "blocked.py")

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
                        blk.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- fake GitLab transport
def gitlab_transport():
    """Patches GitLab.gql/rest/rest_pages with fixture answers; returns the patchers and the call log."""
    pages = [fixture("gitlab_work_items_page1.json")["data"], fixture("gitlab_work_items_page2.json")["data"]]
    milestones = fixture("gitlab_milestones.json")["data"]
    link_1_2 = [{"iid": 2, "project_id": 100, "issue_link_id": 9, "link_type": "blocks",
                 "references": {"full": "demo/app#2"}}]
    detail_9 = {"source_issue": {"iid": 2, "project_id": 100}, "target_issue": {"iid": 1, "project_id": 100}}
    link_2_1 = [{"iid": 1, "project_id": 100, "issue_link_id": 9, "link_type": "blocks",
                 "references": {"full": "demo/app#1"}}]
    rest = {
        "/projects/demo%2Fapp": {"id": 100},
        "/projects/100/issues/1/links": link_1_2, "/projects/100/issues/1/links/9": detail_9,
        "/projects/100/issues/2/links": link_2_1, "/projects/100/issues/2/links/9": detail_9,
        "/projects/100/issues/4/links": fixture("gitlab_links_4.json"),
        "/projects/100/issues/4/links/55": fixture("gitlab_link_detail_55.json"),
        "/projects/100/issues/1/notes": fixture("gitlab_notes_1.json"),
        "/projects/100/issues/1/resource_label_events": fixture("gitlab_label_events_1.json"),
        "/projects/100/issues/4/notes": [],
        "/projects": [{"path_with_namespace": "demo/app", "name": "Demo App",
                       "web_url": "https://gitlab.example.com/demo/app", "open_issues_count": 5}],
    }
    log = {"gql": [], "rest": []}
    queue = []

    def gql(query, variables=None):
        log["gql"].append(dict(variables or {}))
        if "milestones(" in query:
            return milestones
        if "workItem(id:" in query or "workItems(iid:" in query:
            return {"workItem": None, "project": None}
        if variables.get("after") is None:
            queue[:] = list(pages)
        return queue.pop(0)

    def rest_call(path, params=None):
        log["rest"].append(path)
        if path not in rest:
            raise blk.ApiError(404, '{"message": "404 Not found"}', "GET", path)
        return rest[path]

    patches = [mock.patch.object(blk.GitLab, "graphql", side_effect=gql),
               mock.patch.object(blk.GitLab, "rest", side_effect=rest_call),
               mock.patch.object(blk.GitLab, "rest_pages", side_effect=rest_call)]
    return patches, log


class ConfigTest(unittest.TestCase):
    def test_api_error_ends_with_a_message_not_a_traceback(self):
        def boom(*args, **kw):
            raise blk.ApiError(401, '{"detail": "invalid token"}', "GET",
                               "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100")

        with mock.patch.object(blk, "http", side_effect=boom), mock.patch("time.sleep"):
            code, out, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("401", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)


class ProjectsTableTest(unittest.TestCase):
    def test_lists_the_projects_with_their_open_issues(self):
        patches, _ = gitlab_transport()
        with patches[0], patches[1], patches[2]:
            code, out, err = Run(self, GITLAB_ENV)("projects", "--target", "gitlab")
        self.assertEqual(code, 0, err)
        self.assertIn("| 1 | [demo/app](https://gitlab.example.com/demo/app) | Demo App | 5 | — |", out)


class ArgparseTest(unittest.TestCase):
    def test_defaults(self):
        parsed = {}
        real = blk.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        with mock.patch.object(blk, "require_config", side_effect=SystemExit(0)) as rc, \
                mock.patch.object(blk.argparse.ArgumentParser, "parse_args", capture):
            Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertEqual(parsed["stale_days"], 30)
        self.assertEqual(parsed["format"], "md")
        self.assertEqual(parsed["rate"], 60)
        self.assertFalse(parsed["deep"])
        self.assertFalse(parsed["all"])
        self.assertIsNone(parsed["period"])
        self.assertIsNone(parsed["today"])
        self.assertEqual(parsed["plane_url"], "https://plane.example.com")
        self.assertEqual(parsed["workspace"], "demo-ws")
        self.assertEqual(rc.call_args.kwargs["scope"], "read_api")

    def test_invalid_choice_and_exclusive_scope(self):
        code, _, err = Run(self, PLANE_ENV)("report", "--target", "jira")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO",
                                            "--period", "Cycle 2", "--all")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", err)

    def test_periods_and_report_need_a_project(self):
        for cmd in ("periods", "report"):
            code, _, err = Run(self, PLANE_ENV)(cmd, "--target", "plane")
            self.assertNotEqual(code, 0)
            self.assertIn("needs --project", err)

    def test_report_needs_a_scope(self):
        with mock.patch.object(blk.Plane, "request", side_effect=PlaneRouter(plane_routes(), blk.ApiError)):
            code, _, err = Run(self, PLANE_ENV)("report", "--target", "plane", "--project", "DEMO")
        self.assertNotEqual(code, 0)
        self.assertIn("report needs --period <milestone/cycle> or --all", err)


class PlaneReportTest(unittest.TestCase):
    def run_cli(self, *argv, routes=None):
        router = PlaneRouter(routes or plane_routes(), blk.ApiError)
        with mock.patch.object(blk.Plane, "request", side_effect=router):
            code, out, err = Run(self, PLANE_ENV)(*argv)
        self.assertEqual(code, 0, err)
        return out, router

    def report(self, *scope):
        out, router = self.run_cli("report", "--target", "plane", "--project", "DEMO", "--today", "2026-03-01",
                                   "--format", "json", *scope)
        return json.loads(out), router

    def test_cycle_report_json(self):
        res, router = self.report("--period", "Cycle 2")
        self.assertEqual((res["target"], res["project"], res["scope"], res["today"], res["stale_days"]),
                         ("plane", "Demo (DEMO)", "Cycle 2", "2026-03-01", 30))
        self.assertEqual([r["key"] for r in res["issues"]], ["DEMO-1", "DEMO-6"])
        self.assertEqual(res["stale_excluded"], [{
            "key": "DEMO-3", "title": "Old blocked task", "why": "no activity for 59 days",
            "url": "https://plane.example.com/demo-ws/projects/p-1/issues/i-3"}])
        one, six = res["issues"]
        self.assertEqual(one["causes"], ["dependency", "parent blocked"])
        self.assertEqual((one["blocked_since"], one["blocked_since_source"]), ("2026-02-19", "state changed"))
        self.assertAlmostEqual(one["blocked_days"], 10 + (23 * 60 + 59 - 8 * 60) / 1440)
        self.assertEqual([b["key"] for b in one["blocked_by"]], ["DEMO-2"])
        self.assertEqual([b["key"] for b in one["related"]], ["DEMO-4"])
        self.assertEqual(one["parent"]["key"], "DEMO-6")
        self.assertEqual(one["external_refs"], ["OPS-9"])
        self.assertEqual(one["mentioned_not_linked"], [])
        self.assertTrue(one["next_steps"][0].startswith("Follow up on DEMO-2 (In Progress, assigned to @dev-b"))
        self.assertEqual(six["causes"], ["children blocked"])
        self.assertEqual(six["blocked_since_source"], "last update (approx.)")
        # no --deep: relations are read only for the blocked issues
        rel_calls = sorted(c[0] for c in router.calls if c[0].endswith("/relations/"))
        self.assertEqual(rel_calls, ["/projects/p-1/work-items/i-1/relations/",
                                     "/projects/p-1/work-items/i-6/relations/"])

    def test_whole_project_with_deep_finds_relation_only_blocks(self):
        res, router = self.report("--all", "--deep")
        self.assertEqual(res["scope"], "whole project")
        self.assertEqual([r["key"] for r in res["issues"]], ["DEMO-1", "DEMO-6", "DEMO-7"])
        seven = res["issues"][2]
        self.assertFalse(seven["marked_blocked"])
        self.assertEqual(seven["causes"], ["dependency"])
        self.assertIn("not marked as blocked", seven["next_steps"][-1])
        self.assertIsNone(seven["period"])
        self.assertFalse(any("cycle-issues" in c[0] for c in router.calls))

    def test_whole_project_without_deep_misses_relation_only_blocks(self):
        res, _ = self.report("--all")
        self.assertEqual([r["key"] for r in res["issues"]], ["DEMO-1", "DEMO-6"])

    def test_stale_days_option(self):
        res, _ = self.report("--period", "Cycle 2", "--stale-days", "0")
        self.assertEqual([r["key"] for r in res["issues"]], ["DEMO-3", "DEMO-1", "DEMO-6"])  # longest blocked first
        self.assertEqual(res["stale_excluded"], [])

    def test_markdown_report(self):
        out, _ = self.run_cli("report", "--target", "plane", "--project", "DEMO", "--today", "2026-03-01",
                              "--period", "Cycle 2")
        self.assertIn("# Blocked issues — Demo (DEMO)", out)
        self.assertIn("Scope: **Cycle 2** · today 2026-03-01", out)
        self.assertIn("**2 blocked issue(s)**, 1 stale left out.", out)
        self.assertIn("- **External references:** OPS-9", out)
        self.assertIn("### Stale, left out", out)
        self.assertNotIn("plane-secret-456", out)

    def test_periods_json(self):
        out, _ = self.run_cli("periods", "--target", "plane", "--project", "DEMO", "--today", "2026-03-01",
                              "--format", "json")
        res = json.loads(out)
        self.assertEqual([p["name"] for p in res["periods"]], ["Cycle 2", "Cycle 3"])
        self.assertEqual(res["whole_project"], {"open_issues": 5, "blocked_label": 3})

    def test_projects_json(self):
        out, _ = self.run_cli("projects", "--target", "plane", "--format", "json")
        self.assertEqual([p["path"] for p in json.loads(out)], ["DEMO", "OPS"])


class GitLabReportTest(unittest.TestCase):
    def run_cli(self, *argv):
        patches, log = gitlab_transport()
        with patches[0], patches[1], patches[2]:
            code, out, err = Run(self, GITLAB_ENV)(*argv)
        self.assertEqual(code, 0, err)
        return out, err, log

    def report(self, *scope):
        out, err, log = self.run_cli("report", "--target", "gitlab", "--project", "demo/app",
                                     "--today", "2026-03-01", "--format", "json", *scope)
        return json.loads(out), log

    def test_milestone_report_json(self):
        res, log = self.report("--period", "v0.3")
        self.assertEqual(log["gql"][0]["ms"], ["v0.3"])
        self.assertEqual((res["target"], res["project"], res["scope"]), ("gitlab", "demo/app", "v0.3"))
        self.assertEqual([r["key"] for r in res["issues"]], ["#1", "#4"])
        self.assertEqual(sorted(s["key"] for s in res["stale_excluded"]), ["#3", "#6"])
        one, four = res["issues"]
        self.assertTrue(one["marked_blocked"])
        self.assertEqual(one["causes"], ["dependency"])
        self.assertEqual([b["key"] for b in one["blocked_by"]], ["#2"])
        self.assertEqual((one["blocked_since"], one["blocked_since_source"]), ("2026-02-17", "label added"))
        self.assertEqual(one["external_refs"], ["INFRA-312"])
        self.assertEqual(one["parent"]["key"], "#10")
        self.assertEqual([c["key"] for c in one["children"]], ["#5"])
        self.assertFalse(four["marked_blocked"])
        self.assertEqual([b["key"] for b in four["blocked_by"]], ["other-group/lib#7"])
        self.assertEqual((four["blocked_since"], four["blocked_since_source"]), ("2026-02-20", "link created"))
        self.assertIn("Nobody is assigned: assign an owner to drive the unblocking.", four["next_steps"])

    def test_whole_project_sends_no_milestone(self):
        res, log = self.report("--all")
        self.assertIsNone(log["gql"][0]["ms"])
        self.assertEqual(res["scope"], "whole project")

    def test_empty_milestone_warns(self):
        patches, _ = gitlab_transport()
        empty = {"project": {"workItems": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}
        with patches[1], patches[2], mock.patch.object(blk.GitLab, "graphql", return_value=empty):
            code, out, err = Run(self, GITLAB_ENV)("report", "--target", "gitlab", "--project", "demo/app",
                                                   "--period", "v9", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertIn("no open issues in milestone 'v9'", err)
        self.assertEqual(json.loads(out)["issues"], [])

    def test_periods_markdown(self):
        out, _, _ = self.run_cli("periods", "--target", "gitlab", "--project", "demo/app")
        self.assertIn("# Open milestones — Demo App", out)
        self.assertIn("| 1 | v0.3 | 2026-02-16 | 2026-03-13 | 4 | 3 |", out)
        self.assertIn("| 3 | **whole project** | | | 5 | 3 |", out)


if __name__ == "__main__":
    unittest.main()
