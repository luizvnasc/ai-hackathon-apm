"""GitLab and Plane clients and loaders of assign.py, fed with fixtures (no network)."""
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import load

asg = load("assign-issues", "assign.py")
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TOKEN, API_KEY = "glpat-secret-123", "plane-secret-456"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gitlab(project="demo/app"):
    gl = asg.GitLab.__new__(asg.GitLab)
    gl.url, gl.headers, gl.project = "https://gitlab.example.com", {"PRIVATE-TOKEN": TOKEN}, project
    return gl


def plane():
    p = asg.Plane.__new__(asg.Plane)
    p.url, p.ws = "https://plane.example.com", "demo-ws"
    p.base = "https://plane.example.com/api/v1/workspaces/demo-ws"
    p.headers = {"X-API-Key": API_KEY}
    p.min_interval, p.last = 0.0, 0.0
    p.pid, p.ident, p.project = "p-1", "DEMO", "Demo (DEMO)"
    return p


class PlaneRouter:
    """Fake Plane.req(method, path, params, body): answers GETs by path from fixtures.
    A 404 entry raises ApiError(404); a callable entry gets the params (for cursors)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, path, params=None, body=None):
        self.calls.append((method, path, dict(params or {}), body))
        if method != "GET":
            return {}
        value = self.routes[path]
        if callable(value):
            value = value(params or {})
        if isinstance(value, int):
            raise asg.ApiError(value, '{"error": "x"}', method, path)
        return value

    def paths(self, method="GET"):
        return [c[1] for c in self.calls if c[0] == method]


def plane_routes(comments=True):
    pages = {None: fixture("plane_issues.json"), "100:1:0": fixture("plane_issues_page2.json")}
    routes = {
        "/members/": fixture("plane_workspace_members.json"),
        "/projects/p-1/members/": fixture("plane_project_members.json"),
        "/projects/p-1/cycles/": fixture("plane_cycles.json"),
        "/projects/p-1/cycles/c-1/cycle-issues/": {"results": [{"id": "i-1"}, {"id": "i-2"}]},
        "/projects/p-1/cycles/c-2/cycle-issues/": [{"id": "i-3"}],
        "/projects/p-1/cycles/c-3/cycle-issues/": {"results": []},
        "/projects/p-1/cycles/c-4/cycle-issues/": {"results": []},
        "/projects/p-1/issues/": lambda params: pages[params.get("cursor")],
        "/projects/p-1/labels/": fixture("plane_labels.json"),
        "/projects/p-1/issues/i-1/comments/": fixture("plane_comments_i1.json"),
        "/projects/p-1/issues/i-2/comments/": {"results": []},
        "/projects/p-1/issues/i-3/comments/": {"results": []},
        "/projects/p-1/issues/i-5/comments/": {"results": []},
    }
    return routes


# ------------------------------------------------------------------ GitLab
class GitLabLoaderTest(unittest.TestCase):
    def issues(self, comments=True):
        pages = [fixture("gitlab_work_items_page1.json")["data"], fixture("gitlab_work_items_page2.json")["data"]]
        with mock.patch.object(asg.GitLab, "gql", side_effect=pages) as gql:
            issues = gitlab().issues(comments=comments)
        return {i["key"]: i for i in issues}, gql

    def test_issues_paginate_and_normalize(self):
        by_key, gql = self.issues()
        self.assertEqual(gql.call_count, 2)
        self.assertEqual(gql.call_args_list[1].args[1], {"p": "demo/app", "after": "CURSOR1"})
        self.assertEqual(list(by_key), ["#1", "#2", "#3", "#4"])
        self.assertEqual(by_key["#1"], asg.new_issue(
            key="#1", iid=1, title="Add login API", url="https://gitlab.example.com/demo/app/-/issues/1",
            open=False, cancelled=False, status="done",
            labels=["Area::Backend", "type::feature", "points::3", "workflow::done", "priority::high"],
            areas=["backend"], types=["feature"], priority="high", points=3.0, assignees=["dev-a"],
            author="dev-b", commenters=["dev-c"], period="v0.1",
            updated="2026-01-10T15:00:00Z", closed="2026-01-10T15:00:00Z"))

    def test_cancelled_multi_area_blocked_and_sparse_items(self):
        by_key, _ = self.issues()
        two = by_key["#2"]
        self.assertTrue(two["cancelled"])
        self.assertEqual(two["areas"], ["backend", "frontend"])
        self.assertEqual(two["status"], "cancelled")
        three = by_key["#3"]
        self.assertTrue(three["open"])
        self.assertEqual(three["points"], 1.5)  # sp:: scope
        self.assertIsNone(three["period"])
        self.assertTrue(gitlab().blocked(three))
        four = by_key["#4"]
        self.assertEqual((four["status"], four["points"], four["author"]), ("open", None, None))
        self.assertEqual((four["areas"], four["assignees"], four["commenters"]), ([], [], []))
        self.assertFalse(gitlab().blocked(four))

    def test_no_comments_leaves_the_notes_widget_out_of_the_query(self):
        _, gql = self.issues(comments=False)
        self.assertNotIn("WorkItemWidgetNotes", gql.call_args_list[0].args[0])
        _, gql = self.issues(comments=True)
        self.assertIn("WorkItemWidgetNotes", gql.call_args_list[0].args[0])

    def test_periods(self):
        with mock.patch.object(asg.GitLab, "gql", return_value=fixture("gitlab_milestones.json")["data"]):
            periods = gitlab().periods()
        self.assertEqual(periods, [{"name": "v0.2", "start": "2026-02-16", "end": "2026-03-08"},
                                   {"name": "Backlog", "start": None, "end": None}])

    def test_missing_project_exits_with_a_message(self):
        for call in ("periods", "issues"):
            with mock.patch.object(asg.GitLab, "gql", return_value={"project": None}):
                with self.assertRaises(SystemExit) as cm:
                    getattr(gitlab(), call)()
            self.assertIn("not found", str(cm.exception.code))
            self.assertNotIn(TOKEN, str(cm.exception.code))

    def test_members_skip_blocked_users_and_bots(self):
        with mock.patch.object(asg, "http", return_value=(fixture("gitlab_members.json"), {})) as http:
            gl = gitlab()
            members = gl.members()
            self.assertEqual([m["name"] for m in members], ["dev-a", "dev-b", "lead"])
            self.assertEqual([m["name"] for m in asg.developers(gl, "gitlab", ["developer"])], ["dev-a", "dev-b"])
            self.assertEqual([m["name"] for m in asg.developers(gl, "gitlab", ["developer", "maintainer"])],
                             ["dev-a", "dev-b", "lead"])
        self.assertIn("/projects/demo%2Fapp/members/all?", http.call_args.args[1])

    def test_project_path_is_url_encoded(self):
        self.assertEqual(gitlab("grp/sub proj").pid(), "grp%2Fsub%20proj")
        self.assertEqual(gitlab("a/b?x=1#y").pid(), "a%2Fb%3Fx%3D1%23y")

    def test_my_projects_follow_pages_and_skip_deleted(self):
        responses = [([{"path_with_namespace": "g/a", "name": "A", "web_url": "wa", "open_issues_count": 2},
                       {"path_with_namespace": "g/gone", "name": "Gone", "web_url": "wg",
                        "marked_for_deletion_on": "2026-01-01"}], {"X-Next-Page": "2"}),
                     ([{"path_with_namespace": "g/b", "name": "B", "web_url": "wb"}], {"X-Next-Page": ""})]
        with mock.patch.object(asg, "http", side_effect=responses) as http:
            rows = gitlab().my_projects()
        self.assertEqual([(r["path"], r["open_issues"]) for r in rows], [("g/a", 2), ("g/b", None)])
        urls = [c.args[1] for c in http.call_args_list]
        self.assertIn("page=2", urls[1])
        self.assertTrue(all(TOKEN not in u for u in urls))

    def test_normalize_key(self):
        gl = gitlab()
        for key in ("#45", "45", " 45 ", "https://gitlab.example.com/demo/app/-/issues/45",
                    "https://gitlab.example.com/demo/app/-/work_items/45"):
            self.assertEqual(gl.normalize_key(key), "#45", key)
        self.assertEqual(gl.normalize_key("abc"), "abc")

    def test_graphql_errors_raise_api_error(self):
        with mock.patch.object(asg, "http", return_value=({"errors": [{"message": "boom"}]}, {})):
            with self.assertRaises(asg.ApiError):
                gitlab().gql("query { x }")


class GitLabAssignTest(unittest.TestCase):
    MEMBERS = [{"id": 1, "name": "dev-a"}, {"id": 2, "name": "dev-b"}]

    def run_assign(self, it, member, replace, current=None):
        calls = []

        def rest(method, path, params=None, body=None):
            calls.append((method, path, body))
            return current if method == "GET" else {}

        gl = gitlab()
        with mock.patch.object(gl, "rest", side_effect=rest):
            gl.assign(it, member, self.MEMBERS, replace)
        return calls

    def test_add_keeps_the_current_assignees(self):
        it = asg.new_issue(key="#7", iid=7, assignees=["dev-a"])
        calls = self.run_assign(it, self.MEMBERS[1], False, {"assignees": [{"id": 1, "username": "dev-a"}]})
        self.assertEqual(calls[-1], ("PUT", "/projects/demo%2Fapp/issues/7", {"assignee_ids": [1, 2]}))
        self.assertEqual([c[0] for c in calls].count("PUT"), 1)

    def test_add_keeps_assignees_who_are_no_longer_active_members(self):
        # Regression: the ids were rebuilt from the active member list, so adding a developer
        # silently removed an assignee who had left the project or been blocked.
        it = asg.new_issue(key="#7", iid=7, assignees=["dev-a", "former"])
        current = {"assignees": [{"id": 1, "username": "dev-a"}, {"id": 99, "username": "former"}]}
        calls = self.run_assign(it, self.MEMBERS[1], False, current)
        self.assertEqual(calls[-1][2], {"assignee_ids": [1, 99, 2]})

    def test_replace_sets_only_the_new_member(self):
        it = asg.new_issue(key="#7", iid=7, assignees=["dev-a"])
        calls = self.run_assign(it, self.MEMBERS[1], True)
        self.assertEqual(calls, [("PUT", "/projects/demo%2Fapp/issues/7", {"assignee_ids": [2]})])


class HttpTest(unittest.TestCase):
    def test_http_error_becomes_api_error_without_headers(self):
        err = urllib.error.HTTPError("https://gitlab.example.com/api/v4/x", 401, "Unauthorized", {},
                                     io.BytesIO(b'{"message": "401 Unauthorized"}'))
        with mock.patch.object(asg.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(asg.ApiError) as cm:
                asg.http("GET", "https://gitlab.example.com/api/v4/x", {"PRIVATE-TOKEN": TOKEN})
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("401", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_unreachable_host_exits_with_a_message(self):
        with mock.patch.object(asg.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SystemExit) as cm:
                asg.http("GET", "https://gitlab.example.com/api/v4/x", {"PRIVATE-TOKEN": TOKEN})
        self.assertIn("cannot reach", str(cm.exception.code))
        self.assertNotIn(TOKEN, str(cm.exception.code))

    def test_every_request_has_a_timeout(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"[]"
        with mock.patch.object(asg.urllib.request, "urlopen", return_value=resp) as urlopen:
            asg.http("GET", "https://x.example.com", {})
        self.assertGreater(urlopen.call_args.kwargs["timeout"], 0)

    def test_non_json_response_exits_with_a_message(self):
        # Regression: a wrong URL that serves an HTML page (e.g. the Plane web app) ended in a
        # JSONDecodeError traceback instead of a clear message.
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"<!doctype html><html></html>"
        with mock.patch.object(asg.urllib.request, "urlopen", return_value=resp):
            with self.assertRaises(SystemExit) as cm:
                asg.http("GET", "https://plane.example.com/api/v1/workspaces/w/projects/", {"X-API-Key": API_KEY})
        self.assertIn("not JSON", str(cm.exception.code))
        self.assertNotIn(API_KEY, str(cm.exception.code))


# ------------------------------------------------------------------ Plane
class PlaneLoaderTest(unittest.TestCase):
    def load(self, routes, comments=True):
        p = plane()
        router = PlaneRouter(routes)
        with mock.patch.object(asg.Plane, "req", side_effect=router):
            members = p.members()
            periods = p.periods()
            issues = p.issues(comments=comments)
        return p, members, periods, {i["key"]: i for i in issues}, router

    def test_members_with_workspace_roles(self):
        _, members, _, _, _ = self.load(plane_routes())
        self.assertEqual([(m["name"], m["level"]) for m in members],
                         [("dev-a", 15), ("b@example.com", 15), ("owner", 20), ("left-workspace", 0)])
        self.assertEqual(members[0]["display"], "Dev A")
        self.assertIsNone(members[1]["display"])
        self.assertEqual(members[2]["display"], "Own")

    def test_developers_are_members_role_by_default(self):
        p, _, _, _, _ = self.load(plane_routes())
        with mock.patch.object(asg.Plane, "req", side_effect=PlaneRouter(plane_routes())):
            self.assertEqual([m["name"] for m in asg.developers(p, "plane", ["developer"])], ["dev-a", "b@example.com"])
            self.assertEqual([m["name"] for m in asg.developers(p, "plane", ["member", "admin"])],
                             ["dev-a", "b@example.com", "owner"])

    def test_periods_drop_completed_and_archived_and_trim_dates(self):
        _, _, periods, _, _ = self.load(plane_routes())
        self.assertEqual(periods, [{"name": "Cycle 2", "id": "c-2", "start": "2026-02-16", "end": "2026-03-08"},
                                   {"name": "Draft", "id": "c-3", "start": None, "end": None}])

    def test_issues_follow_the_cursor_and_skip_drafts(self):
        _, _, _, by_key, router = self.load(plane_routes())
        self.assertEqual(sorted(by_key), ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-5"])
        issue_calls = [c for c in router.calls if c[1] == "/projects/p-1/issues/"]
        self.assertEqual([c[2].get("cursor") for c in issue_calls], [None, "100:1:0"])
        self.assertEqual(issue_calls[0][2]["expand"], "state,labels,assignees,estimate_point")

    def test_issue_normalized_with_label_groups_and_estimate_points(self):
        _, _, _, by_key, _ = self.load(plane_routes())
        one = by_key["DEMO-1"]
        self.assertEqual(one["labels"], ["area::backend", "type::feature", "points::8"])
        self.assertEqual((one["areas"], one["types"]), (["backend"], ["feature"]))
        self.assertEqual(one["points"], 3.0)  # from the Estimate field, not the points::8 label
        self.assertEqual((one["open"], one["cancelled"], one["status"]), (False, False, "Done"))
        self.assertEqual(one["priority"], "high")
        self.assertEqual(one["assignees"], ["dev-a", "b@example.com"])
        self.assertEqual(one["assignee_ids"], ["u-a", "u-b"])
        self.assertEqual(one["author"], "b@example.com")
        self.assertEqual(one["commenters"], ["owner", "dev-a"])  # deduplicated, unknown actor dropped
        self.assertEqual(one["period"], "Cycle 1")
        self.assertEqual(one["url"], "https://plane.example.com/demo-ws/projects/p-1/issues/i-1")
        self.assertEqual(one["closed"], "2026-01-10T15:00:00Z")

    def test_cancelled_non_numeric_estimate_and_group_label_on_the_issue(self):
        _, _, _, by_key, _ = self.load(plane_routes())
        two = by_key["DEMO-2"]
        self.assertTrue(two["cancelled"])
        self.assertFalse(two["open"])
        self.assertIsNone(two["points"])       # estimate value "XL"
        self.assertIsNone(two["priority"])     # "none"
        self.assertEqual(two["areas"], ["frontend"])
        self.assertEqual(two["labels"], ["area", "area::frontend"])
        three = by_key["DEMO-3"]
        self.assertEqual(three["points"], 0.5)  # " 0.5 " trimmed
        self.assertEqual(three["period"], "Cycle 2")
        self.assertEqual(three["author"], "left-workspace")
        self.assertTrue(plane().blocked(three))  # "Blocked" label
        five = by_key["DEMO-5"]                  # unexpanded state/labels/assignees
        self.assertTrue(five["open"])
        self.assertEqual((five["labels"], five["assignees"], five["points"]), ([], [], None))
        self.assertFalse(plane().blocked(five))

    def test_label_parents_are_read_once(self):
        _, _, _, _, router = self.load(plane_routes())
        self.assertEqual(router.paths().count("/projects/p-1/labels/"), 1)

    def test_estimates_endpoint_is_never_called(self):
        # Plane CE does not register it (makeplane/plane#9933); expand works with or without that bug.
        _, _, _, _, router = self.load(plane_routes())
        self.assertFalse([p for p in router.paths() if "/estimates/" in p])

    def test_estimate(self):
        def point(value):
            return asg.estimate({"estimate_point": {"value": value}})

        self.assertEqual(point("3"), 3.0)
        self.assertEqual(point(" 0.5 "), 0.5)
        self.assertIsNone(point("XL"))
        self.assertIsNone(point(""))    # how Plane expands an unset estimate
        self.assertIsNone(point(None))
        self.assertIsNone(asg.estimate({"estimate_point": "ep-3"}))  # not expanded
        self.assertIsNone(asg.estimate({}))

    def test_no_comments_reads_no_comment_endpoint(self):
        _, _, _, by_key, router = self.load(plane_routes(), comments=False)
        self.assertFalse([p for p in router.paths() if p.endswith("/comments/")])
        self.assertEqual(by_key["DEMO-1"]["commenters"], [])

    def test_issues_load_members_when_needed(self):
        p = plane()
        with mock.patch.object(asg.Plane, "req", side_effect=PlaneRouter(plane_routes())):
            issues = p.issues()
        self.assertEqual(len(issues), 4)
        self.assertEqual(issues[0]["author"], "b@example.com")

    def test_blocked_by_state_name(self):
        self.assertTrue(plane().blocked(asg.new_issue(status="On Hold")))
        self.assertFalse(plane().blocked(asg.new_issue(status="In Progress", labels=["area::backend"])))

    def test_normalize_key(self):
        p = plane()
        for key in ("DEMO-45", "INV-45", "45", " 45 "):
            self.assertEqual(p.normalize_key(key), "DEMO-45", key)


class PlaneClientTest(unittest.TestCase):
    def test_init_resolves_project_by_identifier_name_or_id(self):
        projects = fixture("plane_projects.json")["results"]
        for project in ("demo", "DEMO", "Demo", "p-1"):
            with mock.patch.object(asg.Plane, "list", return_value=projects):
                p = asg.Plane("https://plane.example.com/", "demo-ws", API_KEY, project)
            self.assertEqual((p.pid, p.ident, p.project), ("p-1", "DEMO", "Demo (DEMO)"))

    def test_unknown_project_lists_the_projects(self):
        with mock.patch.object(asg.Plane, "list", return_value=fixture("plane_projects.json")["results"]):
            with self.assertRaises(SystemExit) as cm:
                asg.Plane("https://plane.example.com", "demo-ws", API_KEY, "NOPE")
        self.assertIn("not found", str(cm.exception.code))
        self.assertIn("Demo (DEMO)", str(cm.exception.code))
        self.assertNotIn(API_KEY, str(cm.exception.code))

    def test_workspace_is_url_encoded(self):
        # Regression: the workspace slug went into the base URL as typed.
        with mock.patch.object(asg.Plane, "list", return_value=[]):
            p = asg.Plane("https://plane.example.com", "my ws/../x?y", API_KEY, None)
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/my%20ws%2F..%2Fx%3Fy")

    def test_my_projects_skip_archived_and_non_member(self):
        with mock.patch.object(asg.Plane, "list", return_value=fixture("plane_projects.json")["results"]):
            p = asg.Plane("https://plane.example.com", "demo-ws", API_KEY, None)
        rows = p.my_projects()
        self.assertEqual([r["path"] for r in rows], ["DEMO"])
        self.assertEqual(rows[0]["url"], "https://plane.example.com/demo-ws/projects/p-1/issues")

    def test_list_follows_cursor_until_the_last_page(self):
        p = plane()
        pages = [{"results": [1, 2], "next_page_results": True, "next_cursor": "c2"},
                 {"results": [3], "next_page_results": True, "next_cursor": None}]
        with mock.patch.object(asg.Plane, "req", side_effect=pages) as req:
            self.assertEqual(p.list("/x/"), [1, 2, 3])
        self.assertEqual(req.call_args_list[1].args[2]["cursor"], "c2")
        with mock.patch.object(asg.Plane, "req", return_value=[4, 5]):
            self.assertEqual(p.list("/x/"), [4, 5])

    def test_req_retries_429_then_succeeds(self):
        p = plane()
        side = [asg.ApiError(429, "slow down", "GET", "u"), ({"ok": 1}, {})]
        with mock.patch.object(asg, "http", side_effect=side) as http, mock.patch.object(asg.time, "sleep") as sleep:
            self.assertEqual(p.req("GET", "/x/"), {"ok": 1})
        self.assertEqual(http.call_count, 2)
        sleep.assert_any_call(10)

    def test_req_429_retries_are_bounded(self):
        p = plane()
        with mock.patch.object(asg, "http", side_effect=asg.ApiError(429, "slow", "GET", "u")) as http, \
                mock.patch.object(asg.time, "sleep"):
            with self.assertRaises(asg.ApiError) as cm:
                p.req("GET", "/x/")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(http.call_count, 8)

    def test_req_5xx_retries_three_times_then_raises(self):
        p = plane()
        with mock.patch.object(asg, "http", side_effect=asg.ApiError(502, "bad gateway", "GET", "u")) as http, \
                mock.patch.object(asg.time, "sleep"):
            with self.assertRaises(asg.ApiError) as cm:
                p.req("GET", "/x/")
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(http.call_count, 4)

    def test_req_4xx_is_not_retried(self):
        p = plane()
        with mock.patch.object(asg, "http", side_effect=asg.ApiError(401, "no", "GET", "u")) as http, \
                mock.patch.object(asg.time, "sleep"):
            with self.assertRaises(asg.ApiError):
                p.req("GET", "/x/")
        self.assertEqual(http.call_count, 1)

    def test_req_sends_the_key_only_as_a_header(self):
        p = plane()
        with mock.patch.object(asg, "http", return_value=({}, {})) as http:
            p.req("GET", "/projects/", {"per_page": 100})
        url, headers = http.call_args.args[1], http.call_args.args[2]
        self.assertEqual(url, "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100")
        self.assertNotIn(API_KEY, url)
        self.assertEqual(headers, {"X-API-Key": API_KEY})


class PlaneAssignTest(unittest.TestCase):
    def run_assign(self, it, replace):
        p = plane()
        router = PlaneRouter({})
        with mock.patch.object(asg.Plane, "req", side_effect=router):
            p.assign(it, {"id": "u-b", "name": "b@example.com"}, [], replace)
        return router.calls

    def test_add_keeps_the_current_assignees(self):
        it = asg.new_issue(key="DEMO-3", id="i-3", assignee_ids=["u-a"])
        self.assertEqual(self.run_assign(it, False),
                         [("PATCH", "/projects/p-1/issues/i-3/", {}, {"assignees": ["u-a", "u-b"]})])

    def test_already_assigned_is_not_duplicated(self):
        it = asg.new_issue(key="DEMO-3", id="i-3", assignee_ids=["u-b"])
        self.assertEqual(self.run_assign(it, False)[0][3], {"assignees": ["u-b"]})

    def test_replace(self):
        it = asg.new_issue(key="DEMO-3", id="i-3", assignee_ids=["u-a"])
        self.assertEqual(self.run_assign(it, True)[0][3], {"assignees": ["u-b"]})


if __name__ == "__main__":
    unittest.main()
