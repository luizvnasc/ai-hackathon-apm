"""GitLab and Plane clients and loaders, fed with fixtures (no network)."""
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import load

blk = load("blocked-issues", "blocked.py")
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gitlab():
    gl = blk.GitLab.__new__(blk.GitLab)
    gl.url, gl.headers, gl.project = "https://gitlab.example.com", {"PRIVATE-TOKEN": "glpat-secret"}, "demo/app"
    gl.items, gl.by_gid, gl.project_ids = {}, {}, {"demo/app": 100}
    return gl


def plane():
    p = blk.Plane.__new__(blk.Plane)
    p.url, p.ws = "https://plane.example.com", "demo-ws"
    p.base = "https://plane.example.com/api/v1/workspaces/demo-ws"
    p.headers = {"X-API-Key": "plane-secret"}
    p.min_interval, p.last = 0.0, 0.0
    p.items, p.by_id = {}, {}
    p.idents = {"p-1": "DEMO", "p-2": "OPS"}
    p.pid, p.ident, p.project = "p-1", "DEMO", "Demo (DEMO)"
    p.members = {"u-1": "dev-a", "u-2": "dev-b"}
    return p


class PlaneRouter:
    """Fake Plane.req: answers by path from fixtures; a missing or 404 entry raises ApiError(404).

    Pass the `ApiError` of the module under test: every `load` returns a fresh module.
    """

    def __init__(self, routes, error=None):
        self.routes, self.calls, self.error = routes, [], error or blk.ApiError

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        value = self.routes.get(path, 404)
        if value == 404:
            raise self.error(404, '{"error": "not found"}', "GET", path)
        return value


def plane_routes(**extra):
    routes = {
        "/projects/": fixture("plane_projects.json"),
        "/members/": fixture("plane_members.json"),
        "/projects/p-1/cycles/": fixture("plane_cycles.json"),
        "/projects/p-1/cycles/c-2/cycle-issues/": fixture("plane_cycle_issues_c2.json"),
        "/projects/p-1/cycles/c-3/cycle-issues/": {"results": [], "next_page_results": False},
        "/projects/p-1/issues/": fixture("plane_issues_all.json"),
        "/projects/p-1/work-items/i-1/relations/": fixture("plane_relations_i1.json"),
        "/projects/p-1/work-items/i-7/relations/": fixture("plane_relations_i7.json"),
        "/projects/p-1/issues/i-1/comments/": fixture("plane_comments_i1.json"),
        "/projects/p-1/issues/i-1/activities/": fixture("plane_activities_i1.json"),
    }
    for key in ("i-2", "i-3", "i-4", "i-6"):
        routes[f"/projects/p-1/work-items/{key}/relations/"] = fixture("plane_relations_empty.json")
    for key in ("i-3", "i-6", "i-7"):
        routes[f"/projects/p-1/issues/{key}/comments/"] = {"results": []}
        routes[f"/projects/p-1/issues/{key}/activities/"] = {"results": []}
    routes.update(extra)
    return routes


# ====================================================================== GitLab
class GitLabRouter:
    """Fake GitLab.gql/rest/rest_pages answering from fixtures."""

    def __init__(self, gql_pages=None, rest=None, extra_items=None):
        self.gql_pages = list(gql_pages or [])
        self.rest_routes = rest or {}
        self.extra_items = extra_items or {}
        self.gql_calls, self.rest_calls = [], []

    def gql(self, query, variables=None):
        self.gql_calls.append(dict(variables or {}))
        if "workItem(id:" in query:
            return {"workItem": self.extra_items.get(variables["id"])}
        if "workItems(iid:" in query:
            node = self.extra_items.get((variables["p"], variables["iid"]))
            return {"project": {"workItems": {"nodes": [node] if node else []}}}
        return self.gql_pages.pop(0)

    def rest(self, path, params=None):
        self.rest_calls.append(path)
        value = self.rest_routes.get(path, 404)
        if value == 404:
            raise blk.ApiError(404, '{"message": "404 Not found"}', "GET", path)
        return value


def gitlab_with(router):
    gl = gitlab()
    gl.gql, gl.rest, gl.rest_pages = router.gql, router.rest, router.rest
    return gl


