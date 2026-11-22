"""CLI of stability.py: config, arguments, rendering and end-to-end runs on the shared eval fixtures."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit.scope_stability.helpers import issue, period, ss

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".apm/skills/scope-stability/scripts/stability.py"
EVALS = ROOT / "tests/evals"
ENV = {"GITLAB_URL": "https://gl.example.com", "GITLAB_TOKEN": "glpat-secret-123"}


def run(*argv, env=None):
    out, err, code, cwd = io.StringIO(), io.StringIO(), 0, os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, env or {}, clear=True), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                try:
                    ss.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
                    if isinstance(e.code, str):
                        err.write(e.code)
        finally:
            os.chdir(cwd)
    return code, out.getvalue(), err.getvalue()


class Fake:
    kind, name, tz, tz_note = "milestone", "Demo", ss.UTC, None

    def __init__(self, items):
        self.items = items

    def periods(self):
        return [period()]

    def issues(self, since):
        return self.items


class ConfigTest(unittest.TestCase):
    def test_not_configured_names_the_variables(self):
        for target, names in (("gitlab", ["GITLAB_URL", "GITLAB_TOKEN"]),
                              ("plane", ["PLANE_URL", "PLANE_WORKSPACE", "PLANE_API_KEY"])):
            code, out, err = run("report", "--target", target, "--project", "X")
            self.assertEqual((code, out), (2, ""))
            for text in names + ["NOT CONFIGURED", "export ", "$env:"]:
                self.assertIn(text, err)

    def test_no_secret_in_the_output(self):
        code, out, err = run("report", "--target", "gitlab", "--project", "X", env={"GITLAB_TOKEN": "glpat-secret-123"})
        self.assertEqual(code, 2)
        self.assertNotIn("glpat-secret-123", out + err)

    def test_invalid_arguments(self):
        for extra in (["--last", "0"], ["--grace-days", "-1"], ["--timezone", "Mars/Base"]):
            code, _, err = run("report", "--target", "gitlab", "--project", "X", *extra, env=ENV)
            self.assertNotEqual(code, 0, extra)
            self.assertNotIn("Traceback", err)

    def test_report_needs_a_project(self):
        self.assertIn("--project", run("report", "--target", "gitlab", env=ENV)[2])

    def test_api_error_is_a_message(self):
        with mock.patch.object(ss.GitLab, "periods", side_effect=ss.ApiError(401, "no", "GET", "u")):
            code, _, err = run("report", "--target", "gitlab", "--project", "X", env=ENV)
        self.assertEqual(code, 1)
        self.assertIn("API error", err)


class RenderTest(unittest.TestCase):
    def report(self, items, *extra):
        with mock.patch.object(ss, "GitLab", return_value=Fake(items)):
            return run("report", "--target", "gitlab", "--project", "X", "--today", "2026-04-01", *extra, env=ENV)

    def test_markdown_has_table_changes_and_thirds(self):
        items = [issue("B", points=10.0), issue("A|B", start=None, moves=[("03-05", "S")]),
                 issue("R", moves=[("03-13", "S")])]
        code, out, err = self.report(items)
        self.assertEqual(code, 0, err)
        for text in ("# Scope stability", "**67%**", "unstable", "## When it changed", "added", "A\\|B"):
            self.assertIn(text, out)

    def test_json_parses(self):
        res = json.loads(self.report([issue()], "--format", "json")[1])
        self.assertEqual(res["rows"][0]["stability"], 1.0)

    def test_long_lists_are_cut_in_markdown_only(self):
        items = [issue("B")] + [issue(f"A{n}", start=None, moves=[("03-05", "S")]) for n in range(ss.MAX_LISTED + 3)]
        self.assertIn("3 more", self.report(items)[1])
        self.assertEqual(len(json.loads(self.report(items, "--format", "json")[1])["rows"][0]["changes"]), ss.MAX_LISTED + 3)


class PercentTest(unittest.TestCase):
    def test_rounding_never_crosses_a_rating_limit(self):
        for value, shown in ((1 - 31 / 54, "43%"), (0.896, "89%"), (0.748, "74%"), (0.9, "90%"), (1.0, "100%"), (None, "—")):
            self.assertEqual(ss.pct(value), shown)


class EndToEndTest(unittest.TestCase):
    """Against tests/evals/fixtures. The expected numbers were worked out by hand from the fixture events."""

    def report(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "PLANE_", "EVAL_"))}
        env.update(PYTHONPATH=str(EVALS / "mock"), EVAL_FIXTURES=str(EVALS / "fixtures"), EVAL_TODAY="2026-11-12",
                   EVAL_DATASET_DATE="2026-11-12", EVAL_PLANE_NO_ESTIMATES="1",
                   GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                   PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake")
        with tempfile.TemporaryDirectory() as cwd:
            out = subprocess.run([sys.executable, str(SCRIPT), "report", *args, "--format", "json"], env=env, cwd=cwd,
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("[eval-mock]", out.stderr)
        return json.loads(out.stdout)

    def test_gitlab_and_plane_agree(self):
        g = self.report("--target", "gitlab", "--project", "group/inv")
        p = self.report("--target", "plane", "--project", "INV", "--rate", "6000")
        keys = ("name", "initial", "added", "removed", "reestimated", "cancelled", "changed", "stability")
        self.assertEqual([[r[k] for k in keys] for r in g["rows"]], [[r[k] for k in keys] for r in p["rows"]])
        self.assertEqual([r["name"] for r in g["rows"]], [f"Sprint {n}" for n in range(1, 6)])
        self.assertEqual(g["unit"], "points")
        #                           initial, added, removed, re-estimated, changed
        expected = {"Sprint 1": (13, 2, 2, 0, 4),  # #4 (3) was cancelled on the start day: planning
                    "Sprint 2": (10, 8, 2, 0, 10), "Sprint 3": (4, 10, 2, 0, 12), "Sprint 4": (2, 8, 0, 0, 8),
                    "Sprint 5": (16, 2, 0, 1, 3)}  # #18 entered with 2 and was re-estimated to 3
        for r in g["rows"]:
            self.assertEqual(tuple(r[k] for k in ("initial", "added", "removed", "reestimated", "changed")),
                             expected[r["name"]], r["name"])
        self.assertEqual([ss.pct(r["stability"]) for r in g["rows"]], ["69%", "0%", "0%", "0%", "81%"])
        self.assertEqual((g["summary"]["median"], g["summary"]["rating"]), (0, "unstable"))


if __name__ == "__main__":
    unittest.main()
