"""quality.py through its CLI, against an offline GitLab and Plane."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load

dq = load("delivery-quality", "quality.py")
ENVIRONMENT = {"GITLAB_URL": "https://gl.example", "GITLAB_TOKEN": "glpat-secret", "PLANE_URL": "https://plane.example",
               "PLANE_API_KEY": "plane-secret", "PLANE_WORKSPACE": "demo"}
SECRETS = ("glpat-secret", "plane-secret")


class World:
    """One sprint from 03-02 to 03-15: #1 went to fix twice and was delivered, #2 was delivered, #3 went to fix
    and is open, #4 went to fix before it joined the sprint, #5 went to fix, was delivered and was moved to
    another milestone after closing."""

    def __init__(self, fix_label="workflow::to-fix", plane_state="To Fix"):
        self.fix_label, self.plane_state = fix_label, plane_state
        self.requests = []

    def http(self, method, url, headers, body=None):
        self.requests.append((method, url))
        if url.endswith("/api/graphql"):
            return self.graphql(body["query"]), {}
        path = url.split("?")[0]
        return (self.gitlab(path) if "/api/v4/" in path else self.plane(path)), {}

    def graphql(self, query):
        if "milestones(" in query:
            return {"data": {"project": {"name": "Demo", "lastActivityAt": "2026-03-20T10:00:00Z", "milestones": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"title": "Sprint 1", "state": "closed", "startDate": "2026-03-02", "dueDate": "2026-03-15"}]}}}}
        nodes = [self.work_item(1, "2026-03-12T10:00:00Z"), self.work_item(2, "2026-03-13T10:00:00Z"),
                 self.work_item(3, None), self.work_item(4, "2026-03-11T10:00:00Z"),
                 self.work_item(5, "2026-03-10T10:00:00Z", milestone="Later", updated="2026-03-16T10:00:00Z")]
        return {"data": {"project": {"workItems": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}}

    @staticmethod
    def work_item(iid, closed, milestone="Sprint 1", updated=None):
        return {"iid": str(iid), "title": f"Task | {iid}", "state": "CLOSED" if closed else "OPEN",
                "webUrl": f"https://gl/demo/{iid}", "createdAt": "2026-02-20T10:00:00Z",
                "updatedAt": updated or closed or "2026-03-14T10:00:00Z", "closedAt": closed,
                "widgets": [{"type": "LABELS", "labels": {"nodes": []}},
                            {"type": "MILESTONE", "milestone": {"title": milestone}}]}

    PASSES = {"1": ["2026-03-05T10:00:00Z", "2026-03-08T10:00:00Z"], "3": ["2026-03-09T10:00:00Z"],
              "4": ["2026-03-01T10:00:00Z"], "5": ["2026-03-06T10:00:00Z"]}
    JOINED = {"4": "2026-03-03T10:00:00Z"}
    MOVED_OUT = {"5": "2026-03-16T10:00:00Z"}  # to "Later", after it was delivered

    def gitlab(self, path):
        if path.endswith("/projects"):
            return [{"path_with_namespace": "demo/app", "name": "App", "web_url": "https://gl/demo"}]
        iid = path.split("/issues/")[1].split("/")[0]
        if path.endswith("/resource_label_events"):
            return [{"action": "add", "created_at": when, "label": {"name": self.fix_label}} for when in self.PASSES.get(iid, [])]
        if iid in self.JOINED:
            return [{"action": "add", "created_at": self.JOINED[iid], "milestone": {"title": "Sprint 1"}}]
        if iid in self.MOVED_OUT:
            when = self.MOVED_OUT[iid]
            return [{"action": "add", "created_at": "2026-02-20T10:00:00Z", "milestone": {"title": "Sprint 1"}},
                    {"action": "remove", "created_at": when, "milestone": {"title": "Sprint 1"}},
                    {"action": "add", "created_at": when, "milestone": {"title": "Later"}}]
        return []

    def plane(self, path):
        parts = path.rstrip("/").split("/")
        if parts[-1] == "projects":
            return [{"id": "p", "identifier": "DEMO", "name": "Demo", "timezone": "UTC"}]
        if parts[-1] == "cycles":
            return [{"id": "c", "name": "Sprint 1", "start_date": "2026-03-02T00:00:01Z",
                     "end_date": "2026-03-15T23:59:00Z", "status": "completed"}]
        if parts[-1] == "cycle-issues":
            return [{"id": f"i{n}"} for n in range(1, 5)]
        if parts[-1] == "issues":
            return [self.plane_issue(n, closed) for n, closed in
                    ((1, "2026-03-12T10:00:00Z"), (2, "2026-03-13T10:00:00Z"), (3, None), (4, "2026-03-11T10:00:00Z"),
                     (5, "2026-03-10T10:00:00Z"))]
        if parts[-1] == "activities":
            iid = parts[-2][1:]
            moves = [{"field": "cycles", "verb": "created", "created_at": self.JOINED[iid], "old_value": None,
                      "new_value": "Sprint 1"}] if iid in self.JOINED else []
            if iid in self.MOVED_OUT:  # out of its cycle after it was delivered
                moves = [{"field": "cycles", "verb": "deleted", "created_at": self.MOVED_OUT[iid],
                          "old_value": "Sprint 1", "new_value": ""}]
            return moves + [{"field": "state", "verb": "updated", "created_at": when, "new_value": self.plane_state}
                            for when in self.PASSES.get(iid, [])]
        return []

    @staticmethod
    def plane_issue(n, closed):
        state = {"group": "completed", "name": "Done"} if closed else {"group": "started", "name": "Doing"}
        updated = "2026-03-16T10:00:00Z" if n == 5 else closed or "2026-03-14T10:00:00Z"
        return {"id": f"i{n}", "sequence_id": n, "name": f"Task | {n}", "state": state, "completed_at": closed,
                "updated_at": updated, "created_at": "2026-02-20T10:00:00Z"}


def run(world, *argv, environment=ENVIRONMENT):
    stdout, stderr, code, here = io.StringIO(), io.StringIO(), 0, os.getcwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(dq, "http", world.http), \
                    mock.patch("time.sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                dq.main([*argv, "--rate", "6000"])
        except SystemExit as stop:
            code = stop.code
        finally:
            os.chdir(here)
    return code, stdout.getvalue(), stderr.getvalue()


def project(target):
    return ["--target", target, "--project", "demo/app" if target == "gitlab" else "DEMO"]


class ReportTest(unittest.TestCase):
    def report(self, target, *extra, world=None):
        code, stdout, stderr = run(world or World(), "report", *project(target), "--today", "2026-03-20",
                                   "--format", "json", *extra)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def test_numbers_are_the_same_in_gitlab_and_plane(self):
        for target in ("gitlab", "plane"):
            with self.subTest(target=target):
                row = self.report(target)["rows"][0]
                self.assertEqual((row["delivered"], row["went_to_fix"], row["passes"], row["quality"], row["rating"]),
                                 (4, 2, 3, 0.5, "poor"))
                self.assertEqual([i["passes"] for i in row["returned"]], [["2026-03-05", "2026-03-08"], ["2026-03-06"]])
                self.assertEqual([i["passes"] for i in row["still_open"]], [["2026-03-09"]])

    def test_another_name_for_to_fix(self):
        for target in ("gitlab", "plane"):
            with self.subTest(target=target):
                world = World(fix_label="workflow::devolvida", plane_state="Devolvida")
                self.assertEqual(self.report(target, world=world)["rows"][0]["went_to_fix"], 0)
                row = self.report(target, "--fix-names", "devolvida", world=world)["rows"][0]
                self.assertEqual(row["went_to_fix"], 2)

    def test_markdown(self):
        code, stdout, stderr = run(World(), "report", *project("gitlab"), "--today", "2026-03-20")
        self.assertEqual(code, 0, stderr)
        self.assertIn("| Sprint 1 | 2026-03-02 → 2026-03-15 | finished | 4 | 2 | 3 | **50%** | poor |", stdout)
        self.assertIn("[#1](https://gl/demo/1) Task \\| 1 | 2 | 2026-03-05, 2026-03-08 |", stdout)
        self.assertIn("still open in Sprint 1, not counted: [#3]", stdout)
        self.assertIn("To fix: to fix · days in UTC · today 2026-03-20", stdout)

    def test_reads_only(self):
        world = World()
        for target in ("gitlab", "plane"):
            run(world, "report", *project(target), "--today", "2026-03-20")
        writes = [r for r in world.requests if r[0] != "GET" and not r[1].endswith("/api/graphql")]
        self.assertEqual(writes, [])

    def test_unknown_period_lists_the_valid_ones(self):
        code, _, stderr = run(World(), "report", *project("plane"), "--period", "Nope", "--today", "2026-03-20")
        self.assertIn("Available: Sprint 1", str(code) + stderr)


class CommandsTest(unittest.TestCase):
    def test_projects(self):
        code, stdout, _ = run(World(), "projects", "--target", "gitlab")
        self.assertEqual(code, 0)
        self.assertIn("| 1 | [demo/app](https://gl/demo) | App |", stdout)

    def test_periods(self):
        code, stdout, _ = run(World(), "periods", *project("plane"), "--format", "json", "--today", "2026-03-20")
        self.assertEqual(code, 0)
        self.assertEqual([(p["name"], p["status"]) for p in json.loads(stdout)["periods"]], [("Sprint 1", "finished")])

    def test_a_command_needs_a_project(self):
        code, _, _ = run(World(), "report", "--target", "gitlab")
        self.assertIn("needs --project", str(code))

    def test_not_configured(self):
        code, stdout, stderr = run(World(), "report", *project("plane"), environment={})
        self.assertEqual(code, 2)
        self.assertIn("NOT CONFIGURED: missing PLANE_URL, PLANE_WORKSPACE, PLANE_API_KEY.", stderr)

    def test_api_error_is_a_message_without_secrets(self):
        def refuse(method, url, headers, body=None):
            raise dq.ApiError(401, '{"message": "401 Unauthorized"}', method, url)

        world = World()
        world.http = refuse
        for target in ("gitlab", "plane"):
            with self.subTest(target=target):
                code, stdout, stderr = run(world, "report", *project(target))
                self.assertIn("API error", str(code))
                self.assertIn("401", str(code))
                for secret in SECRETS:
                    self.assertNotIn(secret, str(code) + stdout + stderr)

    def test_gitlab_project_not_found(self):
        world = World()
        world.graphql = lambda query: {"data": {"project": None}}
        code, _, _ = run(world, "report", *project("gitlab"))
        self.assertIn("not found", str(code))


class RetryTest(unittest.TestCase):
    def test_gitlab_reads_are_retried(self):
        answers = [dq.ApiError(502, "bad gateway", "GET", "u"), dq.ApiError(429, "slow", "GET", "u"), ({"ok": 1}, {})]
        with mock.patch.object(dq, "http", side_effect=answers) as http, mock.patch("time.sleep") as sleep:
            self.assertEqual(dq.fetch("GET", "u", {}), ({"ok": 1}, {}))
        self.assertEqual(http.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 10])

    def test_retries_are_bounded(self):
        for status, attempts in ((429, 8), (502, 4), (404, 1)):
            with mock.patch.object(dq, "http", side_effect=dq.ApiError(status, "x", "GET", "u")) as http, \
                    mock.patch("time.sleep"):
                with self.assertRaises(dq.ApiError):
                    dq.fetch("GET", "u", {})
            self.assertEqual(http.call_count, attempts, status)

    def test_bad_answers_are_api_errors(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>"
        for patch in ({"return_value": response}, {"side_effect": TimeoutError("slow")}):
            with mock.patch.object(dq.urllib.request, "urlopen", **patch):
                with self.assertRaises(dq.ApiError) as caught:
                    dq.http("GET", "https://x", {"PRIVATE-TOKEN": "glpat-secret"})
            self.assertNotIn("glpat-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
