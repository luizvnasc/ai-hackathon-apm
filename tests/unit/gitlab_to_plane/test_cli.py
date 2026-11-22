"""CLI of migrate.py: config handling, argparse, dry run and re-run end to end, failures without tracebacks."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import load
from tests.unit.gitlab_to_plane.fake_plane import FIXTURES, FakePlane
from tests.unit.gitlab_to_plane.test_clients import Resp, http_error

mig = load("gitlab-to-plane", "migrate.py")

GITLAB_ENV = {"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "glpat-secret-123"}
PLANE_ENV = {"PLANE_URL": "https://plane.example.com", "PLANE_WORKSPACE": "demo-ws", "PLANE_API_KEY": "plane-secret-456"}
SECRETS = ("glpat-secret-123", "plane-secret-456")
EXPORT = (FIXTURES / "gitlab_export.json").read_text(encoding="utf-8")


class Run:
    """Runs main(argv) in an empty temp dir (no .env, no .mcp.json) with only `env` set.

    Files written by the run (e.g. --report) are returned in `self.files`."""

    def __init__(self, test, env=None):
        self.test, self.env, self.files = test, env or {}, {}

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
                        mig.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
                self.files = {n: open(n, encoding="utf-8").read() for n in os.listdir(".")}
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


def with_plane(fake):
    """Route every Plane API call of main() to the in-memory fake."""
    return mock.patch.object(mig.Plane, "_request", lambda self, method, path, body=None, params=None:
                             fake(method, path, body, params))


class ConfigTest(unittest.TestCase):
    def assert_not_configured(self, code, out, err, names, absent=()):
        self.assertEqual(code, 2)
        self.assertIn("NOT CONFIGURED", err)
        for n in names:
            self.assertIn(n, err)
        for n in absent:
            self.assertNotIn(n, err)
        self.assertIn("export ", err)
        self.assertIn("$env:", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)

    def test_gitlab_project_needs_both_tools(self):
        code, out, err = Run(self)("--gitlab-project", "demo/app", "--dry-run")
        self.assert_not_configured(code, out, err, ["GITLAB_URL", "GITLAB_TOKEN", "PLANE_URL", "PLANE_WORKSPACE",
                                                    "PLANE_API_KEY"])

    def test_data_needs_only_plane(self):
        code, out, err = Run(self)("--data", "export.json", "--dry-run", files={"export.json": EXPORT})
        self.assert_not_configured(code, out, err, ["PLANE_URL", "PLANE_WORKSPACE", "PLANE_API_KEY"], ["GITLAB_"])

    def test_listing_projects_needs_only_gitlab(self):
        code, out, err = Run(self)("--list-gitlab-projects")
        self.assert_not_configured(code, out, err, ["GITLAB_URL", "GITLAB_TOKEN"], ["PLANE_"])

    def test_partial_config_names_only_the_missing_and_never_the_token(self):
        env = {"PLANE_API_KEY": "plane-secret-456", "GITLAB_TOKEN": "glpat-secret-123"}
        code, out, err = Run(self, env)("--gitlab-project", "demo/app")
        self.assert_not_configured(code, out, err, ["GITLAB_URL", "PLANE_URL", "PLANE_WORKSPACE"])
        self.assertEqual(err.splitlines()[0], "NOT CONFIGURED: missing GITLAB_URL, PLANE_URL, PLANE_WORKSPACE.")

    def test_options_replace_url_and_workspace(self):
        env = {"PLANE_API_KEY": "plane-secret-456"}
        fake = FakePlane(mig)
        with with_plane(fake):
            code, _, err = Run(self, env)("--data", "e.json", "--plane-url", "https://p.example.com",
                                          "--workspace", "ws", "--dry-run", files={"e.json": EXPORT})
        self.assertEqual(code, 0, err)

    def test_dotenv_fills_missing_values_and_env_wins(self):
        dotenv = "export PLANE_URL=https://plane.example.com\nPLANE_WORKSPACE='from-dotenv'\nPLANE_API_KEY=\"plane-secret-456\"\n"
        seen = {}
        real_init = mig.Plane.__init__

        def capture(self, base_url, api_key, workspace, *a):
            seen["workspace"] = workspace
            real_init(self, base_url, api_key, workspace, *a)

        fake = FakePlane(mig)
        with with_plane(fake), mock.patch.object(mig.Plane, "__init__", capture):
            code, out, err = Run(self, {"PLANE_WORKSPACE": "from-env"})(
                "--data", "e.json", "--dry-run", files={".env": dotenv, "e.json": EXPORT})
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["workspace"], "from-env")
        for s in SECRETS:
            self.assertNotIn(s, out + err)


class ArgparseTest(unittest.TestCase):
    def test_a_source_is_required_and_sources_are_exclusive(self):
        code, _, err = Run(self, PLANE_ENV)()
        self.assertEqual(code, 2)
        self.assertIn("one of the arguments", err)
        code, _, err = Run(self, PLANE_ENV)("--data", "x.json", "--gitlab-project", "a/b")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)

    def test_defaults(self):
        parsed = {}

        def stop(**kw):
            raise SystemExit(0)

        real = mig.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        with mock.patch.object(mig, "require_config", side_effect=stop), \
                mock.patch.object(mig.argparse.ArgumentParser, "parse_args", capture):
            Run(self, dict(PLANE_ENV, **GITLAB_ENV))("--gitlab-project", "demo/app")
        self.assertEqual(parsed["rate"], 60)
        self.assertFalse(parsed["dry_run"])
        self.assertFalse(parsed["no_comments"])
        self.assertIsNone(parsed["identifier"])
        self.assertIsNone(parsed["report"])
        self.assertEqual(parsed["plane_url"], "https://plane.example.com")
        self.assertEqual(parsed["workspace"], "demo-ws")
        self.assertEqual(parsed["gitlab_url"], "https://gitlab.example.com")


class MigrateCommandTest(unittest.TestCase):
    def migrate(self, fake, *extra, export=EXPORT):
        runner = Run(self, PLANE_ENV)
        with with_plane(fake):
            code, out, err = runner("--data", "export.json", *extra, files={"export.json": export})
        for s in SECRETS:
            self.assertNotIn(s, out + err)
        return code, out, err, runner.files

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        fake = FakePlane(mig)
        code, out, err, _ = self.migrate(fake, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.writes(), [])
        self.assertIn("DRY RUN — nothing will be written.", out)
        self.assertIn("4 work items -> Plane https://plane.example.com workspace 'demo-ws', project INVM", out)
        self.assertIn("Project: creating Inventory Manager (INVM)", out)
        self.assertIn("Estimate: Story points with points 1, 3, 5 (3 added)", out)
        self.assertIn("Assignees: 1/3 mapped to Plane members; not found: bob, carol", out)  # no --user-map here
        self.assertIn("created: project 1,", out)
        self.assertIn("assignees not found in Plane (left unassigned): bob, carol", out)

    def test_migrate_then_rerun_creates_nothing_and_writes_the_report(self):
        fake = FakePlane(mig)
        code, out, err, files = self.migrate(fake, "--identifier", "invm", "--user-map", "bob=robert@example.com",
                                             "--report", "report.json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("DRY RUN", out)
        report = json.loads(files["report.json"])
        self.assertEqual(report["project"]["identifier"], "INVM")
        self.assertEqual(report["created"]["work_item"], 4)
        self.assertEqual(sorted(report["items"]), [f"demo/inventory-manager#{n}" for n in (1, 2, 3, 4)])
        self.assertIn("api_calls", report)  # 0 here: the fake replaces _request, which counts
        before = fake.counts()
        code, out, err, files = self.migrate(fake, "--identifier", "INVM", "--user-map", "bob=robert@example.com",
                                             "--report", "report.json")
        self.assertEqual(code, 0, err)
        self.assertIn("created: -", out)
        self.assertEqual(json.loads(files["report.json"])["created"], {})
        self.assertEqual(fake.counts(), before)

    def test_plane_api_error_ends_with_a_message_not_a_traceback(self):
        fake = FakePlane(mig)
        fake.fail("GET", r"^/projects/$", 401, {"detail": "Invalid API key"})
        code, out, err, _ = self.migrate(fake, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("Plane API error: GET /projects/ -> HTTP 401", err)
        self.assertNotIn("Traceback", err)

    def test_unreachable_plane_ends_with_a_message(self):
        # Regression: a connection error escaped main() as a URLError traceback.
        runner = Run(self, PLANE_ENV)
        with mock.patch.object(mig.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")), \
                mock.patch.object(mig.time, "sleep"):
            code, out, err = runner("--data", "e.json", "--dry-run", files={"e.json": EXPORT})
        self.assertEqual(code, 1)
        self.assertIn("Plane API error", err)
        self.assertIn("refused", err)

    def test_empty_export(self):
        code, _, err, _ = self.migrate(FakePlane(mig), "--dry-run", export='{"project": {"path": "a/b"}, "work_items": []}')
        self.assertEqual(code, 1)
        self.assertIn("no work items in the export", err)

    def test_missing_or_invalid_export_file(self):
        # Regression: a missing --data file or broken JSON ended with a traceback.
        runner = Run(self, PLANE_ENV)
        code, _, err = runner("--data", "missing.json", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("cannot read missing.json", err)
        code, _, err = runner("--data", "bad.json", "--dry-run", files={"bad.json": "{not json"})
        self.assertEqual(code, 1)
        self.assertIn("cannot read bad.json", err)

    def test_compact_list_export_derives_the_path_from_the_reference(self):
        items = [{"iid": 1, "title": "A", "state": "opened", "reference": "grp/my-app#1"}]
        code, out, err, _ = self.migrate(FakePlane(mig), "--dry-run", export=json.dumps(items))
        self.assertEqual(code, 0, err)
        self.assertIn("GitLab grp/my-app: 1 work items", out)
        self.assertIn("project MYA", out)


class ListProjectsTest(unittest.TestCase):
    def test_lists_all_pages_as_a_table(self):
        pages = [Resp([{"path_with_namespace": "demo/a", "web_url": "https://gitlab.example.com/demo/a", "name": "A",
                        "open_issues_count": 3}], headers={"X-Next-Page": "2"}),
                 Resp([{"path_with_namespace": "demo/b", "web_url": "https://gitlab.example.com/demo/b", "name": "B",
                        "marked_for_deletion_on": "2026-01-01"},
                       {"path_with_namespace": "demo/c", "web_url": "https://gitlab.example.com/demo/c", "name": "C"}],
                      headers={"X-Next-Page": ""})]
        with mock.patch.object(mig.urllib.request, "urlopen", side_effect=pages) as urlopen:
            code, out, err = Run(self, GITLAB_ENV)("--list-gitlab-projects")
        self.assertEqual(code, 0, err)
        self.assertIn("| 1 | [demo/a](https://gitlab.example.com/demo/a) | A | 3 |", out)
        self.assertIn("| 2 | [demo/c](https://gitlab.example.com/demo/c) | C | — |", out)
        self.assertNotIn("demo/b", out)
        urls = [c[0][0].full_url for c in urlopen.call_args_list]
        self.assertIn("page=2", urls[1])
        self.assertFalse(any("glpat" in u for u in urls))

    def test_errors_end_with_a_message(self):
        for exc in (http_error(401, b'{"message": "401 Unauthorized"}'), urllib.error.URLError("no route")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(mig.urllib.request, "urlopen", side_effect=[exc]):
                    code, out, err = Run(self, GITLAB_ENV)("--list-gitlab-projects")
                self.assertEqual(code, 1)
                self.assertIn("GitLab", err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("glpat-secret-123", out + err)


class EstimateApiBugCliTest(unittest.TestCase):
    def test_explains_the_bug_and_the_way_out_without_writing(self):
        for extra in (["--dry-run"], []):
            fake = FakePlane(mig, estimates_routed=False)
            with with_plane(fake):
                code, out, err = Run(self, PLANE_ENV)("--data", "export.json", *extra, files={"export.json": EXPORT})
            self.assertNotEqual(code, 0)
            for text in ("ESTIMATES UNAVAILABLE", "makeplane/plane/issues/9933", "--no-points", "Nothing was written"):
                self.assertIn(text, err)
            self.assertNotIn("Summary", out)
            self.assertNotIn("Traceback", err)
            self.assertEqual(fake.writes(), [])
            for s in SECRETS:
                self.assertNotIn(s, out + err)

    def test_no_points_option(self):
        fake = FakePlane(mig, estimates_routed=False)
        with with_plane(fake):
            code, out, err = Run(self, PLANE_ENV)("--data", "export.json", "--dry-run", "--no-points",
                                                  files={"export.json": EXPORT})
        self.assertEqual(code, 0, err)
        self.assertIn("warning: --no-points", out)
        self.assertNotIn("estimate 1", out)
        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
