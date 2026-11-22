"""GitLab and Plane clients and loaders, fed with fixtures (no network)."""
import json
import pathlib
import unittest
from unittest import mock

from tests.unit._load import load

vel = load("team-velocity", "velocity.py")
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gitlab():
    gl = vel.GitLab.__new__(vel.GitLab)
    gl.url, gl.headers, gl.project = "https://gitlab.example.com", {"PRIVATE-TOKEN": "glpat-secret"}, "demo/app"
    return gl


def plane():
    p = vel.Plane.__new__(vel.Plane)
    p.url, p.ws = "https://plane.example.com", "demo-ws"
    p.base = "https://plane.example.com/api/v1/workspaces/demo-ws"
    p.headers = {"X-API-Key": "plane-secret"}
    p.min_interval, p.last_request = 0.0, 0.0
    p.pid, p.ident, p.name = "p-1", "DEMO", "Demo (DEMO)"
    return p


class PlaneRouter:
    """Fake Plane.request: answers by path from fixtures; a 404 entry raises ApiError(404)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        value = self.routes[path]
        if value == 404:
            raise vel.ApiError(404, '{"error": "not found"}', "GET", path)
        return value


class GitLabLoaderTest(unittest.TestCase):
    def test_periods(self):
        gl = gitlab()
        with mock.patch.object(vel.GitLab, "graphql", return_value=fixture("gitlab_milestones.json")["data"]):
            periods = gl.periods()
        self.assertEqual(gl.name, "Demo Project")
        self.assertEqual(periods[0], {"name": "v0.1", "start": "2026-01-05", "end": "2026-01-18", "closed": True})
        self.assertFalse(periods[1]["closed"])
        self.assertIsNone(periods[2]["start"])

    def test_missing_project_exits_with_a_message(self):
        with mock.patch.object(vel.GitLab, "graphql", return_value={"project": None}):
            with self.assertRaises(SystemExit) as cm:
                gitlab().periods()
        self.assertIn("not found", str(cm.exception.code))
        self.assertNotIn("glpat-secret", str(cm.exception.code))

    def test_issues_paginate_and_normalize(self):
        pages = [fixture("gitlab_work_items_page1.json")["data"], fixture("gitlab_work_items_page2.json")["data"]]
        with mock.patch.object(vel.GitLab, "graphql", side_effect=pages) as gql:
            issues = gitlab().issues()
        self.assertEqual(gql.call_count, 2)
        self.assertEqual(gql.call_args_list[1].args[1], {"p": "demo/app", "after": "CURSOR1"})
        by_key = {i["key"]: i for i in issues}
        self.assertEqual(list(by_key), ["#1", "#2", "#3", "#4"])
        self.assertEqual(by_key["#1"], vel.new_issue(
            key="#1", title="Add login", url="https://gitlab.example.com/demo/app/-/issues/1",
            closed="2026-01-10T15:00:00Z", cancelled=False, points=3.0, assignees=["dev-a", "dev-b"], period="v0.1"))
        self.assertTrue(by_key["#2"]["cancelled"])
        self.assertEqual(by_key["#2"]["assignees"], [])
        self.assertIsNone(by_key["#3"]["closed"])          # open
        self.assertEqual(by_key["#3"]["points"], 1.5)      # sp:: scope
        self.assertIsNone(by_key["#3"]["period"])          # milestone: null
        self.assertIsNone(by_key["#4"]["points"])          # non-numeric points label
        self.assertEqual(by_key["#4"]["assignees"], [])    # no assignees widget

    def test_open_issue_with_cancel_label_is_not_cancelled(self):
        page = {"project": {"workItems": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"iid": "9", "title": "t", "state": "OPEN", "webUrl": "u", "closedAt": None,
             "widgets": [{"type": "LABELS", "labels": {"nodes": [{"title": "workflow::cancelled"}]}}]}]}}}
        with mock.patch.object(vel.GitLab, "graphql", return_value=page):
            self.assertFalse(gitlab().issues()[0]["cancelled"])


class PlaneLoaderTest(unittest.TestCase):
    def routes(self):
        return {
            "/projects/p-1/cycles/": fixture("plane_cycles.json"),
            "/projects/p-1/labels/": fixture("plane_labels.json"),
            "/projects/p-1/cycles/c-1/cycle-issues/": fixture("plane_cycle_issues_c1.json"),
            "/projects/p-1/cycles/c-2/cycle-issues/": {"results": [], "next_page_results": False},
            "/projects/p-1/cycles/c-3/cycle-issues/": {"results": []},
        }

    def load(self, routes):
        p = plane()
        router = PlaneRouter(routes)
        with mock.patch.object(vel.Plane, "request", side_effect=router):
            periods = p.periods()
            issues = p.issues()
        return p, periods, issues, router

    def test_periods_drop_archived_and_trim_dates(self):
        _, periods, _, _ = self.load(self.routes())
        self.assertEqual([c["name"] for c in periods], ["Cycle 1", "Cycle 2", "Draft"])
        self.assertEqual(periods[0], {"name": "Cycle 1", "id": "c-1", "start": "2026-01-05", "end": "2026-01-18",
                                      "closed": True})
        self.assertFalse(periods[1]["closed"])
        self.assertIsNone(periods[2]["start"])
        self.assertIsNone(periods[2]["end"])

    def test_issues_normalized_with_estimate_points(self):
        p, _, issues, router = self.load(self.routes())
        by_key = {i["key"]: i for i in issues}
        self.assertEqual(sorted(by_key), ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4"])
        one = by_key["DEMO-1"]
        self.assertEqual(one["points"], 3.0)  # from the Estimate, not the points::8 label
        self.assertEqual(one["closed"], "2026-01-10T15:00:00Z")
        self.assertFalse(one["cancelled"])
        self.assertEqual(one["assignees"], ["dev-a", "b@example.com"])
        self.assertEqual(one["period"], "Cycle 1")
        self.assertEqual(one["url"], "https://plane.example.com/demo-ws/projects/p-1/issues/i-1")
        two = by_key["DEMO-2"]
        self.assertTrue(two["cancelled"])
        self.assertEqual(two["closed"], "2026-01-12T09:00:00Z")  # falls back to updated_at
        self.assertIsNone(by_key["DEMO-3"]["points"])  # no estimate point
        self.assertIsNone(by_key["DEMO-3"]["closed"])
        self.assertIsNone(by_key["DEMO-4"]["points"])  # non-numeric estimate value "XL"
        cycle_calls = [c for c in router.calls if c[0].endswith("cycle-issues/")]
        self.assertTrue(all(c[1]["expand"] == "state,labels,assignees,estimate_point" for c in cycle_calls))

    def test_points_label_is_never_used(self):
        for value in (None, {"value": ""}, {"value": "XL"}):
            routes = self.routes()
            path = "/projects/p-1/cycles/c-1/cycle-issues/"
            routes[path]["results"][0]["estimate_point"] = value  # this issue has a points::8 label
            _, _, issues, _ = self.load(routes)
            one = next(i for i in issues if i["key"] == "DEMO-1")
            self.assertIsNone(one["points"])

    def test_estimates_endpoint_is_never_called(self):
        # Plane 1.4.3 does not route /estimates/ in its public API.
        _, _, _, router = self.load(self.routes())
        self.assertFalse([c for c in router.calls if "/estimates/" in c[0]])


class PlaneConstructorTest(unittest.TestCase):
    def make(self, project):
        with mock.patch.object(vel.Plane, "list", return_value=fixture("plane_projects.json")["results"]):
            return vel.Plane("https://plane.example.com/", "demo-ws", "plane-secret", project)

    def test_matches_identifier_name_or_id(self):
        for name in ("demo", " DEMO ", "Demo", "p-1"):
            self.assertEqual(self.make(name).pid, "p-1")
        self.assertEqual(self.make("DEMO").name, "Demo (DEMO)")


class HttpTest(unittest.TestCase):
    def test_http_error_becomes_api_error_without_headers(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("https://h/x", 401, "Unauthorized", {}, io.BytesIO(b'{"message":"401"}'))
        with mock.patch.object(vel.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(vel.ApiError) as cm:
                vel.http("GET", "https://h/x", {"PRIVATE-TOKEN": "glpat-secret"})
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn("glpat-secret", str(cm.exception))

    def test_unreachable_host_exits_cleanly(self):
        import urllib.error
        with mock.patch.object(vel.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SystemExit) as cm:
                vel.http("GET", "https://h/x", {"X-API-Key": "plane-secret"})
        self.assertIn("cannot reach", str(cm.exception.code))
        self.assertNotIn("plane-secret", str(cm.exception.code))

    def test_request_has_a_timeout(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"[]"
        with mock.patch.object(vel.urllib.request, "urlopen", return_value=resp) as urlopen:
            vel.http("GET", "https://h/x", {})
        self.assertGreater(urlopen.call_args.kwargs["timeout"], 0)


if __name__ == "__main__":
    unittest.main()