def gl_pages():
    return [fixture("gitlab_work_items_page1.json")["data"], fixture("gitlab_work_items_page2.json")["data"]]


class GitLabLoaderTest(unittest.TestCase):
    def test_load_paginates_and_normalizes(self):
        router = GitLabRouter(gl_pages())
        gl = gitlab_with(router)
        items = gl.load("v0.3")
        self.assertEqual([i["key"] for i in items], ["#1", "#2", "#3", "#4", "#6"])
        self.assertEqual(router.gql_calls, [{"p": "demo/app", "after": None, "ms": ["v0.3"]},
                                            {"p": "demo/app", "after": "CURSOR1", "ms": ["v0.3"]}])
        one = items[0]
        self.assertEqual(one["title"], "Checkout flow")
        self.assertEqual(one["status"], "workflow::blocked")
        self.assertEqual(one["priority"], "high")
        self.assertEqual(one["assignees"], ["dev-a"])
        self.assertEqual(one["period"], "v0.3")
        self.assertEqual(one["parent"], "#10")
        self.assertEqual(one["children"], ["#5"])
        self.assertEqual(one["links"], [{"type": "blocks", "key": "#2", "created": "2026-02-18T08:00:00Z",
                                         "resolved": False}])
        self.assertEqual(one["url"], "https://gitlab.example.com/demo/app/-/issues/1")
        self.assertTrue(one["open"])
        self.assertEqual(items[1]["status"], "workflow::in progress")
        self.assertIsNone(items[1]["priority"])
        four = items[3]
        self.assertIsNone(four["period"])                       # no milestone
        self.assertEqual(four["links"][0]["key"], "other-group/lib#7")  # other project keeps its path
        # linked, parent and child issues are remembered as stubs
        self.assertFalse(gl.items["#10"]["full"])
        self.assertFalse(gl.items["#5"]["full"])
        self.assertFalse(gl.items["other-group/lib#7"]["full"])
        self.assertTrue(gl.items["#2"]["full"])

    def test_whole_project_sends_no_milestone(self):
        router = GitLabRouter(gl_pages())
        gitlab_with(router).load(None)
        self.assertIsNone(router.gql_calls[0]["ms"])

    def test_missing_project_exits_with_a_message(self):
        router = GitLabRouter([{"project": None}])
        with self.assertRaises(SystemExit) as cm:
            gitlab_with(router).load(None)
        self.assertIn("'demo/app' not found", str(cm.exception.code))
        self.assertNotIn("glpat-secret", str(cm.exception.code))

    def test_periods_count_open_and_blocked_per_milestone(self):
        router = GitLabRouter([fixture("gitlab_milestones.json")["data"]] + gl_pages())
        p = gitlab_with(router).periods()
        self.assertEqual(p["kind"], "milestone")
        self.assertEqual(p["project"], "Demo App")
        self.assertEqual(p["periods"], [
            {"name": "v0.3", "start": "2026-02-16", "end": "2026-03-13", "open_issues": 4, "blocked_label": 3},
            {"name": "v0.4", "start": None, "end": None, "open_issues": 0, "blocked_label": 0}])
        self.assertEqual(p["whole_project"], {"open_issues": 5, "blocked_label": 3})

    def test_resolve_links_reads_the_direction_from_the_link(self):
        router = GitLabRouter(gl_pages(), rest={
            "/projects/other-group%2Flib": {"id": 200},
            "/projects/100/issues/4/links": fixture("gitlab_links_4.json"),
            "/projects/100/issues/4/links/55": fixture("gitlab_link_detail_55.json"),
        })
        gl = gitlab_with(router)
        items = gl.load(None)
        four = items[3]
        gl.resolve_links(four)
        self.assertEqual(four["links"][0]["type"], "blocked_by")  # the other issue is the link's source
        self.assertTrue(four["links"][0]["resolved"])

    def test_resolve_links_keeps_blocks_when_this_issue_is_the_source(self):
        detail = {"source_issue": {"iid": 1, "project_id": 100}, "target_issue": {"iid": 2, "project_id": 100},
                  "link_type": "blocks"}
        links = [{"iid": 2, "project_id": 100, "issue_link_id": 9, "link_type": "blocks",
                  "references": {"full": "demo/app#2"}}]
        router = GitLabRouter(gl_pages(), rest={"/projects/100/issues/1/links": links,
                                                "/projects/100/issues/1/links/9": detail})
        gl = gitlab_with(router)
        one = gl.load(None)[0]
        gl.resolve_links(one)
        self.assertEqual(one["links"][0]["type"], "blocks")
        self.assertTrue(one["links"][0]["resolved"])

    def test_resolve_links_404_keeps_the_graphql_type(self):
        router = GitLabRouter(gl_pages())
        gl = gitlab_with(router)
        one = gl.load(None)[0]
        gl.resolve_links(one)
        self.assertEqual(one["links"][0]["type"], "blocks")
        self.assertFalse(one["links"][0]["resolved"])

    def test_resolve_links_skips_issues_without_blocking_links(self):
        router = GitLabRouter()
        gl = gitlab_with(router)
        gl.resolve_links(blk.new_item(key="#9", links=[{"type": "relates_to", "key": "#1"}]))
        self.assertEqual(router.rest_calls, [])

    def test_detail_reads_notes_and_blocked_label_date(self):
        router = GitLabRouter(gl_pages(), rest={
            "/projects/100/issues/1/notes": fixture("gitlab_notes_1.json"),
            "/projects/100/issues/1/resource_label_events": fixture("gitlab_label_events_1.json"),
        })
        gl = gitlab_with(router)
        one = gl.load(None)[0]
        gl.detail(one)
        self.assertEqual([c["body"][:7] for c in one["comments"]], ["Waiting", "Any new"])  # system note dropped
        self.assertEqual(one["comments"][0]["author"], "dev-a")
        self.assertEqual((one["blocked_since"], one["blocked_since_source"]), ("2026-02-17T08:00:00Z", "label added"))
        calls = len(router.rest_calls)
        gl.detail(one)  # cached
        self.assertEqual(len(router.rest_calls), calls)

    def test_detail_without_label_events(self):
        router = GitLabRouter(gl_pages(), rest={"/projects/100/issues/2/notes": []})
        gl = gitlab_with(router)
        two = gl.load(None)[1]
        gl.detail(two)
        self.assertIsNone(two["blocked_since"])

    def test_mentions(self):
        # Regression: `group/project#N` references were never matched, because the lookbehind
        # meant for a bare `#N` also ran right after the path.
        gl = gitlab()
        text = ("Waiting on #2, other-group/lib#7 and demo/app#3. See "
                "https://gitlab.example.com/other-group/lib/-/issues/8 and "
                "https://gitlab.example.com/demo/app/-/work_items/9. Not &#39;html&#39; or abc#4")
        self.assertEqual(gl.mentions(text), ["#2", "other-group/lib#7", "#3", "other-group/lib#8", "#9"])
        self.assertEqual(gl.mentions(None), [])
        self.assertIsNone(gl.own_ticket_prefix())

    def test_full_fetches_a_stub_by_global_id(self):
        node = fixture("gitlab_work_items_page1.json")["data"]["project"]["workItems"]["nodes"][1]
        router = GitLabRouter([fixture("gitlab_work_items_page2.json")["data"]],
                              extra_items={"gid://gitlab/WorkItem/1007": node})
        gl = gitlab_with(router)
        gl.load(None)
        self.assertFalse(gl.items["other-group/lib#7"]["full"])
        it = gl.full("other-group/lib#7")
        self.assertTrue(it["full"])

    def test_full_fetches_an_unknown_key_by_path_and_iid(self):
        node = fixture("gitlab_work_items_page1.json")["data"]["project"]["workItems"]["nodes"][1]
        router = GitLabRouter(extra_items={("demo/app", "2"): node})
        it = gitlab_with(router).full("#2")
        self.assertEqual(it["title"], "Payment API")
        self.assertEqual(router.gql_calls, [{"p": "demo/app", "iid": "2"}])

    def test_full_of_a_missing_issue_returns_none(self):
        # Regression: a comment mentioning an issue number that does not exist (empty `nodes`)
        # crashed the whole report with an IndexError.
        router = GitLabRouter()
        self.assertIsNone(gitlab_with(router).full("#999"))

    def test_full_of_an_unreadable_project_returns_none(self):
        gl = gitlab()
        gl.gql = mock.Mock(return_value={"project": None})
        self.assertIsNone(gl.full("secret-group/x#1"))
        gl.gql = mock.Mock(side_effect=blk.ApiError(403, "forbidden", "POST", "u"))
        self.assertIsNone(gl.full("secret-group/x#1"))

    def test_project_id_quotes_the_path(self):
        gl = gitlab()
        gl.project_ids = {}
        gl.rest = mock.Mock(return_value={"id": 42})
        self.assertEqual(gl.project_id("a/b c"), 42)
        gl.rest.assert_called_once_with("/projects/a%2Fb%20c")
        gl.project_id("a/b c")
        self.assertEqual(gl.rest.call_count, 1)  # cached

    def test_my_projects_follows_rest_pages_and_skips_deleted(self):
        responses = [([{"path_with_namespace": "g/a", "name": "A", "web_url": "wa", "open_issues_count": 3,
                        "last_activity_at": "2026-02-01T10:00:00Z"},
                       {"path_with_namespace": "g/gone", "name": "Gone", "web_url": "wg",
                        "marked_for_deletion_on": "2026-01-01"}], {"X-Next-Page": "2"}),
                     ([{"path_with_namespace": "g/b", "name": "B", "web_url": "wb"}], {"X-Next-Page": ""})]
        with mock.patch.object(blk, "http", side_effect=responses) as http:
            rows = gitlab().my_projects()
        self.assertEqual(rows, [
            {"path": "g/a", "name": "A", "url": "wa", "open_issues": 3, "last_activity": "2026-02-01"},
            {"path": "g/b", "name": "B", "url": "wb", "open_issues": None, "last_activity": None}])
        urls = [c.args[1] for c in http.call_args_list]
        self.assertIn("membership=true", urls[0])
        self.assertIn("page=2", urls[1])
        self.assertTrue(all("glpat-secret" not in u for u in urls))
        self.assertEqual(http.call_args_list[0].args[2], {"PRIVATE-TOKEN": "glpat-secret"})

    def test_graphql_errors_raise_api_error(self):
        with mock.patch.object(blk, "http", return_value=({"errors": [{"message": "boom"}]}, {})):
            with self.assertRaises(blk.ApiError):
                gitlab().gql("query { x }")


