"""GitLab and Plane clients and loaders of stale.py, fed with fixtures (no network)."""
import datetime as dt
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import load

st = load("stale-issues", "stale.py")
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 3, 10)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gitlab():
    gl = st.GitLab.__new__(st.GitLab)
    gl.url, gl.headers, gl.project = "https://gitlab.example.com", {"PRIVATE-TOKEN": "glpat-secret"}, "demo/app"
    return gl


def plane():
    p = st.Plane.__new__(st.Plane)
    p.url, p.ws = "https://plane.example.com", "demo-ws"
    p.base = "https://plane.example.com/api/v1/workspaces/demo-ws"
    p.headers = {"X-API-Key": "plane-secret"}
    p.min_interval, p.last = 0.0, 0.0
    p.pid, p.ident, p.name = "p-1", "DEMO", "Demo (DEMO)"
    return p


class PlaneRouter:
    """Fake Plane.req: answers by path from fixtures; a 404 entry raises ApiError(404)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        value = self.routes[path]
        if value == 404:
            raise st.ApiError(404, '{"error": "not found"}', "GET", path)
        return value


class GitLabLoaderTest(unittest.TestCase):
    def test_periods(self):
        gl = gitlab()
        with mock.patch.object(st.GitLab, "gql", return_value=fixture("gitlab_milestones.json")["data"]) as gql:
            periods = gl.periods(TODAY)
        self.assertEqual(gl.name, "Demo Project")
        self.assertEqual(periods[0], {"name": "Sprint 5", "start": "2026-03-01", "end": "2026-03-14"})
        self.assertEqual(periods[2], {"name": "Someday", "start": None, "end": None})
        self.assertEqual(gql.call_args.args[1], {"p": "demo/app"})  # the path goes in as a variable, not into a URL

    def test_missing_project_exits_with_a_message(self):
        for call in (lambda gl: gl.periods(TODAY), lambda gl: gl.issues(None)):
            with mock.patch.object(st.GitLab, "gql", return_value={"project": None}):
                with self.assertRaises(SystemExit) as cm:
                    call(gitlab())
            self.assertIn("not found", str(cm.exception.code))
            self.assertNotIn("glpat-secret", str(cm.exception.code))

    def test_issues_paginate_and_normalize(self):
        pages = [fixture("gitlab_work_items_page1.json")["data"], fixture("gitlab_work_items_page2.json")["data"]]
        with mock.patch.object(st.GitLab, "gql", side_effect=pages) as gql:
            issues = gitlab().issues("Sprint 5")
        self.assertEqual(gql.call_count, 2)
        self.assertEqual(gql.call_args_list[0].args[1], {"p": "demo/app", "after": None, "ms": ["Sprint 5"]})
        self.assertEqual(gql.call_args_list[1].args[1], {"p": "demo/app", "after": "CURSOR1", "ms": ["Sprint 5"]})
        by_key = {i["key"]: i for i in issues}
        self.assertEqual(list(by_key), ["#1", "#2", "#3", "#4"])
        self.assertEqual(by_key["#1"], st.new_issue(
            key="#1", title="Checkout flow", url="https://gitlab.example.com/demo/app/-/issues/1",
            created="2026-01-10T09:00:00Z", updated="2026-02-20T10:00:00Z", status="in-progress", started=True,
            blocked=False, stale_mark=False, assignees=["dev-a"], priority="high", points=5.0, period="Sprint 5"))
        two = by_key["#2"]
        self.assertEqual((two["status"], two["started"], two["blocked"], two["points"]), ("blocked", False, True, 2.5))
        self.assertEqual(two["assignees"], ["dev-a", "dev-b"])
        three = by_key["#3"]
        self.assertEqual((three["status"], three["stale_mark"], three["started"]), ("to-do", True, False))
        self.assertIsNone(three["period"])       # milestone: null
        self.assertEqual(three["assignees"], [])  # no assignees widget
        self.assertIsNone(three["priority"])
        four = by_key["#4"]
        self.assertEqual((four["status"], four["started"], four["blocked"]), ("review", True, True))  # unscoped Blocked
        self.assertIsNone(four["points"])         # estimate::XL is not a number
        self.assertEqual(four["priority"], "low")
        self.assertEqual(st.kind_of(four), "blocked")

    def test_whole_project_passes_no_milestone_filter(self):
        page = {"project": {"name": "P", "workItems": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}
        with mock.patch.object(st.GitLab, "gql", return_value=page) as gql:
            self.assertEqual(gitlab().issues(None), [])
        self.assertIsNone(gql.call_args.args[1]["ms"])

    def test_graphql_errors_raise_api_error(self):
        with mock.patch.object(st, "http", return_value=({"errors": [{"message": "boom"}]}, {})):
            with self.assertRaises(st.ApiError):
                gitlab().gql("query { x }")

    def test_gql_sends_the_token_only_in_headers(self):
        with mock.patch.object(st, "http", return_value=({"data": {"ok": 1}}, {})) as http:
            self.assertEqual(gitlab().gql("query { x }", {"p": "a/b"}), {"ok": 1})
        method, url, headers, body = http.call_args.args
        self.assertEqual((method, url), ("POST", "https://gitlab.example.com/api/graphql"))
        self.assertEqual(headers, {"PRIVATE-TOKEN": "glpat-secret"})
        self.assertNotIn("glpat-secret", json.dumps(body))

    def test_my_projects_follows_rest_pages_and_skips_deleted(self):
        responses = [([{"path_with_namespace": "g/a", "name": "A", "web_url": "wa"},
                       {"path_with_namespace": "g/gone", "name": "Gone", "web_url": "wg",
                        "marked_for_deletion_on": "2026-01-01"}], {"X-Next-Page": "2"}),
                     ([{"path_with_namespace": "g/b", "name": "B", "web_url": "wb"}], {"X-Next-Page": ""})]
        with mock.patch.object(st, "http", side_effect=responses) as http:
            rows = gitlab().my_projects()
        self.assertEqual([r["path"] for r in rows], ["g/a", "g/b"])
        urls = [c.args[1] for c in http.call_args_list]
        self.assertIn("page=2", urls[1])
        self.assertTrue(all("glpat-secret" not in u for u in urls))


class PlaneLoaderTest(unittest.TestCase):
    def routes(self):
        return {
            "/projects/p-1/cycles/": fixture("plane_cycles.json"),
            "/projects/p-1/labels/": fixture("plane_labels.json"),
            "/projects/p-1/cycles/c-2/cycle-issues/": fixture("plane_cycle_issues_c2.json"),
            "/projects/p-1/issues/": fixture("plane_cycle_issues_c2.json"),
        }

    def load(self, routes, period="Sprint 5"):
        p = plane()
        router = PlaneRouter(routes)
        with mock.patch.object(st.Plane, "req", side_effect=router):
            periods = p.periods(TODAY)
            issues = p.issues(period)
        return p, periods, {i["key"]: i for i in issues}, router

    def test_periods_keep_open_cycles_only(self):
        _, periods, _, _ = self.load(self.routes())
        # completed (Sprint 4), archived and past their end date (Ended yesterday) are dropped;
        # a cycle ending today is still open
        self.assertEqual([c["name"] for c in periods], ["Sprint 5", "Sprint 6", "Ends today", "Draft"])
        self.assertEqual(periods[0], {"name": "Sprint 5", "id": "c-2", "start": "2026-03-01", "end": "2026-03-14"})
        self.assertEqual(periods[3], {"name": "Draft", "id": "c-7", "start": None, "end": None})

    def test_issues_skip_closed_archived_and_drafts(self):
        _, _, by_key, router = self.load(self.routes())
        self.assertEqual(sorted(by_key), ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-8"])
        cycle_calls = [c for c in router.calls if c[0].endswith("cycle-issues/")]
        self.assertEqual(cycle_calls[0][1]["expand"], "state,labels,assignees,estimate_point")

    def test_issues_normalized_with_estimate_points(self):
        _, _, by_key, _ = self.load(self.routes())
        self.assertEqual(by_key["DEMO-1"], st.new_issue(
            key="DEMO-1", title="Checkout flow", url="https://plane.example.com/demo-ws/projects/p-1/issues/i-1",
            created="2026-01-10T09:00:00Z", updated="2026-02-20T10:00:00Z", status="In Progress", started=True,
            blocked=False, stale_mark=False, assignees=["dev-a"], priority="high",
            points=5.0,  # from the Estimate field, not the points::8 label
            period="Sprint 5"))

    def test_blocked_state_is_not_started_work(self):
        two = self.load(self.routes())[2]["DEMO-2"]
        self.assertEqual((two["status"], two["started"], two["blocked"]), ("Blocked", False, True))
        self.assertIsNone(two["points"])        # estimate value "XL"
        self.assertIsNone(two["priority"])      # "none"
        self.assertEqual(two["assignees"], ["b@example.com"])  # empty display name falls back to email

    def test_stale_label_and_state(self):
        _, _, by_key, _ = self.load(self.routes())
        three = by_key["DEMO-3"]
        self.assertTrue(three["stale_mark"])     # label "Stale"
        self.assertIsNone(three["points"])       # no estimate point
        self.assertEqual(st.kind_of(three), "marked stale")
        eight = by_key["DEMO-8"]
        self.assertTrue(eight["stale_mark"])     # state "Stale"
        self.assertTrue(eight["blocked"])        # label status::on hold
        self.assertEqual(eight["points"], 0.5)   # " 0.5 " trimmed
        self.assertEqual(eight["priority"], "urgent")
        self.assertEqual(eight["assignees"], ["dev-a"])  # unexpanded ids are ignored

    def test_whole_project_lists_project_issues(self):
        _, _, by_key, router = self.load(self.routes(), period=None)
        self.assertIn(("/projects/p-1/issues/", {"per_page": 100, "expand": "state,labels,assignees,estimate_point"}), router.calls)
        self.assertIsNone(by_key["DEMO-1"]["period"])

    def test_unknown_cycle_exits_with_the_list(self):
        with self.assertRaises(SystemExit) as cm:
            self.load(self.routes(), period="Nope")
        self.assertIn("cycle 'Nope' not found", str(cm.exception.code))
        self.assertIn("Sprint 5", str(cm.exception.code))

    def test_cycle_matched_by_id_or_name_case_insensitive(self):
        for period in ("c-2", "  sprint 5 "):
            self.assertEqual(len(self.load(self.routes(), period=period)[2]), 4)

    def test_estimates_endpoint_is_never_called(self):
        # Plane CE does not register it (makeplane/plane#9933); expand works with or without that bug.
        for period in ("Sprint 5", None):
            _, _, _, router = self.load(self.routes(), period=period)
            self.assertFalse([c for c in router.calls if "/estimates/" in c[0]])

    def test_estimate(self):
        def point(value):
            return st.estimate({"estimate_point": {"value": value}})

        self.assertEqual(point("3"), 3.0)
        self.assertEqual(point(" 0.5 "), 0.5)
        self.assertIsNone(point("XL"))
        self.assertIsNone(point(""))    # how Plane expands an unset estimate
        self.assertIsNone(point(None))
        self.assertIsNone(st.estimate({"estimate_point": "ep-3"}))  # not expanded
        self.assertIsNone(st.estimate({}))

    def test_my_projects_skips_archived_and_non_member(self):
        p = plane()
        p.all_projects = fixture("plane_projects.json")["results"]
        self.assertEqual(p.my_projects(), [{"path": "DEMO", "name": "Demo",
                                            "url": "https://plane.example.com/demo-ws/projects/p-1/issues"}])


class PlaneClientTest(unittest.TestCase):
    def test_list_follows_cursor_pages(self):
        pages = [{"results": [1, 2], "next_page_results": True, "next_cursor": "100:1:0"},
                 {"results": [3], "next_page_results": False, "next_cursor": "100:2:0"}]
        with mock.patch.object(st.Plane, "req", side_effect=pages) as req:
            self.assertEqual(plane().list("/x/", {"expand": "state"}), [1, 2, 3])
        self.assertEqual(req.call_args_list[1].args[1], {"per_page": 100, "expand": "state", "cursor": "100:1:0"})

    def test_list_ends_without_cursor_and_accepts_plain_lists(self):
        with mock.patch.object(st.Plane, "req", return_value={"results": [1], "next_page_results": True}):
            self.assertEqual(plane().list("/x/"), [1])
        with mock.patch.object(st.Plane, "req", return_value=[{"a": 1}]):
            self.assertEqual(plane().list("/x/"), [{"a": 1}])

    def test_req_encodes_params_and_keeps_the_key_in_headers(self):
        with mock.patch.object(st, "http", return_value=({"ok": True}, {})) as http:
            plane().req("/projects/", {"q": "a b&c"})
        method, url, headers = http.call_args.args
        self.assertEqual(url, "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?q=a+b%26c")
        self.assertEqual(headers, {"X-API-Key": "plane-secret"})
        self.assertNotIn("plane-secret", url)

    def test_req_retries_429(self):
        err = st.ApiError(429, "slow down", "GET", "u")
        with mock.patch.object(st, "http", side_effect=[err, err, ({"ok": 1}, {})]) as http, \
                mock.patch.object(st.time, "sleep") as sleep:
            self.assertEqual(plane().req("/x/"), {"ok": 1})
        self.assertEqual(http.call_count, 3)
        self.assertIn(mock.call(10), sleep.call_args_list)

    def test_req_429_is_bounded(self):
        with mock.patch.object(st, "http", side_effect=st.ApiError(429, "slow", "GET", "u")) as http, \
                mock.patch.object(st.time, "sleep"):
            with self.assertRaises(st.ApiError) as cm:
                plane().req("/x/")
        self.assertEqual(http.call_count, 8)
        self.assertEqual(cm.exception.status, 429)

    def test_req_retries_5xx_three_times(self):
        with mock.patch.object(st, "http", side_effect=st.ApiError(502, "bad gateway", "GET", "u")) as http, \
                mock.patch.object(st.time, "sleep"):
            with self.assertRaises(st.ApiError) as cm:
                plane().req("/x/")
        self.assertEqual(http.call_count, 4)
        self.assertEqual(cm.exception.status, 502)

    def test_req_does_not_retry_4xx(self):
        for status in (401, 403, 404):
            with mock.patch.object(st, "http", side_effect=st.ApiError(status, "no", "GET", "u")) as http, \
                    mock.patch.object(st.time, "sleep"):
                with self.assertRaises(st.ApiError):
                    plane().req("/x/")
            self.assertEqual(http.call_count, 1)


class PlaneConstructorTest(unittest.TestCase):
    def make(self, project, workspace="demo-ws"):
        with mock.patch.object(st.Plane, "list", return_value=fixture("plane_projects.json")["results"]):
            return st.Plane("https://plane.example.com/", workspace, "plane-secret", project)

    def test_matches_identifier_name_or_id(self):
        for project in ("DEMO", "demo", " Demo ", "p-1"):
            p = self.make(project)
            self.assertEqual((p.pid, p.ident, p.name), ("p-1", "DEMO", "Demo (DEMO)"))

    def test_no_project_for_the_projects_command(self):
        p = self.make(None)
        self.assertFalse(hasattr(p, "pid"))
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/demo-ws")

    def test_unknown_project_exits_with_the_list_and_no_key(self):
        with self.assertRaises(SystemExit) as cm:
            self.make("NOPE")
        msg = str(cm.exception.code)
        self.assertIn("'NOPE' not found", msg)
        self.assertIn("Demo (DEMO)", msg)
        self.assertNotIn("plane-secret", msg)

    def test_workspace_is_quoted_in_the_api_url(self):
        # Regression: the workspace (from the chat via --workspace) went into the URL unquoted,
        # so "a/../b?x=1" could change the path and the query string.
        p = self.make(None, workspace="a/../b?x=1")
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/a%2F..%2Fb%3Fx%3D1")


class HttpTest(unittest.TestCase):
    def response(self, body, status=200):
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.status, resp.headers = status, {}
        resp.__enter__.return_value = resp
        return resp

    def test_sets_a_timeout_and_parses_json(self):
        with mock.patch.object(st.urllib.request, "urlopen", return_value=self.response(b'{"a": 1}')) as urlopen:
            data, _ = st.http("GET", "https://x.example.com/api", {"X-API-Key": "k"})
        self.assertEqual(data, {"a": 1})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    def test_http_error_becomes_api_error_without_headers(self):
        err = urllib.error.HTTPError("https://x.example.com/api", 401, "Unauthorized", {},
                                     io.BytesIO(b'{"detail": "bad token"}'))
        with mock.patch.object(st.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(st.ApiError) as cm:
                st.http("GET", "https://x.example.com/api", {"PRIVATE-TOKEN": "glpat-secret"})
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("bad token", str(cm.exception))
        self.assertNotIn("glpat-secret", str(cm.exception))

    def test_unreachable_host_exits_with_a_message(self):
        with mock.patch.object(st.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SystemExit) as cm:
                st.http("GET", "https://x.example.com/api", {"PRIVATE-TOKEN": "glpat-secret"})
        self.assertIn("cannot reach https://x.example.com/api", str(cm.exception.code))
        self.assertNotIn("glpat-secret", str(cm.exception.code))

    def test_non_json_response_is_an_api_error(self):
        # Regression: a wrong URL that answers with an HTML page raised JSONDecodeError (a traceback).
        with mock.patch.object(st.urllib.request, "urlopen", return_value=self.response(b"<html>login</html>")):
            with self.assertRaises(st.ApiError) as cm:
                st.http("GET", "https://x.example.com/api", {"X-API-Key": "plane-secret"})
        self.assertIn("not JSON", str(cm.exception))
        self.assertNotIn("plane-secret", str(cm.exception))

    def test_read_timeout_exits_with_a_message(self):
        # Regression: a timeout while reading the body is not a URLError and ended in a traceback.
        resp = self.response(b"")
        resp.read.side_effect = TimeoutError("timed out")
        with mock.patch.object(st.urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(SystemExit) as cm:
                st.http("GET", "https://x.example.com/api", {"X-API-Key": "plane-secret"})
        self.assertIn("cannot reach https://x.example.com/api", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
