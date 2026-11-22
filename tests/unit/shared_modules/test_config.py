"""shared/config.py: the .env rules and the NOT CONFIGURED help.

The rules are tested here once. That each skill really uses them is checked in tests/unit/security.
"""
import contextlib
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load_shared

config = load_shared("config")


def load_dotenv(content, environment=None):
    """Run load_dotenv on a .env with `content`. Returns (resulting environment, stderr)."""
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder, ".env")
        path.write_text(content)
        with mock.patch.dict(os.environ, environment or {}, clear=True), contextlib.redirect_stderr(stderr):
            config.load_dotenv(str(path))
            return dict(os.environ), stderr.getvalue()


def require_config(environment=None, **arguments):
    """Run require_config. Returns (exit code or None, stdout, stderr)."""
    stdout, stderr, code = io.StringIO(), io.StringIO(), None
    with mock.patch.dict(os.environ, environment or {}, clear=True), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            config.require_config(**arguments)
        except SystemExit as stop:
            code = stop.code
    return code, stdout.getvalue(), stderr.getvalue()


class DotenvTest(unittest.TestCase):
    def test_fills_what_is_not_set(self):
        environment, _ = load_dotenv("GITLAB_TOKEN=abc\nPLANE_WORKSPACE=acme\n")
        self.assertEqual(environment, {"GITLAB_TOKEN": "abc", "PLANE_WORKSPACE": "acme"})

    def test_environment_wins(self):
        environment, _ = load_dotenv("PLANE_WORKSPACE=from-file\n", {"PLANE_WORKSPACE": "from-env"})
        self.assertEqual(environment["PLANE_WORKSPACE"], "from-env")

    def test_accepts_export_and_quotes(self):
        environment, _ = load_dotenv("export GITLAB_TOKEN='abc'\nPLANE_WORKSPACE=\"acme\"\n")
        self.assertEqual(environment, {"GITLAB_TOKEN": "abc", "PLANE_WORKSPACE": "acme"})

    def test_reads_only_gitlab_and_plane_variables(self):
        # A cloned .env must not be able to set things like PATH or HTTPS_PROXY
        environment, _ = load_dotenv("HTTPS_PROXY=http://evil\nPATH=/evil\n# GITLAB_TOKEN=commented\n")
        self.assertEqual(environment, {})

    def test_empty_values_are_not_set(self):
        environment, _ = load_dotenv("GITLAB_TOKEN=\nPLANE_WORKSPACE=''\n")
        self.assertEqual(environment, {})

    def test_first_of_repeated_lines_wins(self):
        environment, _ = load_dotenv("GITLAB_TOKEN=first\nGITLAB_TOKEN=second\n")
        self.assertEqual(environment["GITLAB_TOKEN"], "first")

    def test_missing_file_changes_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config.load_dotenv("/no/such/file/.env")
            self.assertEqual(dict(os.environ), {})

    def test_url_and_token_of_the_same_file_are_accepted(self):
        environment, warning = load_dotenv("GITLAB_URL=https://gl\nGITLAB_TOKEN=abc\n")
        self.assertEqual(environment["GITLAB_URL"], "https://gl")
        self.assertEqual(warning, "")

    def test_url_without_the_token_of_its_file_is_ignored(self):
        environment, warning = load_dotenv("PLANE_URL=https://evil\n", {"PLANE_API_KEY": "secret"})
        self.assertNotIn("PLANE_URL", environment)
        self.assertIn("IGNORED PLANE_URL", warning)
        self.assertNotIn("https://evil", warning)

    def test_no_warning_when_the_environment_already_has_the_url(self):
        # The file's URL is simply overridden: warning here would send users to fix a working setup
        environment, warning = load_dotenv("GITLAB_URL=https://other\n",
                                           {"GITLAB_URL": "https://mine", "GITLAB_TOKEN": "secret"})
        self.assertEqual(environment["GITLAB_URL"], "https://mine")
        self.assertEqual(warning, "")


class RequireConfigTest(unittest.TestCase):
    def test_names_everything_missing_for_gitlab(self):
        code, _, stderr = require_config(gitlab=True)
        self.assertEqual(code, 2)
        self.assertEqual(stderr.splitlines()[0], "NOT CONFIGURED: missing GITLAB_URL, GITLAB_TOKEN.")

    def test_names_everything_missing_for_plane(self):
        code, _, stderr = require_config(plane=True)
        self.assertEqual(code, 2)
        self.assertEqual(stderr.splitlines()[0], "NOT CONFIGURED: missing PLANE_URL, PLANE_WORKSPACE, PLANE_API_KEY.")

    def test_blank_values_count_as_missing(self):
        code, _, stderr = require_config({"GITLAB_TOKEN": ""}, gitlab=True, gitlab_url="")
        self.assertEqual(code, 2)
        self.assertEqual(stderr.splitlines()[0], "NOT CONFIGURED: missing GITLAB_URL, GITLAB_TOKEN.")

    def test_names_only_what_is_missing_and_never_shows_a_token(self):
        environment = {"GITLAB_TOKEN": "glpat-secret-123", "PLANE_API_KEY": "plane-secret-456"}
        code, stdout, stderr = require_config(environment, plane=True, plane_url="https://plane")
        self.assertEqual(code, 2)
        self.assertEqual(stderr.splitlines()[0], "NOT CONFIGURED: missing PLANE_WORKSPACE.")
        for secret in environment.values():
            self.assertNotIn(secret, stdout + stderr)

    def test_help_covers_every_system_and_goes_to_stderr(self):
        _, stdout, stderr = require_config(gitlab=True, scope="api")
        self.assertEqual(stdout, "")
        for text in ("export GITLAB_URL", "$env:GITLAB_URL", "setx GITLAB_URL", "set GITLAB_URL=", ".env", "scope api"):
            self.assertIn(text, stderr)

    def test_complete_configuration_passes_silently(self):
        environment = {"GITLAB_TOKEN": "abc", "PLANE_API_KEY": "def"}
        result = require_config(environment, gitlab=True, plane=True, gitlab_url="https://gl",
                                plane_url="https://plane", workspace="acme")
        self.assertEqual(result, (None, "", ""))


if __name__ == "__main__":
    unittest.main()
