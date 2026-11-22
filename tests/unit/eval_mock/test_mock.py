"""The eval mock (tests/evals/mock/sitecustomize.py): matching, misses, refused writes, and a whole
velocity.py, commitment.py and stale.py run against the shared eval fixtures."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[3]
MOCK_DIR = ROOT / "tests" / "evals" / "mock"
FIXTURES = ROOT / "tests" / "evals" / "fixtures"
COMMITMENT = ROOT / ".apm" / "skills" / "commitment-delivered" / "scripts" / "commitment.py"
ASSIGN = ROOT / ".apm" / "skills" / "assign-issues" / "scripts" / "assign.py"
POPULATE = ROOT / ".apm" / "skills" / "populate-project" / "scripts" / "populate.py"
POPULATE_LABELS = ROOT / ".apm" / "skills" / "populate-project" / "labels.default.json"
BLOCKED = ROOT / ".apm" / "skills" / "blocked-issues" / "scripts" / "blocked.py"
STALE = ROOT / ".apm" / "skills" / "stale-issues" / "scripts" / "stale.py"
VELOCITY = ROOT / ".apm" / "skills" / "team-velocity" / "scripts" / "velocity.py"

NO_PACING = ("--rate", "6000")  # the mock has no rate limit: waiting for Plane's 60 requests/minute only slows the tests

spec = importlib.util.spec_from_file_location("eval_mock_sitecustomize", MOCK_DIR / "sitecustomize.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


class Fixtures:
    """A temporary fixtures dir with a routes.json and the bodies it names."""

    def __init__(self, routes, files):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name
        pathlib.Path(self.path, "routes.json").write_text(json.dumps(routes))
        for name, body in files.items():
            pathlib.Path(self.path, name).write_text(json.dumps(body))

    def urlopen(self):
        return mock.make_urlopen(self.path)


def request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.Request(url, data=data, method=method)


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixtures(
            [{"path": "/api/v1/projects/", "query": {"cursor": "c2"}, "file": "page2.json"},
             {"path": "/api/v1/projects/", "file": "page1.json", "headers": {"X-Next-Page": "2"}},
             {"path_regex": r"^/api/v4/projects/[^/]+/$", "file": "one.json"},
             {"method": "POST", "path": "/api/graphql", "graphql": {"contains": ["workItems"], "variables": {"after": None}},
              "file": "g1.json"},
             {"method": "POST", "path": "/api/graphql", "graphql": {"contains": ["workItems"], "variables": {"after": "X"}},
              "file": "g2.json"},
             {"path": "/gone", "status": 410, "file": "gone.json"}],
            {"page1.json": {"p": 1}, "page2.json": {"p": 2}, "one.json": {"one": True}, "g1.json": {"g": 1},
             "g2.json": {"g": 2}, "gone.json": {"error": "gone"}})
        self.addCleanup(self.fx.tmp.cleanup)
        self.urlopen = self.fx.urlopen()

    def get(self, url, **kw):
        with self.urlopen(request("GET", url), **kw) as resp:
            return resp.status, resp.headers, json.loads(resp.read().decode())

    def test_host_is_ignored_and_response_matches_what_scripts_read(self):
        status, headers, body = self.get("http://anything.mock/api/v1/projects/?per_page=100", timeout=5)
        self.assertEqual((status, body), (200, {"p": 1}))
        self.assertEqual(headers.get("X-Next-Page"), "2")
        self.assertIsNone(headers.get("Missing"))

    def test_query_subset_and_first_match_wins(self):
        self.assertEqual(self.get("http://h/api/v1/projects/?per_page=100&cursor=c2")[2], {"p": 2})

    def test_path_regex(self):
        self.assertEqual(self.get("http://h/api/v4/projects/group%2Finv/")[2], {"one": True})

    def test_graphql_matched_by_keywords_and_variables(self):
        def gql(after):
            body = {"query": "query { workItems { id } }", "variables": {"after": after}}
            with self.urlopen(request("POST", "http://h/api/graphql", body)) as resp:
                return json.loads(resp.read())
        self.assertEqual(gql(None), {"g": 1})
        self.assertEqual(gql("X"), {"g": 2})

    def test_miss_is_404_with_json_body_and_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("http://h/nope?b=2&a=1")
        self.assertEqual(cm.exception.code, 404)
        self.assertIn("no fixture", json.loads(cm.exception.read().decode())["error"])
        self.assertIn("[eval-mock] MISS GET /nope?a=1&b=2", err.getvalue())

    def test_route_status_at_or_above_400_raises_http_error(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("http://h/gone")
        self.assertEqual(cm.exception.code, 410)
        self.assertEqual(json.loads(cm.exception.read().decode()), {"error": "gone"})


class ExpandTest(unittest.TestCase):
    def test_field_is_expanded_only_when_asked(self):
        fx = Fixtures([{"path": "/issues/", "file": "issues.json", "expand": {"estimate_point": "points.json"}}],
                      {"issues.json": {"results": [{"estimate_point": "ep-3"}, {"estimate_point": None}]},
                       "points.json": {"results": [{"id": "ep-3", "value": "3"}]}})
        self.addCleanup(fx.tmp.cleanup)

        def get(query):
            with fx.urlopen()(request("GET", "http://h/issues/" + query)) as resp:
                return [r["estimate_point"] for r in json.loads(resp.read())["results"]]

        self.assertEqual(get("?expand=state"), ["ep-3", None])
        self.assertEqual(get("?expand=state,estimate_point"), [{"id": "ep-3", "value": "3"}, None])


class EstimatesBugTest(unittest.TestCase):
    """EVAL_PLANE_NO_ESTIMATES: the mock has Plane CE's bug makeplane/plane#9933."""

    BUG = {"EVAL_PLANE_NO_ESTIMATES": "1"}

    def run_script(self, script, *args, env=None):
        full = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_", "EVAL_"))}
        full.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY="2026-11-12",
                    EVAL_DATASET_DATE="2026-11-12",
                    GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                    PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        full.update(env or {})
        with tempfile.TemporaryDirectory() as cwd:
            return subprocess.run([sys.executable, str(script), *args, *NO_PACING], env=full, cwd=cwd,
                                  capture_output=True, text=True, timeout=120)

    def test_stale_issues_still_has_the_points(self):
        out = self.run_script(STALE, "report", "--target", "plane", "--project", "INV", "--all", env=self.BUG)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        self.assertIn("10 points (33% of the open points)", out.stdout)

    def test_assign_issues_still_measures_load_in_points(self):
        out = self.run_script(ASSIGN, "suggest-member", "--target", "plane", "--project", "INV", "--issue", "INV-23",
                              "--no-comments", env=self.BUG)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        self.assertIn("(in points)", out.stdout)
        self.assertIn("| 1 | @alice | 91 |", out.stdout)


