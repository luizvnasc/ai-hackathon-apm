"""Plane: workflow labels become states (reused, never deleted), scoped labels become label groups,
the API client (pacing, retries, pagination) and the project lookup."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.unit._load import SKILLS, load
from tests.unit.populate_project.fakes import PLANE_KEY, FakePlane, args, fixture

pop = load("populate-project", "populate.py")
DEFAULT = pop.load_labels(str(SKILLS / "populate-project" / "labels.default.json"))


def labels(*names):
    out = []
    for n in names:
        scope, value = pop.split_scope(n)
        out.append({"name": n, "color": "#123456", "description": "", "scope": scope, "value": value})
    return out


def run(server, label_set=DEFAULT, **kw):
    """populate_plane against the fake server; returns (info, plan, stdout, stderr)."""
    kw.setdefault("target", "plane")
    kw.setdefault("project", "SHOP")
    plan = pop.Plan(kw.get("dry_run", False))
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(pop, "http", server), mock.patch.object(pop.time, "sleep"), \
            mock.patch.dict(os.environ, {"PLANE_API_KEY": PLANE_KEY}, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        info = pop.populate_plane(args(**kw), label_set, plan)
    return info, plan, out.getvalue(), err.getvalue()


def names(items, kind):
    return [x["name"] for x in items if x["kind"] == kind]


def state_names(server, pid="p-1"):
    return [s["name"] for s in server.states[pid]]


class PlaneStatesTest(unittest.TestCase):
    def test_default_workflow_reuses_default_states_and_adds_review_and_blocked(self):
        server = FakePlane(pop)
        info, plan, out, _ = run(server)
        self.assertEqual(names(plan.reused, "state"), ["Backlog", "Todo", "In Progress", "Done"])
        self.assertEqual(names(plan.created, "state"),
                         ["In Review [started] for workflow::review", "Blocked [started] for workflow::blocked"])
        new = {s["name"]: s for s in server.states["p-1"] if s["id"].startswith("s-new")}
        self.assertEqual(new["In Review"]["sequence"], 36000)  # after In Progress, before Done (45000)
        self.assertEqual(new["Blocked"]["sequence"], 37000)
        self.assertEqual(new["In Review"]["group"], "started")
        self.assertEqual(new["In Review"]["color"], pop.GROUP_COLORS["started"])
        self.assertEqual(new["Blocked"]["description"], "Waiting on something outside the team")
        self.assertIn("states not in the workflow were kept", " ".join(plan.notes))
        self.assertIn("Cancelled", " ".join(plan.notes))
        self.assertEqual(info["url"], "https://plane.example.com/demo-ws/projects/p-1/issues")

    def test_states_are_never_deleted_or_modified(self):
        server = FakePlane(pop)
        run(server, label_set=labels("workflow::todo", "workflow::done"))
        self.assertEqual(state_names(server), ["Backlog", "Todo", "In Progress", "Done", "Cancelled"])
        self.assertEqual({c[0] for c in server.calls}, {"GET"})

    def test_existing_custom_state_named_like_the_value_is_reused(self):
        states = fixture("plane_states.json")["results"] + [
            {"id": "s-9", "name": "Review", "group": "started", "sequence": 36000}]
        server = FakePlane(pop, states=states)
        _, plan, _, _ = run(server, label_set=labels("workflow::review"))
        self.assertEqual(names(plan.reused, "state"), ["Review"])
        self.assertEqual(plan.created, [])

    def test_unknown_value_and_a_group_without_states(self):
        states = [s for s in fixture("plane_states.json")["results"] if s["group"] != "cancelled"]
        server = FakePlane(pop, states=states)
        _, plan, _, _ = run(server, label_set=labels("status::ready-for-deploy", "Workflow::wontfix"))
        new = {s["name"]: s for s in server.states["p-1"] if s["id"].startswith("s-new")}
        self.assertEqual(new["Ready For Deploy"]["group"], "started")
        self.assertEqual(new["Ready For Deploy"]["sequence"], 36000)
        self.assertEqual(new["Cancelled"]["group"], "cancelled")
        self.assertEqual(new["Cancelled"]["sequence"], 46000)  # no cancelled state yet: after the last one

    def test_no_workflow_labels(self):
        server = FakePlane(pop)
        _, plan, _, _ = run(server, label_set=labels("type::bug"))
        self.assertIn("no workflow labels in the set, so no states were created", plan.notes)
        self.assertFalse(any("states not in the workflow" in n for n in plan.notes))
        self.assertEqual(len(server.states["p-1"]), 5)


class PlaneLabelsTest(unittest.TestCase):
    def test_scoped_labels_become_groups_and_priority_is_skipped(self):
        server = FakePlane(pop)
        _, plan, _, _ = run(server)
        self.assertEqual(names(plan.created, "label"), [
            "type", "feature (in type)", "bug (in type)", "chore (in type)", "tech-debt (in type)", "docs (in type)",
            "area", "frontend (in area)", "backend (in area)", "api (in area)", "database (in area)",
            "infra (in area)"])
        by_name = {l["name"]: l for l in server.labels["p-1"]}
        self.assertIsNone(by_name["type"]["parent"])
        self.assertEqual(by_name["type"]["color"], "#6B7280")
        self.assertEqual(by_name["bug"]["parent"], by_name["type"]["id"])
        self.assertEqual(by_name["bug"]["color"], "#dc143c")
        self.assertEqual(by_name["api"]["parent"], by_name["area"]["id"])
        self.assertNotIn("critical", by_name)
        self.assertNotIn("priority", by_name)
        self.assertIn("built-in Priority field", " ".join(plan.notes))

    def test_priority_labels_flag_creates_them(self):
        server = FakePlane(pop)
        _, plan, _, _ = run(server, priority_labels=True)
        by_name = {l["name"]: l for l in server.labels["p-1"]}
        for v in ("critical", "high", "medium", "low"):
            self.assertEqual(by_name[v]["parent"], by_name["priority"]["id"])
        self.assertFalse(any("Priority field" in n for n in plan.notes))

    def test_existing_labels_are_reused(self):
        server = FakePlane(pop, labels=fixture("plane_labels.json")["results"])
        _, plan, _, _ = run(server)
        self.assertEqual(names(plan.reused, "label"), ["type", "bug"])
        self.assertEqual(len(names(plan.created, "label")), 10)
        feature = next(l for l in server.labels["p-1"] if l["name"] == "feature")
        self.assertEqual(feature["parent"], "l-1")  # under the existing "type" group

    def test_colliding_values_keep_the_full_name(self):
        server = FakePlane(pop)
        _, plan, _, _ = run(server, label_set=labels("type::api", "area::api", "infra", "area::infra", "urgent"))
        self.assertEqual(names(plan.created, "label"), [
            "infra", "urgent", "type", "type::api (in type)", "area", "area::api (in area)",
            "area::infra (in area)"])

    def test_labels_are_read_from_every_page(self):
        many = [{"id": f"l-{i}", "name": f"x{i}", "parent": None} for i in range(5)] + \
               fixture("plane_labels.json")["results"]
        server = FakePlane(pop, labels=many, page_size=2)
        _, plan, _, _ = run(server, label_set=labels("type::bug"))
        self.assertEqual(names(plan.reused, "label"), ["type", "bug"])
        cursors = [c[2].get("cursor") for c in server.calls if c[1].endswith("/labels/")]
        self.assertEqual(cursors, [None, "2", "4", "6"])


class PlaneProjectTest(unittest.TestCase):
    def test_match_by_name_or_identifier_ignoring_case(self):
        for project in ("shop", "SHOP", " Shop "):
            _, _, out, _ = run(FakePlane(pop), project=project, dry_run=True)
            self.assertIn("Plane project: Shop (SHOP)", out)

    def test_not_found_lists_the_existing_projects_and_exits_3(self):
        with self.assertRaises(SystemExit) as cm:
            run(FakePlane(pop), project="Nope")
        self.assertEqual(cm.exception.code, 3)

    def test_ambiguous_match(self):
        projects = [{"id": "p-1", "name": "INV", "identifier": "ABC"}, {"id": "p-2", "name": "Other", "identifier": "INV"}]
        with self.assertRaises(SystemExit) as cm:
            run(FakePlane(pop, projects=projects), project="inv")
        self.assertIn("several Plane projects match", str(cm.exception.code))

    def test_create_project(self):
        server = FakePlane(pop)
        _, plan, _, _ = run(server, project="New Store", create_project=True)
        self.assertEqual(names(plan.created, "project"), ["New Store (NS)"])
        proj = server.projects[-1]
        self.assertEqual((proj["name"], proj["identifier"]), ("New Store", "NS"))
        self.assertIn("In Review", state_names(server, proj["id"]))

    def test_create_project_with_identifier(self):
        server = FakePlane(pop)
        run(server, project="New Store", create_project=True, identifier="store")
        self.assertEqual(server.projects[-1]["identifier"], "STORE")

    def test_create_project_dry_run_writes_nothing(self):
        server = FakePlane(pop)
        _, plan, _, _ = run(server, project="New Store", create_project=True, dry_run=True)
        self.assertEqual(server.writes(), [])
        self.assertEqual([c[1] for c in server.calls], ["/api/v1/workspaces/demo-ws/projects/"])
        self.assertEqual(names(plan.created, "state"),
                         ["In Review [started] for workflow::review", "Blocked [started] for workflow::blocked"])
        self.assertEqual(len(names(plan.created, "label")), 12)

    def test_refused_key_and_unknown_workspace(self):
        for status, text in ((401, "refused the API key (HTTP 401)"), (403, "refused the API key (HTTP 403)"),
                             (404, "workspace 'demo-ws' not found")):
            err = pop.ApiError(status, "{}", "GET", "u")
            with self.assertRaises(SystemExit) as cm, mock.patch.object(pop, "http", side_effect=err), \
                    mock.patch.object(pop.time, "sleep"), mock.patch.dict(os.environ, {"PLANE_API_KEY": PLANE_KEY}):
                pop.populate_plane(args(target="plane", project="SHOP"), DEFAULT, pop.Plan(False))
            self.assertIn(text, str(cm.exception.code))
            self.assertNotIn(PLANE_KEY, str(cm.exception.code))


class PlaneDryRunAndRerunTest(unittest.TestCase):
    def test_dry_run_makes_no_write_calls(self):
        server = FakePlane(pop, labels=fixture("plane_labels.json")["results"])
        _, plan, out, _ = run(server, dry_run=True, priority_labels=True)
        self.assertEqual(server.writes(), [])
        self.assertIn("would create state: In Review", out)

    def test_dry_run_plan_equals_the_real_run(self):
        dry, real = FakePlane(pop), FakePlane(pop)
        _, dry_plan, _, _ = run(dry, dry_run=True)
        _, real_plan, _, _ = run(real)
        self.assertEqual(dry_plan.created, real_plan.created)
        self.assertEqual(dry_plan.reused, real_plan.reused)

    def test_second_run_creates_nothing(self):
        server = FakePlane(pop)
        run(server, priority_labels=True)
        writes = len(server.writes())
        _, plan, _, _ = run(server, priority_labels=True)
        self.assertEqual(plan.created, [])
        self.assertEqual(len(server.writes()), writes)
        self.assertEqual(len(server.states["p-1"]), 7)
        self.assertEqual(len(server.labels["p-1"]), 17)


class PlaneClientTest(unittest.TestCase):
    def client(self, dry_run=False):
        p = pop.Plane("https://plane.example.com/", PLANE_KEY, "demo-ws", 60, dry_run)
        p.min_interval = 0
        return p

    def test_headers_and_base_url(self):
        p = self.client()
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/demo-ws")
        self.assertEqual(p.headers, {"X-API-Key": PLANE_KEY})

    def test_workspace_is_url_encoded(self):
        # Regression: the workspace slug went into the URL as given, so "a/b?x" changed the path.
        p = pop.Plane("https://plane.example.com", PLANE_KEY, "a/b?x", 60)
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/a%2Fb%3Fx")

    def test_429_is_retried_then_succeeds(self):
        p = self.client()
        replies = [pop.ApiError(429, "slow down", "GET", "u"), (200, {"ok": 1}, {})]
        with mock.patch.object(pop, "http", side_effect=replies) as http, mock.patch.object(pop.time, "sleep") as sleep:
            self.assertEqual(p.req("GET", "/projects/"), {"ok": 1})
        self.assertEqual(http.call_count, 2)
        sleep.assert_any_call(10)

    def test_429_retries_are_bounded(self):
        p = self.client()
        with mock.patch.object(pop, "http", side_effect=pop.ApiError(429, "slow", "GET", "u")) as http, \
                mock.patch.object(pop.time, "sleep"):
            with self.assertRaises(pop.ApiError) as cm:
                p.req("GET", "/projects/")
        self.assertEqual(http.call_count, 8)
        self.assertEqual(cm.exception.status, 429)

    def test_5xx_retried_three_times_then_raised(self):
        p = self.client()
        with mock.patch.object(pop, "http", side_effect=pop.ApiError(502, "bad gateway", "GET", "u")) as http, \
                mock.patch.object(pop.time, "sleep"):
            with self.assertRaises(pop.ApiError) as cm:
                p.req("GET", "/projects/")
        self.assertEqual(http.call_count, 4)
        self.assertEqual(cm.exception.status, 502)

    def test_4xx_is_not_retried(self):
        p = self.client()
        with mock.patch.object(pop, "http", side_effect=pop.ApiError(400, "bad", "POST", "u")) as http, \
                mock.patch.object(pop.time, "sleep"):
            with self.assertRaises(pop.ApiError):
                p.req("POST", "/projects/", {"name": "x"})
        self.assertEqual(http.call_count, 1)

    def test_requests_are_paced(self):
        p = pop.Plane("https://plane.example.com", PLANE_KEY, "demo-ws", 60)
        p.last = pop.time.monotonic()
        with mock.patch.object(pop, "http", return_value=(200, [], {})), \
                mock.patch.object(pop.time, "sleep") as sleep:
            p.req("GET", "/projects/")
        self.assertGreater(sleep.call_args[0][0], 0.9)

    def test_list_handles_plain_lists_and_cursors(self):
        p = self.client()
        with mock.patch.object(pop, "http", return_value=(200, [{"id": 1}], {})):
            self.assertEqual(p.list("/x/"), [{"id": 1}])
        pages = [(200, {"results": [{"id": 1}], "next_page_results": True, "next_cursor": "c2"}, {}),
                 (200, {"results": [{"id": 2}], "next_page_results": False, "next_cursor": "c3"}, {})]
        with mock.patch.object(pop, "http", side_effect=pages) as http:
            self.assertEqual(p.list("/x/"), [{"id": 1}, {"id": 2}])
        self.assertIn("cursor=c2", http.call_args_list[1][0][1])

    def test_write_in_dry_run_never_calls_the_api(self):
        p = self.client(dry_run=True)
        with mock.patch.object(pop, "http") as http:
            made = p.write("POST", "/projects/p-1/labels/", {"name": "x"})
        http.assert_not_called()
        self.assertEqual(made["name"], "x")
        self.assertTrue(made["id"].startswith("<new"))


if __name__ == "__main__":
    unittest.main()