# ====================================================================== Plane
class PlaneLoaderTest(unittest.TestCase):
    def run_plane(self, fn, routes=None):
        p = plane()
        router = PlaneRouter(routes or plane_routes())
        with mock.patch.object(blk.Plane, "req", side_effect=router):
            result = fn(p)
        return p, result, router

    def test_load_cycle_normalizes_and_links_parents(self):
        p, items, router = self.run_plane(lambda p: p.load("cycle 2"))
        by_key = {i["key"]: i for i in items}
        self.assertEqual(sorted(by_key), ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4", "DEMO-6"])
        one = by_key["DEMO-1"]
        self.assertEqual((one["status"], one["status_group"]), ("Blocked", "started"))
        self.assertEqual(one["labels"], ["backend"])
        self.assertEqual(one["assignees"], ["dev-a"])
        self.assertEqual(one["priority"], "high")
        self.assertEqual(one["period"], "Cycle 2")
        self.assertEqual(one["parent"], "DEMO-6")
        self.assertEqual(by_key["DEMO-6"]["children"], ["DEMO-1"])
        self.assertEqual(one["description"], "Checkout & payment.\nBlocked by the gateway contract.")
        self.assertEqual(one["url"], "https://plane.example.com/demo-ws/projects/p-1/issues/i-1")
        self.assertEqual(by_key["DEMO-2"]["priority"], "medium")
        self.assertIsNone(by_key["DEMO-3"]["priority"])  # "none" means no priority
        self.assertFalse(by_key["DEMO-4"]["open"])  # completed group
        self.assertEqual(by_key["DEMO-3"]["labels"], ["blocked"])
        cycle_calls = [c for c in router.calls if c[0].endswith("cycle-issues/")]
        self.assertEqual([c[0] for c in cycle_calls], ["/projects/p-1/cycles/c-2/cycle-issues/"])
        self.assertEqual(cycle_calls[0][1]["expand"], "state,labels,assignees")

    def test_load_whole_project_skips_drafts_and_has_no_period(self):
        _, items, router = self.run_plane(lambda p: p.load(None))
        keys = [i["key"] for i in items]
        self.assertIn("DEMO-7", keys)
        self.assertNotIn("DEMO-8", keys)  # draft
        self.assertTrue(all(i["period"] is None for i in items))
        self.assertIn(("/projects/p-1/issues/", {"per_page": 100, "expand": "state,labels,assignees"}), router.calls)
        self.assertFalse(any(c[0].endswith("cycle-issues/") for c in router.calls))

    def test_load_cycle_by_id(self):
        _, items, _ = self.run_plane(lambda p: p.load("c-2"))
        self.assertEqual(items[0]["period"], "Cycle 2")

    def test_unknown_cycle_lists_the_cycles(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_plane(lambda p: p.load("Cycle 9"))
        self.assertIn("'Cycle 9' not found", str(cm.exception.code))
        self.assertIn("Cycle 2", str(cm.exception.code))

    def test_to_item_edge_cases(self):
        p = plane()
        it = p.to_item({"id": "x-1", "sequence_id": 5, "name": "n", "project": "p-2", "state": "s-uuid",
                        "priority": "none", "archived_at": None})
        self.assertEqual(it["key"], "OPS-5")
        self.assertTrue(it["open"])             # state not expanded: treated as open
        self.assertIsNone(it["status"])
        self.assertIsNone(it["priority"])
        archived = p.to_item({"id": "x-2", "sequence_id": 6, "name": "n", "state": {"name": "Todo", "group": "unstarted"},
                              "archived_at": "2026-01-01T00:00:00Z"})
        self.assertFalse(archived["open"])
        cancelled = p.to_item({"id": "x-3", "sequence_id": 7, "name": "n",
                               "state": {"name": "Cancelled", "group": "cancelled"}})
        self.assertFalse(cancelled["open"])

    def test_open_cycles_and_periods(self):
        import datetime as dt
        _, res, _ = self.run_plane(lambda p: p.periods(dt.date(2026, 3, 1)))
        self.assertEqual(res["kind"], "cycle")
        self.assertEqual([(c["name"], c["start"], c["end"]) for c in res["periods"]],
                         [("Cycle 2", "2026-02-16", "2026-03-01"), ("Cycle 3", "2026-03-02", "2026-03-15")])
        self.assertEqual((res["periods"][0]["open_issues"], res["periods"][0]["blocked_label"]), (4, 3))
        self.assertEqual(res["whole_project"], {"open_issues": 5, "blocked_label": 3})

    def test_relations_blocked_by_blocks_relates_to(self):
        def go(p):
            p.load("Cycle 2")
            it = p.items["DEMO-1"]
            p.load_relations(it)
            p.load_relations(it)  # loaded once
            return it
        p, it, router = self.run_plane(go)
        self.assertEqual(it["links"], [
            {"type": "blocked_by", "key": "DEMO-2", "created": None, "resolved": True},
            {"type": "relates_to", "key": "DEMO-4", "created": None, "resolved": True}])
        self.assertEqual(sum(1 for c in router.calls if c[0].endswith("i-1/relations/")), 1)

    def test_relation_blocking_maps_to_blocks_and_fetches_unknown_issues(self):
        rel = {"blocking": [{"project_id": "p-2", "issue_id": "o-9"}], "blocked_by": [], "duplicate": [
            {"project_id": "p-1", "issue_id": "i-gone"}]}
        other = {"id": "o-9", "sequence_id": 9, "name": "Ops task", "project": "p-2",
                 "state": {"name": "Todo", "group": "unstarted"}}
        routes = plane_routes(**{"/projects/p-1/work-items/i-1/relations/": rel,
                                 "/projects/p-2/issues/o-9/": other})

        def go(p):
            p.load("Cycle 2")
            p.load_relations(p.items["DEMO-1"])
            return p.items["DEMO-1"]
        _, it, router = self.run_plane(go, routes)
        self.assertEqual([(l["type"], l["key"]) for l in it["links"]], [("blocks", "OPS-9")])  # unreadable dropped
        self.assertIn(("/projects/p-2/issues/o-9/", {"expand": "state,labels,assignees"}), router.calls)

    def test_relations_fall_back_to_the_issues_path_then_warn(self):
        routes = plane_routes(**{"/projects/p-1/work-items/i-1/relations/": 404,
                                 "/projects/p-1/issues/i-1/relations/": fixture("plane_relations_i1.json")})

        def go(p):
            p.load("Cycle 2")
            p.load_relations(p.items["DEMO-1"])
            return p.items["DEMO-1"]
        _, it, _ = self.run_plane(go, routes)
        self.assertEqual(it["links"][0]["key"], "DEMO-2")

        routes = plane_routes(**{"/projects/p-1/work-items/i-1/relations/": 404})
        err = io.StringIO()
        with mock.patch.object(blk.sys, "stderr", err):
            _, it, _ = self.run_plane(go, routes)
        self.assertEqual(it["links"], [])
        self.assertIn("cannot read the relations of DEMO-1", err.getvalue())

    def test_detail_reads_comments_and_blocked_state_date(self):
        def go(p):
            p.load("Cycle 2")
            it = p.items["DEMO-1"]
            p.detail(it)
            return it
        _, it, _ = self.run_plane(go)
        self.assertEqual([c["created"] for c in it["comments"]], ["2026-02-20T09:00:00Z", "2026-02-24T09:00:00Z"])
        self.assertEqual(it["comments"][0]["author"], "dev-b")      # from the members map
        self.assertEqual(it["comments"][1]["author"], "dev-a")      # from actor_detail
        self.assertEqual(it["comments"][1]["body"], "Still waiting for DEMO-2 and OPS-9.")
        self.assertEqual((it["blocked_since"], it["blocked_since_source"]), ("2026-02-19T08:00:00Z", "state changed"))
        self.assertEqual(it["links"][0]["type"], "blocked_by")

    def test_detail_without_activities(self):
        routes = plane_routes(**{"/projects/p-1/issues/i-1/activities/": 404})

        def go(p):
            p.load("Cycle 2")
            p.detail(p.items["DEMO-1"])
            return p.items["DEMO-1"]
        _, it, _ = self.run_plane(go, routes)
        self.assertIsNone(it["blocked_since"])

    def test_full_by_key_and_unknown_key(self):
        routes = plane_routes(**{"/work-items/DEMO-42/": {"id": "i-42", "sequence_id": 42, "name": "Far",
                                                          "project": "p-1",
                                                          "state": {"name": "Todo", "group": "unstarted"}}})
        _, (found, missing), _ = self.run_plane(lambda p: (p.full("DEMO-42"), p.full("DEMO-404")), routes)
        self.assertEqual(found["title"], "Far")
        self.assertIsNone(missing)

    def test_mentions(self):
        p = plane()
        self.assertEqual(p.mentions("Waiting for DEMO-2, OPS-9 and XDEMO-3; DEMO-12."), ["DEMO-2", "DEMO-12"])
        self.assertEqual(p.own_ticket_prefix(), "DEMO")

    def test_my_projects_skips_archived(self):
        p = plane()
        p.all_projects = fixture("plane_projects.json")["results"]
        self.assertEqual([r["path"] for r in p.my_projects()], ["DEMO", "OPS"])
        self.assertEqual(p.my_projects()[0]["url"], "https://plane.example.com/demo-ws/projects/p-1/issues")


class PlaneConstructorTest(unittest.TestCase):
    def make(self, project, routes=None):
        router = PlaneRouter(routes or plane_routes())
        with mock.patch.object(blk.Plane, "req", side_effect=router):
            return blk.Plane("https://plane.example.com/", "demo-ws", "plane-secret", project)

    def test_matches_identifier_name_or_id(self):
        for name in ("demo", " DEMO ", "Demo", "p-1"):
            p = self.make(name)
            self.assertEqual(p.pid, "p-1")
        self.assertEqual(p.project, "Demo (DEMO)")
        self.assertEqual(p.members, {"u-1": "dev-a", "u-2": "dev-b"})

    def test_members_unreadable_is_not_fatal(self):
        routes = plane_routes(**{"/members/": 404})
        self.assertEqual(self.make("DEMO", routes).members, {})

    def test_unknown_project_lists_the_others_without_the_key(self):
        with self.assertRaises(SystemExit) as cm:
            self.make("nope")
        msg = str(cm.exception.code)
        self.assertIn("'nope' not found", msg)
        self.assertIn("Demo (DEMO)", msg)
        self.assertNotIn("plane-secret", msg)

    def test_no_project_only_lists(self):
        p = self.make(None)
        self.assertFalse(hasattr(p, "pid"))
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/demo-ws")


class PlaneHttpTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(blk.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_follows_cursor_pagination(self):
        pages = [({"results": [1, 2], "next_page_results": True, "next_cursor": "100:1:0"}, {}),
                 ({"results": [3], "next_page_results": False, "next_cursor": "100:2:0"}, {})]
        with mock.patch.object(blk, "http", side_effect=pages) as http:
            self.assertEqual(plane().list("/projects/"), [1, 2, 3])
        urls = [c.args[1] for c in http.call_args_list]
        self.assertIn("per_page=100", urls[0])
        self.assertIn("cursor=100%3A1%3A0", urls[1])
        self.assertTrue(all("plane-secret" not in u for u in urls))
        self.assertEqual(http.call_args_list[0].args[2], {"X-API-Key": "plane-secret"})

    def test_list_accepts_a_plain_list_and_stops_without_a_cursor(self):
        with mock.patch.object(blk, "http", return_value=([{"id": 1}], {})):
            self.assertEqual(plane().list("/x/"), [{"id": 1}])
        with mock.patch.object(blk, "http", return_value=({"results": [1], "next_page_results": True}, {})) as http:
            self.assertEqual(plane().list("/x/"), [1])
        self.assertEqual(http.call_count, 1)

    def test_429_is_retried(self):
        responses = [blk.ApiError(429, "slow down", "GET", "u"), ({"ok": True}, {})]
        with mock.patch.object(blk, "http", side_effect=responses) as http:
            self.assertEqual(plane().req("/x/"), {"ok": True})
        self.assertEqual(http.call_count, 2)

    def test_429_retries_are_bounded(self):
        with mock.patch.object(blk, "http", side_effect=blk.ApiError(429, "slow", "GET", "u")) as http:
            with self.assertRaises(blk.ApiError) as cm:
                plane().req("/x/")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(http.call_count, 8)

    def test_5xx_retried_three_times_then_raised(self):
        with mock.patch.object(blk, "http", side_effect=blk.ApiError(502, "bad gateway", "GET", "u")) as http:
            with self.assertRaises(blk.ApiError) as cm:
                plane().req("/x/")
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(http.call_count, 4)

    def test_4xx_is_not_retried(self):
        with mock.patch.object(blk, "http", side_effect=blk.ApiError(404, "nf", "GET", "u")) as http:
            with self.assertRaises(blk.ApiError):
                plane().req("/x/")
        self.assertEqual(http.call_count, 1)


class HttpTest(unittest.TestCase):
    def test_http_error_becomes_api_error_without_headers(self):
        err = urllib.error.HTTPError("https://h/x", 401, "Unauthorized", {}, io.BytesIO(b'{"message":"401"}'))
        with mock.patch.object(blk.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(blk.ApiError) as cm:
                blk.http("GET", "https://h/x", {"PRIVATE-TOKEN": "glpat-secret"})
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn("glpat-secret", str(cm.exception))

    def test_unreachable_host_exits_cleanly(self):
        with mock.patch.object(blk.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(SystemExit) as cm:
                blk.http("GET", "https://h/x", {"X-API-Key": "plane-secret"})
        self.assertIn("cannot reach", str(cm.exception.code))
        self.assertNotIn("plane-secret", str(cm.exception.code))

    def test_request_has_a_timeout_and_headers(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"[]"
        with mock.patch.object(blk.urllib.request, "urlopen", return_value=resp) as urlopen:
            blk.http("GET", "https://h/x", {"X-API-Key": "k"})
        self.assertGreater(urlopen.call_args.kwargs["timeout"], 0)
        self.assertEqual(urlopen.call_args.args[0].get_header("X-api-key"), "k")


if __name__ == "__main__":
    unittest.main()
