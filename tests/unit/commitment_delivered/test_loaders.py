"""GitLab and Plane clients and loaders of commitment.py, fed with fixtures (no network)."""
import datetime as dt
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import load

cd = load("commitment-delivered", "commitment.py")
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SINCE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gitlab():
    gl = cd.GitLab.__new__(cd.GitLab)
    gl.url, gl.headers, gl.project = "https://gitlab.example.com", {"PRIVATE-TOKEN": "glpat-secret"}, "demo/app"
    return gl


def plane():
    p = cd.Plane.__new__(cd.Plane)
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
            raise cd.ApiError(404, '{"error": "not found"}', "GET", path)
        return value


class GitLabLoaderTest(unittest.TestCase):
    def test_periods(self):
        gl = gitlab()
        with mock.patch.object(cd.GitLab, "graphql", return_value=fixture("gitlab_milestones.json")["data"]):
            periods = gl.periods()
        self.assertEqual(gl.name, "Demo Project")
        self.assertEqual(periods[0], {"name": "v0.1", "start": "2026-01-05", "end": "2026-01-18", "closed": True})
        self.assertFalse(periods[1]["closed"])
        self.assertIsNone(periods[2]["start"])

    def test_missing_project_exits_with_a_message(self):
        with mock.patch.object(cd.GitLab, "graphql", return_value={"project": None}):
            with self.assertRaises(SystemExit) as cm:
                gitlab().periods()
        self.assertIn("not found", str(cm.exception.code))
        self.assertNotIn("glpat-secret", str(cm.exception.code))

    def load_issues(self):
        pages = [fixture("gitlab_work_items_page1.json")["data"], fixture("gitlab_work_items_page2.json")["data"]]
        events = {f"/projects/demo%2Fapp/issues/{n}/resource_milestone_events": fixture(f"gitlab_milestone_events_{n}.json")
                  for n in ("1", "2", "3")}
        with mock.patch.object(cd.GitLab, "graphql", side_effect=pages) as gql, \
                mock.patch.object(cd.GitLab, "rest_pages", side_effect=lambda path: list(events[path])) as rest:
            issues = gitlab().issues(SINCE)
        return issues, gql, rest

    def test_issues_paginate_and_normalize(self):
        issues, gql, _ = self.load_issues()
        self.assertEqual(gql.call_count, 2)
        self.assertEqual(gql.call_args_list[1].args[1], {"p": "demo/app", "after": "CURSOR1"})
        by_key = {i["key"]: i for i in issues}
        self.assertEqual(list(by_key), ["#1", "#2", "#3", "#4"])
        one = by_key["#1"]
        self.assertEqual((one["iid"], one["title"], one["url"]),
                         ("1", "Add login", "https://gitlab.example.com/demo/app/-/issues/1"))
        self.assertEqual((one["created"], one["updated"], one["closed"]),
                         ("2026-01-02T09:00:00Z", "2026-01-10T15:00:00Z", "2026-01-10T15:00:00Z"))
        self.assertEqual((one["points"], one["assignees"], one["period"]), (3.0, ["dev-a", "dev-b"], "v0.1"))
        self.assertFalse(one["cancelled"])
        self.assertTrue(by_key["#2"]["cancelled"])
        self.assertEqual(by_key["#2"]["assignees"], [])
        self.assertIsNone(by_key["#3"]["closed"])          # open
        self.assertEqual(by_key["#3"]["points"], 1.5)      # sp:: scope
        self.assertIsNone(by_key["#3"]["period"])          # milestone: null
        self.assertIsNone(by_key["#4"]["points"])          # non-numeric points label
        self.assertEqual(by_key["#4"]["assignees"], [])    # no assignees widget

    def test_milestone_history(self):
        issues, _, rest = self.load_issues()
        by_key = {i["key"]: i for i in issues}
        # "add" first: the issue had no milestone before.
        self.assertIsNone(by_key["#1"]["initial"])
        self.assertEqual(by_key["#1"]["events"], [(cd.parse_dt("2026-01-03T10:00:00Z"), "v0.1")])
        # "remove" first (sorted by time): the issue was created in that milestone.
        self.assertEqual(by_key["#2"]["initial"], "v0.2")
        self.assertEqual([p for _, p in by_key["#2"]["events"]], [None, "v0.1"])
        # Unsorted events are replayed in time order; "remove" leaves the issue without a milestone.
        self.assertEqual(by_key["#3"]["events"], [(cd.parse_dt("2026-01-03T10:00:00Z"), "v0.1"),
                                                  (cd.parse_dt("2026-01-08T10:00:00Z"), None)])
        self.assertEqual(cd.period_at(by_key["#3"], cd.parse_dt("2026-01-05T23:59:59Z")), "v0.1")
        # Not updated since the first analyzed period began: no request, the current milestone holds.
        self.assertIsNone(by_key["#4"]["events"])
        self.assertEqual(rest.call_count, 3)
        # The project path is URL-encoded and the token is never part of the path.
        for call in rest.call_args_list:
            self.assertIn("/projects/demo%2Fapp/", call.args[0])
            self.assertNotIn("glpat-secret", call.args[0])

    def test_no_events_keeps_the_issue_without_history(self):
        pages = [fixture("gitlab_work_items_page2.json")["data"]]
        with mock.patch.object(cd.GitLab, "graphql", side_effect=pages), \
                mock.patch.object(cd.GitLab, "rest_pages", return_value=[]):
            issues = gitlab().issues(SINCE)
        self.assertIsNone(issues[0]["events"])
        self.assertIsNone(issues[0]["initial"])

    def test_open_issue_with_cancel_label_is_not_cancelled(self):
        page = {"project": {"workItems": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"iid": "9", "title": "t", "state": "OPEN", "webUrl": "u", "closedAt": None,
             "createdAt": "2025-01-01T00:00:00Z", "updatedAt": "2025-01-01T00:00:00Z",
             "widgets": [{"type": "LABELS", "labels": {"nodes": [{"title": "workflow::cancelled"}]}}]}]}}}
        with mock.patch.object(cd.GitLab, "graphql", return_value=page):
            self.assertFalse(gitlab().issues(SINCE)[0]["cancelled"])


