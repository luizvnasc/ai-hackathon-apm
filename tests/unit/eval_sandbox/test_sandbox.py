"""The eval sandbox: proxy policy, container flags, what enters, and that configs fail closed (no Docker)."""
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

EVALS = pathlib.Path(__file__).resolve().parents[2] / "evals"


def load(name):
    spec = importlib.util.spec_from_file_location("eval_sandbox_" + name, EVALS / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run, proxy = load("run"), load("proxy")
OK = {"model": "claude-haiku-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}


def values(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


class ProxyPolicyTest(unittest.TestCase):
    def refused(self, body=None, path="/v1/messages", method="POST"):
        return proxy.refusal(method, path, OK if body is None else body)

    def test_messages_api_is_allowed(self):
        for path in proxy.PATHS:
            self.assertIsNone(self.refused(path=path))
        self.assertIsNone(self.refused({**OK, "tools": [{"name": "Bash", "input_schema": {}}]}))

    def test_other_routes_and_methods_are_refused(self):
        for path in ("/v1/files", "/v1/messages/batches", "/v1/messages/../files", "/v1/messages?x=1",
                     "https://example.com/v1/messages"):
            self.assertTrue(self.refused(path=path), path)
        self.assertTrue(self.refused(method="GET"))

    def test_only_haiku(self):
        for model in ("claude-opus-5-5", "claude-sonnet-5-5", "", None):
            self.assertTrue(self.refused({**OK, "model": model}), model)

    def test_nothing_that_makes_the_api_fetch_or_run(self):
        for tool in ({"type": "web_search_20250305"}, {"type": "web_fetch_20250910"}, {"type": "code_execution_20250825"},
                     "not an object"):
            self.assertTrue(self.refused({**OK, "tools": [tool]}), tool)
        for field in ("mcp_servers", "container", "unknown_field"):
            self.assertTrue(self.refused({**OK, field: []}), field)
        for source in ({"type": "url", "url": "http://localhost"}, {"type": "file", "file_id": "f"}):
            nested = [{"type": "tool_result", "content": [{"type": "image", "source": source}]}]
            self.assertTrue(self.refused({**OK, "messages": [{"role": "user", "content": nested}]}), source)
            self.assertTrue(self.refused({**OK, "system": [{"type": "document", "source": source}]}), source)

    def test_body_must_be_an_object(self):
        self.assertTrue(self.refused([]))

    def test_agent_headers_cannot_choose_the_credential(self):
        sent = {"x-api-key": "agent", "authorization": "Bearer agent", "host": "elsewhere", "anthropic-beta": "a"}
        key = proxy.upstream_headers(sent, "api_key", "real")
        self.assertEqual(key["x-api-key"], "real")
        self.assertNotIn("authorization", key)
        self.assertNotIn("host", key)
        oauth = proxy.upstream_headers(sent, "oauth", "real")
        self.assertEqual(oauth["authorization"], "Bearer real")
        self.assertNotIn("x-api-key", oauth)
        self.assertEqual(oauth["anthropic-beta"], "a,oauth-2025-04-20")


class ContainerFlagsTest(unittest.TestCase):
    def test_agent_has_no_network_mount_or_secret(self):
        for kind in ("api_key", "oauth"):
            args = run.agent_args("img", "n", "vol", kind, ["c", "1"])
            self.assertEqual(values(args, "--network"), ["none"])
            self.assertEqual(values(args, "-v"), ["vol:/run/proxy:ro"])
            self.assertEqual(values(args, "--cap-drop"), ["ALL"])
            self.assertEqual(values(args, "--user"), ["10001:10001"])
            self.assertIn("--read-only", args)
            self.assertIn("no-new-privileges", args)
            for flag in ("--privileged", "--mount", "--pid", "--add-host", "--env-file"):
                self.assertNotIn(flag, args)
            for env in values(args, "-e"):
                self.assertIn("=", env)  # a bare name would copy the host's value
                if env.split("=")[0] in run.SECRET_VARS:
                    self.assertEqual(env.split("=", 1)[1], run.DUMMY)

    def test_proxy_gets_the_secret_only_on_stdin(self):
        args = run.proxy_args("img", "n", "vol", fake=False)
        self.assertEqual(values(args, "-e"), [])
        self.assertEqual(values(args, "-v"), ["vol:/run/proxy"])
        self.assertEqual(values(run.proxy_args("img", "n", "vol", fake=True), "--network"), ["none"])

    def test_docker_cli_does_not_inherit_the_secrets(self):
        with mock.patch.dict(os.environ, {k: "secret" for k in run.SECRET_VARS}):
            self.assertFalse(set(run.SECRET_VARS) & set(run.docker_env()))

    def test_no_credential_means_not_configured(self):
        with self.assertRaises(SystemExit) as cm:
            run.credential({})
        self.assertIn("NOT CONFIGURED", str(cm.exception.code))

    def test_output_cannot_drive_the_terminal(self):
        self.assertEqual(run.clean("\x1b[31mred\x1b[0m\x1b]52;c;eA==\x07\r\n"), "red]52;c;eA==\n")

    def test_unknown_skill_is_refused_before_anything_runs(self):
        for name in ("../x", "nope", "selftest"):
            with mock.patch.object(run, "build_image", side_effect=AssertionError("must not run")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run.main([name])


class PayloadTest(unittest.TestCase):
    def test_only_regular_unignored_skill_and_eval_files_enter(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = pathlib.Path(tmp)
            secret = pathlib.Path(outside, "secret")
            secret.write_text("secret")
            files = [".apm/skills/a/SKILL.md", ".apm/skills/a/.env", ".apm/skills/a/.env.local",
                     "tests/evals/a/promptfooconfig.yaml", "tests/evals/ignored.txt", "README.md", ".env",
                     "shared/config.py", "mcp/server.py", "mcp/metrics/net.py", "mcp/.env", "tests/unit/x.py"]
            for rel in files:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x")
            (root / ".gitignore").write_text("ignored.txt\n")
            (root / "tests/evals/link").symlink_to(secret)
            (root / "tests/evals/linkdir").symlink_to(outside)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            names = sorted(tarfile.open(fileobj=io.BytesIO(run.payload(root))).getnames())
        self.assertEqual(names, [".apm/skills/a/SKILL.md", "mcp/metrics/net.py", "mcp/server.py",
                                 "tests/evals/a/promptfooconfig.yaml"])


class ConfigsFailClosedTest(unittest.TestCase):
    def test_every_agent_works_in_a_path_that_exists_only_in_the_sandbox(self):
        configs = sorted(EVALS.glob("*/promptfooconfig.yaml"))
        self.assertTrue(configs)
        for path in configs:
            dirs = [l.split(":", 1)[1].split("#")[0].strip() for l in path.read_text().splitlines() if l.strip().startswith("working_dir:")]
            self.assertTrue(dirs, path)
            self.assertEqual(set(dirs), {"/sandbox/tests/evals/.workspace"}, path)


if __name__ == "__main__":
    unittest.main()