class WriteRefusalTest(unittest.TestCase):
    def setUp(self):
        # Even routes that would match a write must not be reached.
        self.fx = Fixtures(
            [{"method": m, "path": "/api/v1/things/", "file": "ok.json"} for m in ("POST", "PATCH", "PUT", "DELETE")]
            + [{"method": "POST", "path": "/api/graphql", "graphql": {"contains": ["mutation"]}, "file": "ok.json"},
               {"method": "POST", "path": "/api/graphql", "graphql": {"contains": ["project"]}, "file": "ok.json"}],
            {"ok.json": {"ok": True}})
        self.addCleanup(self.fx.tmp.cleanup)
        self.urlopen = self.fx.urlopen()

    def assert_refused(self, method, url, body=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(urllib.error.HTTPError) as cm:
            self.urlopen(request(method, url, body))
        self.assertEqual(cm.exception.code, 403)
        self.assertIn(f"[eval-mock] WRITE REFUSED {method}", err.getvalue())

    def test_rest_writes_are_403(self):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assert_refused(method, "http://h/api/v1/things/", {"a": 1})

    def test_graphql_mutation_is_403(self):
        self.assert_refused("POST", "http://h/api/graphql", {"query": "mutation { x }", "variables": {}})
        self.assert_refused("POST", "http://h/api/graphql", {"query": "  mutation($a: ID) { x }"})

    def test_graphql_post_without_a_query_is_treated_as_a_write(self):
        self.assert_refused("POST", "http://h/api/graphql", {"nothing": 1})

    def test_graphql_read_is_allowed(self):
        with self.urlopen(request("POST", "http://h/api/graphql", {"query": "query { project { id } }"})) as resp:
            self.assertEqual(json.loads(resp.read()), {"ok": True})


class ShiftDatesTest(unittest.TestCase):
    def test_moves_dates_and_keeps_times(self):
        body = b'{"a": "2026-11-12", "b": "2026-10-31T09:00:00Z", "c": "2026-12-30T23:59:00+01:00"}'
        self.assertEqual(mock.shift_dates(body, 10),
                         b'{"a": "2026-11-22", "b": "2026-11-10T09:00:00Z", "c": "2027-01-09T23:59:00+01:00"}')

    def test_zero_or_negative_and_non_dates(self):
        body = b'{"v": "1.2026-11-12", "id": "12026-11-120", "bad": "2026-13-40", "d": "2026-03-01"}'
        self.assertEqual(mock.shift_dates(body, 0), body)
        self.assertEqual(mock.shift_dates(body, -1),
                         b'{"v": "1.2026-11-11", "id": "12026-11-120", "bad": "2026-13-40", "d": "2026-02-28"}')

    def test_urlopen_shifts_bodies(self):
        fx = Fixtures([{"path": "/x", "file": "x.json"}], {"x.json": {"due": "2026-11-12"}})
        self.addCleanup(fx.tmp.cleanup)
        with mock.make_urlopen(fx.path, 3)(request("GET", "http://plane.mock/x")) as resp:
            self.assertEqual(json.loads(resp.read()), {"due": "2026-11-15"})


class InstallTest(unittest.TestCase):
    def test_without_eval_fixtures_nothing_is_patched(self):
        code = ("import urllib.request as u; o = u.urlopen; import sitecustomize; "
                "assert u.urlopen is o; print('untouched')")
        env = {k: v for k, v in os.environ.items() if k != "EVAL_FIXTURES"}
        env["PYTHONPATH"] = str(MOCK_DIR)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "untouched", out.stderr)

    def test_eval_today_freezes_date_today(self):
        env = {**os.environ, "PYTHONPATH": str(MOCK_DIR), "EVAL_FIXTURES": str(FIXTURES), "EVAL_TODAY": "2026-11-12"}
        out = subprocess.run([sys.executable, "-c", "import datetime as d; print(d.date.today(), "
                              "d.date.fromisoformat('2026-01-02') < d.date.today())"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.split(), ["2026-11-12", "True"], out.stderr)


class VelocityEndToEndTest(unittest.TestCase):
    """velocity.py in a subprocess, its HTTP layer answered by the mock from the committed fixtures."""

    def run_report(self, *args, token_vars=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_"))}
        env.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY="2026-11-12",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        with tempfile.TemporaryDirectory() as cwd:  # no .env
            return subprocess.run([sys.executable, str(VELOCITY), *args, *NO_PACING], env=env, cwd=cwd,
                                  capture_output=True, text=True, timeout=120)

    def check_report(self, res):
        self.assertEqual(res["unit"], "points")
        self.assertEqual([r["name"] for r in res["rows"]], ["Sprint 1", "Sprint 2", "Sprint 3", "Sprint 4"])
        self.assertEqual([r["velocity"] for r in res["rows"]], [10, 14, 12, 10])
        self.assertEqual([r["per_week"] for r in res["rows"]], [5, 7, 6, 5])
        self.assertEqual([r["scope"] for r in res["rows"]], [13, 16, 12, 10])
        self.assertEqual([r["late"] for r in res["rows"]], [3, 0, 0, 0])
        self.assertEqual(res["summary"]["median"], 11)
        self.assertEqual(res["summary"]["mean"], 11.5)
        self.assertEqual(res["summary"]["median_per_week"], 5.5)
        self.assertEqual(res["stability"], "stable")
        self.assertEqual(res["trend_word"], "flat")
        self.assertEqual(res["next"]["name"], "Sprint 6")
        self.assertEqual((res["next"]["recommended"], res["next"]["conservative"], res["next"]["scope"]), (11, 10, 8))
        self.assertEqual((res["current"]["name"], res["current"]["done"], res["current"]["scope"]), ("Sprint 5", 5, 19))
        self.assertEqual(res["member_totals"], {"alice": 25, "bob": 15, "carol": 6})

    def test_plane_report_numbers(self):
        out = self.run_report("report", "--target", "plane", "--project", "INV", "--format", "json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)  # no miss, no refused write
        res = json.loads(out.stdout)
        self.assertEqual(res["project"], "Inventory (INV)")
        self.check_report(res)

    def test_gitlab_report_numbers_match_plane(self):
        out = self.run_report("report", "--target", "gitlab", "--project", "group/inv", "--format", "json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        self.check_report(json.loads(out.stdout))

    def test_weekly_throughput(self):
        out = self.run_report("report", "--target", "plane", "--project", "INV", "--by", "week", "--format", "json")
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout)
        self.assertEqual([r["velocity"] for r in res["rows"]], [5, 5, 3, 14, 5, 7, 0, 10])

    def test_projects_listing(self):
        out = self.run_report("projects", "--target", "plane", "--format", "json")
        self.assertEqual([p["path"] for p in json.loads(out.stdout)], ["INV"])
        out = self.run_report("projects", "--target", "gitlab", "--format", "json")
        self.assertEqual([p["path"] for p in json.loads(out.stdout)], ["group/inv"])

    def test_unknown_project_is_a_clear_message_not_a_traceback(self):
        out = self.run_report("report", "--target", "gitlab", "--project", "nobody/nothing")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("Traceback", out.stderr)
        self.assertIn("[eval-mock] MISS", out.stderr)
        self.assertIn("API error", out.stderr)


class CommitmentEndToEndTest(unittest.TestCase):
    """commitment.py in a subprocess against the same fixtures (Plane activities, GitLab milestone events)."""

    def run_report(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_"))}
        env.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY="2026-11-12",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        with tempfile.TemporaryDirectory() as cwd:  # no .env
            return subprocess.run([sys.executable, str(COMMITMENT), *args, "--format", "json"], env=env, cwd=cwd,
                                  capture_output=True, text=True, timeout=120)

    def check_report(self, res):
        self.assertEqual(res["unit"], "points")
        rows = {r["name"]: r for r in res["rows"]}
        self.assertEqual(list(rows), ["Sprint 1", "Sprint 2", "Sprint 3", "Sprint 4", "Sprint 5"])
        keys = ("committed", "delivered", "removed", "cancelled", "carried", "added", "added_delivered")
        expected = {"Sprint 1": (16, 8, 2, 3, 3, 2, 2), "Sprint 2": (10, 6, 2, 0, 2, 8, 8),
                    "Sprint 3": (4, 2, 2, 0, 0, 10, 10), "Sprint 4": (2, 2, 0, 0, 0, 8, 8),
                    "Sprint 5": (16, 5, 0, 0, 11, 3, 0)}
        for name, values in expected.items():
            self.assertEqual(tuple(rows[name][k] for k in keys), values, name)
        self.assertEqual([rows[f"Sprint {n}"]["say_do"] for n in (1, 2, 3, 4)], [0.5, 0.6, 0.5, 1.0])
        self.assertEqual([rows[f"Sprint {n}"]["total_delivered"] for n in (1, 2, 3, 4)], [10, 14, 12, 10])
        self.assertEqual(rows["Sprint 5"]["status"], "current")
        s = res["summary"]
        self.assertEqual((s["n"], s["median"], s["mean"], s["last3"], s["latest"]), (4, 0.55, 0.65, 0.7, 1.0))
        self.assertEqual((s["reliability"], s["trend_word"], s["recent_drop"]), ("overcommits", "rising", False))
        self.assertEqual(s["median_delivered"], 4)
        self.assertEqual((res["next"]["name"], res["next"]["scope"]), ("Sprint 6", 8))
        self.assertEqual(dict(res["members"]), {"bob": [19, 10], "alice": [9, 7], "carol": [4, 1]})

    def test_plane_report_numbers(self):
        out = self.run_report("report", "--target", "plane", "--project", "INV", "--rate", "6000")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        res = json.loads(out.stdout)
        self.assertEqual(res["project"], "Inventory (INV)")
        self.check_report(res)

    def test_gitlab_report_numbers_match_plane(self):
        out = self.run_report("report", "--target", "gitlab", "--project", "group/inv")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        self.check_report(json.loads(out.stdout))

    def test_single_period_and_grace_days(self):
        out = self.run_report("report", "--target", "gitlab", "--project", "group/inv", "--period", "Sprint 2")
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout)
        self.assertEqual([r["name"] for r in res["rows"]], ["Sprint 2"])
        self.assertNotIn("next", res)
        late = self.run_report("report", "--target", "gitlab", "--project", "group/inv", "--grace-days", "4")
        self.assertEqual(late.returncode, 0, late.stderr)
        self.assertNotIn("[eval-mock]", late.stderr)

    def test_periods_and_projects(self):
        out = self.run_report("periods", "--target", "plane", "--project", "INV", "--rate", "6000")
        res = json.loads(out.stdout)
        self.assertEqual([p["status"] for p in res["periods"]],
                         ["finished"] * 4 + ["current", "upcoming"])
        out = self.run_report("projects", "--target", "gitlab")
        self.assertEqual([p["path"] for p in json.loads(out.stdout)], ["group/inv"])
        self.assertNotIn("[eval-mock]", out.stderr)


class StaleEndToEndTest(unittest.TestCase):
    """stale.py in a subprocess against the same fixtures (today 2026-11-12; Plane and GitLab hold the same data)."""

    def run_report(self, target, *args, today="2026-11-12"):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_", "EVAL_"))}
        env.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY=today,
                   EVAL_DATASET_DATE="2026-11-12",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        project = "INV" if target == "plane" else "group/inv"
        extra = ["--rate", "6000"] if target == "plane" else []
        with tempfile.TemporaryDirectory() as cwd:  # no .env
            out = subprocess.run([sys.executable, str(STALE), *args[:1], "--target", target, "--project", project,
                                  *args[1:], *extra, "--format", "json"], env=env, cwd=cwd,
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)  # no miss, no refused write
        return json.loads(out.stdout)

    @staticmethod
    def rows(res):
        return [(i["key"].lstrip("#").split("-")[-1], i["kind"], round(i["idle_days"]), i["points"],
                 tuple(i["assignees"])) for i in res["stale"]]

    def check_whole_project(self, res):
        self.assertEqual((res["open"], res["stale_count"], res["unit"], res["open_size"], res["stale_size"]),
                         (9, 4, "points", 30, 10))
        self.assertEqual(res["by_kind"], {"stalled": 1, "blocked": 0, "not started": 1, "marked stale": 2})
        self.assertEqual(self.rows(res), [("9", "stalled", 54, 2, ("bob",)), ("21", "not started", 49, 2, ("carol",)),
                                          ("22", "marked stale", 5, 3, ("bob",)), ("18", "marked stale", 2, 3, ("alice",))])
        self.assertEqual(dict(res["per_person"]), {"bob": 2, "carol": 1, "alice": 1})
        self.assertEqual(sorted(i["key"].split("-")[-1].lstrip("#") for i in res["aging"]), ["16", "19", "20", "23"])

    def check_sprint5(self, res, days, expected, by_kind):
        self.assertEqual(res["stale_days"], days)
        self.assertEqual((res["period"]["name"], res["period_left"], res["open"]), ("Sprint 5", 10, 3))
        self.assertEqual(self.rows(res), expected)
        self.assertEqual(res["by_kind"], by_kind)

    def test_plane_whole_project(self):
        res = self.run_report("plane", "report", "--all")
        self.assertEqual(res["project"], "Inventory (INV)")
        self.check_whole_project(res)

    def test_gitlab_whole_project_matches_plane(self):
        self.check_whole_project(self.run_report("gitlab", "report", "--all"))

    def test_shifted_dataset_gives_the_same_numbers_on_any_day(self):
        # Evals run on the real date: the mock moves every fixture date by (today - dataset date).
        for target in ("plane", "gitlab"):
            for today in ("2026-11-22", "2027-03-03"):
                with self.subTest(target=target, today=today):
                    res = self.run_report(target, "report", "--all", today=today)
                    self.assertEqual(res["today"], today)
                    self.check_whole_project(res)

    def test_current_sprint_default_threshold_only_the_marked_issue(self):
        for target in ("plane", "gitlab"):
            res = self.run_report(target, "report", "--period", "Sprint 5")
            self.check_sprint5(res, 14, [("18", "marked stale", 2, 3, ("alice",))],
                               {"stalled": 0, "blocked": 0, "not started": 0, "marked stale": 1})
            self.assertEqual([i["key"].lstrip("#").split("-")[-1] for i in res["aging"]], ["16"])

    def test_current_sprint_five_days_has_stalled_blocked_and_marked(self):
        for target in ("plane", "gitlab"):
            res = self.run_report(target, "report", "--period", "Sprint 5", "--stale-days", "5")
            self.check_sprint5(res, 5, [("16", "stalled", 8, 8, ("bob",)), ("17", "blocked", 7, 3, ("carol",)),
                                        ("18", "marked stale", 2, 3, ("alice",))],
                               {"stalled": 1, "blocked": 1, "not started": 0, "marked stale": 1})
            self.assertEqual((res["stale_size"], res["open_size"]), (14, 14))

    def test_periods_counts(self):
        for target in ("plane", "gitlab"):
            res = self.run_report(target, "periods")
            self.assertEqual([(p["name"], p["open"], p["stale"]) for p in res["periods"]],
                             [("Sprint 5", 3, 1), ("Sprint 6", 2, 0)])
            self.assertEqual(res["whole"], {"open": 9, "stale": 4})

    def test_projects_listing(self):
        for target, path in (("plane", "INV"), ("gitlab", "group/inv")):
            self.assertEqual([p["path"] for p in self.run_report(target, "projects")], [path])


if __name__ == "__main__":
    unittest.main()


class BlockedEndToEndTest(unittest.TestCase):
    """blocked.py in a subprocess against the shared fixtures: INV-17 / #17 is blocked by INV-16 / #16 since
    2026-11-05 (state change in Plane, label event in GitLab), with one comment from carol."""

    def run_cmd(self, target, *args, today="2026-11-12"):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_", "EVAL_"))}
        env.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY=today,
                   EVAL_DATASET_DATE="2026-11-12",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        project = "INV" if target == "plane" else "group/inv"
        extra = ["--rate", "6000"] if target == "plane" else []
        with tempfile.TemporaryDirectory() as cwd:
            out = subprocess.run([sys.executable, str(BLOCKED), args[0], "--target", target, "--project", project,
                                  *args[1:], *extra, "--format", "json"], env=env, cwd=cwd,
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        return json.loads(out.stdout)

    def check(self, res, prefix, dep):
        self.assertEqual(len(res["issues"]), 1)
        it = res["issues"][0]
        self.assertEqual((it["key"], it["assignees"], it["causes"]), (f"{prefix}17", ["carol"], ["dependency"]))
        self.assertEqual(round(it["blocked_days"]), 8)
        self.assertEqual([(b["key"], b["open"], b["assignees"]) for b in it["blocked_by"]], [(dep, True, ["bob"])])
        self.assertEqual(it["comment_count"], 1)
        self.assertIn("stock sync API", it["last_comment"]["body"])
        self.assertEqual(res["stale_excluded"], [])

    def test_whole_project_and_sprint5_both_tools(self):
        for target, prefix, dep in (("plane", "INV-", "INV-16"), ("gitlab", "#", "#16")):
            for scope in (["--all"], ["--period", "Sprint 5"]):
                with self.subTest(target=target, scope=scope):
                    res = self.run_cmd(target, "report", *scope)
                    self.check(res, prefix, dep)
                    self.assertEqual(res["issues"][0]["blocked_since"], "2026-11-05")

    def test_deep_finds_no_other_blocked_issue(self):
        self.check(self.run_cmd("plane", "report", "--all", "--deep"), "INV-", "INV-16")

    def test_shifted_dataset_moves_dates_not_numbers(self):
        res = self.run_cmd("plane", "report", "--all", today="2026-11-22")
        self.check(res, "INV-", "INV-16")
        self.assertEqual(res["issues"][0]["blocked_since"], "2026-11-15")

    def test_periods(self):
        for target in ("plane", "gitlab"):
            with self.subTest(target=target):
                res = self.run_cmd(target, "periods")
                self.assertEqual([(p["name"], p["open_issues"], p["blocked_label"]) for p in res["periods"]],
                                 [("Sprint 5", 3, 1), ("Sprint 6", 2, 0)])
                self.assertEqual(res["whole_project"], {"open_issues": 9, "blocked_label": 1})


class AssignEndToEndTest(unittest.TestCase):
    """assign.py against the shared fixtures. Areas: alice frontend (and INV-12 backend), bob backend, carol data
    (and INV-8 frontend); odd issues are features, even ones bugs; dave is a guest and ci-bot a bot (both left out).
    INV-23 / #23 (frontend feature, no cycle) is the only open unassigned issue."""

    def run_cmd(self, target, *args, today="2026-11-12", fmt="json"):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_", "EVAL_"))}
        env.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY=today,
                   EVAL_DATASET_DATE="2026-11-12",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        project = "INV" if target == "plane" else "group/inv"
        extra = ["--rate", "6000"] if target == "plane" else []
        with tempfile.TemporaryDirectory() as cwd:
            out = subprocess.run([sys.executable, str(ASSIGN), args[0], "--target", target, "--project", project,
                                  *args[1:], *extra, "--format", fmt], env=env, cwd=cwd,
                                 capture_output=True, text=True, timeout=120)
        return out

    def ok(self, out):
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        return json.loads(out.stdout)

    def test_scores_both_tools_and_any_day(self):
        for target in ("plane", "gitlab"):
            for today in ("2026-11-12", "2027-02-01"):
                with self.subTest(target=target, today=today):
                    text = self.run_cmd(target, "scores", today=today, fmt="md").stdout
                    self.assertIn("3 developer(s) · 23 issues read", text)
                    self.assertIn("| @alice | 15 | 0 | 100 |", text)
                    self.assertIn("| @bob | 100 | 0 | 0 |", text)
                    self.assertIn("| @carol | 0 | 100 | 16 |", text)
                    self.assertNotIn("dave", text)
                    self.assertNotIn("ci-bot", text)

    def test_suggest_member_for_the_frontend_issue(self):
        for target, key in (("plane", "INV-23"), ("gitlab", "#23")):
            with self.subTest(target=target):
                text = self.run_cmd(target, "suggest-member", "--issue", key, fmt="md").stdout
                self.assertIn("**Recommendation: @alice** (score 91)", text)
                self.assertIn("| 2 | @carol | 38 |", text)
                self.assertIn("| 3 | @bob | 8 |", text)

    def test_suggest_issue_whole_project_and_empty_sprint(self):
        text = self.run_cmd("plane", "suggest-issue", "--member", "alice", "--all", fmt="md").stdout
        self.assertIn("**Recommendation: INV-23**", text)
        text = self.run_cmd("gitlab", "suggest-issue", "--member", "carol", fmt="md").stdout
        self.assertIn("Scope: **Sprint 5**", text)
        self.assertIn("No open, unassigned, unblocked issue in this scope", text)

    def test_assign_without_yes_is_a_plan_and_with_yes_the_mock_refuses(self):
        out = self.run_cmd("plane", "assign", "--issue", "INV-23", "--member", "alice", fmt="md")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("PLAN, nothing written: INV-23 (Inventory task 23): assignees none -> @alice", out.stdout)
        self.assertNotIn("[eval-mock]", out.stderr)
        for target, key in (("plane", "INV-23"), ("gitlab", "23")):
            with self.subTest(target=target):
                out = self.run_cmd(target, "assign", "--issue", key, "--member", "alice", "--yes", fmt="md")
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("[eval-mock] WRITE REFUSED", out.stderr)
                self.assertNotIn("Traceback", out.stderr)


class PopulateEndToEndTest(unittest.TestCase):
    """populate.py with the default labels against the shared fixtures (dry run), and a real run that the mock
    refuses: nothing may be reported as created when the write failed."""

    def run_cmd(self, target, project, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_", "EVAL_"))}
        env.update(PYTHONPATH=str(MOCK_DIR), EVAL_FIXTURES=str(FIXTURES), EVAL_TODAY="2026-11-12",
                   EVAL_DATASET_DATE="2026-11-12",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        extra = ["--rate", "6000"] if target == "plane" else []
        with tempfile.TemporaryDirectory() as cwd:
            return subprocess.run([sys.executable, str(POPULATE), "--target", target, "--project", project,
                                   "--labels", str(POPULATE_LABELS), *args, *extra], env=env, cwd=cwd,
                                  capture_output=True, text=True, timeout=120)

    def test_dry_run_plans(self):
        out = self.run_cmd("plane", "INV", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        self.assertIn("Summary: 8 to create, 10 reused", out.stdout)
        self.assertIn("+ would create state: In Review [started] for workflow::review", out.stdout)
        out = self.run_cmd("gitlab", "group/inv", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        self.assertIn("Summary: 21 to create, 6 reused", out.stdout)
        self.assertIn("+ would create board: Kanban", out.stdout)

    def test_unknown_projects(self):
        out = self.run_cmd("gitlab", "group/shop", "--dry-run")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("GitLab project 'group/shop' not found", out.stderr + out.stdout)
        self.assertNotIn("[eval-mock] MISS", out.stderr)
        out = self.run_cmd("plane", "Shop", "--dry-run", "--create-project", "--identifier", "SHOP")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("+ would create project: Shop (SHOP)", out.stdout)

    def test_refused_write_is_never_reported_as_created(self):
        for target, project in (("gitlab", "group/inv"), ("plane", "INV")):
            with self.subTest(target=target):
                out = self.run_cmd(target, project)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("[eval-mock] WRITE REFUSED", out.stderr)
                self.assertNotIn("+ created", out.stdout)