class PlaneLoaderTest(unittest.TestCase):
    def routes(self):
        return {
            "/projects/p-1/cycles/": fixture("plane_cycles.json"),
            "/projects/p-1/labels/": fixture("plane_labels.json"),
            "/projects/p-1/cycles/c-1/cycle-issues/": fixture("plane_cycle_issues_c1.json"),
            "/projects/p-1/cycles/c-2/cycle-issues/": fixture("plane_cycle_issues_c2.json"),
            "/projects/p-1/cycles/c-3/cycle-issues/": {"results": []},
            "/projects/p-1/issues/": fixture("plane_issues.json"),
            "/projects/p-1/issues/i-1/activities/": fixture("plane_activities_i1.json"),
            "/projects/p-1/issues/i-2/activities/": fixture("plane_activities_i2.json"),
            "/projects/p-1/issues/i-3/activities/": fixture("plane_activities_i3.json"),
        }

    def load(self, routes):
        p = plane()
        router = PlaneRouter(routes)
        with mock.patch.object(cd.Plane, "request", side_effect=router):
            periods = p.periods()
            issues = p.issues(SINCE)
        return p, periods, {i["key"]: i for i in issues}, router

    def test_periods_drop_archived_and_trim_dates(self):
        _, periods, _, _ = self.load(self.routes())
        self.assertEqual([c["name"] for c in periods], ["Cycle 1", "Cycle 2", "Draft"])
        self.assertEqual(periods[0], {"name": "Cycle 1", "id": "c-1", "start": "2026-01-05", "end": "2026-01-18",
                                      "closed": True})
        self.assertFalse(periods[1]["closed"])
        self.assertIsNone(periods[2]["start"])

    def test_issues_normalized_with_estimate_points(self):
        _, _, by_key, router = self.load(self.routes())
        self.assertEqual(sorted(by_key), ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4"])
        one = by_key["DEMO-1"]
        self.assertEqual(one["points"], 3.0)  # from the Estimate, not the points::8 label
        self.assertEqual(one["closed"], "2026-01-10T15:00:00Z")
        self.assertEqual(one["created"], "2026-01-02T09:00:00Z")
        self.assertFalse(one["cancelled"])
        self.assertEqual(one["assignees"], ["dev-a", "b@example.com"])
        self.assertEqual(one["period"], "Cycle 1")
        self.assertEqual(one["url"], "https://plane.example.com/demo-ws/projects/p-1/issues/i-1")
        two = by_key["DEMO-2"]
        self.assertTrue(two["cancelled"])
        self.assertEqual(two["points"], 5.0)
        self.assertEqual(two["closed"], "2026-01-12T09:00:00Z")  # falls back to updated_at
        self.assertEqual(by_key["DEMO-3"]["period"], "Cycle 2")  # cycle-issues row keyed by "issue"
        self.assertIsNone(by_key["DEMO-3"]["points"])            # no estimate point
        self.assertIsNone(by_key["DEMO-3"]["closed"])
        self.assertIsNone(by_key["DEMO-4"]["points"])            # non-numeric estimate value "XL"
        self.assertIsNone(by_key["DEMO-4"]["period"])
        issue_calls = [c for c in router.calls if c[0] == "/projects/p-1/issues/"]
        self.assertEqual(issue_calls[0][1]["expand"], "state,labels,assignees,estimate_point")

    def test_cycle_history_from_activities(self):
        _, _, by_key, router = self.load(self.routes())
        one = by_key["DEMO-1"]
        self.assertIsNone(one["initial"])  # "created": joined Cycle 1 from no cycle
        self.assertEqual(one["events"], [(cd.parse_dt("2026-01-03T10:00:00Z"), "Cycle 1")])  # only cycle activity
        two = by_key["DEMO-2"]
        self.assertEqual(two["initial"], "Archived")  # archived cycle: falls back to the activity's name
        self.assertEqual(two["events"], [(cd.parse_dt("2026-01-06T10:00:00Z"), "Cycle 1")])
        three = by_key["DEMO-3"]
        self.assertEqual([(e[0].day, e[1]) for e in three["events"]], [(3, "Cycle 1"), (10, None), (20, "Cycle 2")])
        self.assertIsNone(cd.period_at(three, cd.parse_dt("2026-01-15T00:00:00Z")))
        # Not updated since the first analyzed period began: no activity request.
        self.assertIsNone(by_key["DEMO-4"]["events"])
        activity_paths = [c[0] for c in router.calls if c[0].endswith("/activities/")]
        self.assertEqual(sorted(activity_paths), ["/projects/p-1/issues/i-1/activities/",
                                                  "/projects/p-1/issues/i-2/activities/",
                                                  "/projects/p-1/issues/i-3/activities/"])

    def test_points_label_is_never_used(self):
        for value in (None, {"value": ""}, {"value": "XL"}):
            routes = self.routes()
            path = "/projects/p-1/issues/"
            routes[path]["results"][0]["estimate_point"] = value  # this issue has a points::8 label
            _, _, issues, _ = self.load(routes)
            one = issues["DEMO-1"]
            self.assertIsNone(one["points"])

    def test_estimates_endpoint_is_never_called(self):
        # Plane 1.4.3 does not route /estimates/ in its public API.
        _, _, _, router = self.load(self.routes())
        self.assertFalse([c for c in router.calls if "/estimates/" in c[0]])


class PlaneConstructorTest(unittest.TestCase):
    def make(self, project):
        with mock.patch.object(cd.Plane, "list", return_value=fixture("plane_projects.json")["results"]):
            return cd.Plane("https://plane.example.com/", "demo-ws", "plane-secret", project)

    def test_matches_identifier_name_or_id(self):
        for name in ("demo", " DEMO ", "Demo", "p-1"):
            self.assertEqual(self.make(name).pid, "p-1")
        self.assertEqual(self.make("DEMO").name, "Demo (DEMO)")


class HttpTest(unittest.TestCase):
    def test_http_error_becomes_api_error_without_headers(self):
        err = urllib.error.HTTPError("https://h/x", 401, "Unauthorized", {}, io.BytesIO(b'{"message":"401"}'))
        with mock.patch.object(cd.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(cd.ApiError) as cm:
                cd.http("GET", "https://h/x", {"PRIVATE-TOKEN": "glpat-secret"})
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("HTTP 401", str(cm.exception))
        self.assertNotIn("glpat-secret", str(cm.exception))

    def test_unreachable_host_exits_cleanly(self):
        with mock.patch.object(cd.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SystemExit) as cm:
                cd.http("GET", "https://h/x", {"X-API-Key": "plane-secret"})
        self.assertIn("cannot reach", str(cm.exception.code))
        self.assertNotIn("plane-secret", str(cm.exception.code))

    def test_request_has_a_timeout_and_sends_the_token_only_as_a_header(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"[]"
        with mock.patch.object(cd.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.assertEqual(cd.http("GET", "https://h/x", {"X-API-Key": "plane-secret"})[0], [])
        self.assertGreater(urlopen.call_args.kwargs["timeout"], 0)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://h/x")
        self.assertEqual(request.get_header("X-api-key"), "plane-secret")


if __name__ == "__main__":
    unittest.main()
