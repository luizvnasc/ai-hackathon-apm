"""CLI of populate.py: config handling, argparse defaults, dry run and real run end to end, the JSON report."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import SKILLS, load
from tests.unit.populate_project.fakes import GITLAB_TOKEN, PLANE_KEY, FakeGitLab, FakePlane

pop = load("populate-project", "populate.py")
DEFAULT = str(SKILLS / "populate-project" / "labels.default.json")
GITLAB_ENV = {"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": GITLAB_TOKEN}
PLANE_ENV = {"PLANE_URL": "https://plane.example.com", "PLANE_WORKSPACE": "demo-ws", "PLANE_API_KEY": PLANE_KEY}
SECRETS = (GITLAB_TOKEN, PLANE_KEY)


class Run:
    """Runs main(argv) in an empty temp dir (no .env, no .mcp.json) with only `env` set.

    `server` (a fake from fakes.py) replaces the module's http; `self.tmp` is the working dir
    of the last run, so a test can read files the run wrote there.
    """

    def __init__(self, test, env=None, server=None):
        self.test, self.env, self.server = test, env or {}, server
        self.files = {}

    def __call__(self, *argv, files=None):
        out, err, code = io.StringIO(), io.StringIO(), 0
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (files or {}).items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            os.chdir(tmp)
            patches = [mock.patch("time.sleep")]
            if self.server is not None:
                patches.append(mock.patch.object(pop, "http", self.server))
            try:
                with contextlib.ExitStack() as stack:
                    for p in patches:
                        stack.enter_context(p)
                    stack.enter_context(mock.patch.dict(os.environ, self.env, clear=True))
                    stack.enter_context(contextlib.redirect_stdout(out))
                    stack.enter_context(contextlib.redirect_stderr(err))
                    try:
                        pop.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
                self.files = {n: open(os.path.join(tmp, n), encoding="utf-8").read() for n in os.listdir(tmp)}
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


class ConfigTest(unittest.TestCase):
    def test_options_fill_the_url_and_workspace(self):
        server = FakePlane(pop)
        code, out, err = Run(self, {"PLANE_API_KEY": PLANE_KEY}, server)(
            "--target", "plane", "--project", "SHOP", "--labels", DEFAULT, "--dry-run",
            "--plane-url", "https://plane.example.com", "--workspace", "demo-ws")
        self.assertEqual(code, 0, err)

    def test_api_error_ends_with_a_message_not_a_traceback(self):
        def boom(method, url, headers, body=None):
            raise pop.ApiError(500, '{"message": "500 Internal Server Error"}', method, url)

        code, out, err = Run(self, GITLAB_ENV, boom)("--target", "gitlab", "--project", "acme/shop",
                                                     "--labels", DEFAULT)
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("HTTP 500", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)

    def test_bad_label_file_ends_with_a_message(self):
        server = FakeGitLab(pop)
        run = Run(self, GITLAB_ENV, server)
        code, _, err = run("--target", "gitlab", "--project", "acme/shop", "--labels", "missing.json")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read", err)
        code, _, err = run("--target", "gitlab", "--project", "acme/shop", "--labels", "bad.json",
                           files={"bad.json": "{not json"})
        self.assertIn("not valid JSON", err)
        self.assertEqual(server.calls, [])  # validated before any request


class ArgparseTest(unittest.TestCase):
    def test_defaults(self):
        parsed = {}
        real = pop.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        with mock.patch.object(pop, "require_config", side_effect=SystemExit(0)) as rc, \
                mock.patch.object(pop.argparse.ArgumentParser, "parse_args", capture):
            Run(self, PLANE_ENV)("--target", "plane", "--project", "SHOP", "--labels", DEFAULT)
        self.assertEqual(parsed["board_name"], "Kanban")
        self.assertEqual(parsed["rate"], 60)
        self.assertFalse(parsed["dry_run"])
        self.assertFalse(parsed["create_project"])
        self.assertFalse(parsed["priority_labels"])
        self.assertIsNone(parsed["identifier"])
        self.assertIsNone(parsed["report"])
        self.assertIsNone(parsed["gitlab_url"])
        self.assertEqual(parsed["plane_url"], "https://plane.example.com")
        self.assertEqual(parsed["workspace"], "demo-ws")
        self.assertEqual(rc.call_args.kwargs["scope"], "api")

    def test_required_and_choices(self):
        code, _, err = Run(self, PLANE_ENV)("--target", "jira", "--project", "x", "--labels", DEFAULT)
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        code, _, err = Run(self, PLANE_ENV)("--target", "plane", "--project", "x")
        self.assertEqual(code, 2)
        self.assertIn("--labels", err)


class EndToEndTest(unittest.TestCase):
    def test_gitlab_dry_run_then_real_run_then_rerun(self):
        server = FakeGitLab(pop)
        run = Run(self, GITLAB_ENV, server)
        base = ("--target", "gitlab", "--project", "acme/shop", "--labels", DEFAULT)
        code, out, err = run(*base, "--dry-run", "--report", "r.json")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("DRY RUN: nothing will be written"))
        self.assertIn("Summary: 27 to create, 0 reused", out)
        self.assertEqual(server.writes(), [])
        report = json.loads(run.files["r.json"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["target"], "gitlab")
        self.assertEqual(len(report["created"]), 27)  # 20 labels + 1 board + 6 lists

        code, out, err = run(*base)
        self.assertEqual(code, 0, err)
        self.assertIn("Summary: 27 created, 0 reused", out)
        code, out, err = run(*base, "--report", "r.json")
        self.assertIn("Summary: 0 created, 27 reused", out)
        report = json.loads(run.files["r.json"])
        self.assertEqual(report["created"], [])
        self.assertFalse(report["dry_run"])
        for s in SECRETS:
            self.assertNotIn(s, out + err + run.files["r.json"])

    def test_plane_dry_run_then_real_run_then_rerun(self):
        server = FakePlane(pop)
        run = Run(self, PLANE_ENV, server)
        base = ("--target", "plane", "--project", "SHOP", "--labels", DEFAULT)
        code, out, err = run(*base, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Summary: 14 to create, 4 reused", out)  # 2 states + 12 labels; 4 states reused
        self.assertEqual(server.writes(), [])
        run(*base)
        code, out, err = run(*base, "--report", "r.json")
        self.assertIn("Summary: 0 created, 18 reused", out)
        report = json.loads(run.files["r.json"])
        self.assertEqual(report["url"], "https://plane.example.com/demo-ws/projects/p-1/issues")
        self.assertIn("(choose the Board layout, grouped by state)", out)
        for s in SECRETS:
            self.assertNotIn(s, out + err + run.files["r.json"])


class ErrorTextTest(unittest.TestCase):
    def test_shows_500_characters_of_the_api_answer(self):
        refused = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"x" * 900))
        with mock.patch.object(pop.urllib.request, "urlopen", side_effect=refused):
            with self.assertRaises(pop.ApiError) as caught:
                pop.http("GET", "https://gitlab.example.com/api", {})
        self.assertTrue(str(caught.exception).endswith("HTTP 400: " + "x" * 500))


if __name__ == "__main__":
    unittest.main()
