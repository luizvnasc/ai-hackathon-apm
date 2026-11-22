"""No script may send a credential to a server the user did not configure.

Black box: every script runs as a real process against real HTTP servers on 127.0.0.1, with no mock.
`trusted` is the server the user configured, `other` is one that must never see a token.

Each rule runs on one script (REFERENCE), because the copies of shared/ are identical (checked in
tests/unit/shared_modules). WiringTest then checks that every script really uses them.
"""
import ast
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

SKILLS = pathlib.Path(__file__).resolve().parents[3] / ".apm" / "skills"
SCRIPTS = sorted(SKILLS.glob("*/scripts/*.py"))
REFERENCE = SKILLS / "team-velocity" / "scripts" / "velocity.py"
TOKENS = {"gitlab": ("GITLAB_URL", "GITLAB_TOKEN", "glpat-secret-123"),
          "plane": ("PLANE_URL", "PLANE_API_KEY", "plane-secret-456")}
CALL_HTTP = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("skill", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    m.http(sys.argv[2], sys.argv[3], {"PRIVATE-TOKEN": "glpat-secret-123", "X-API-Key": "plane-secret-456"},
           {"q": 1} if sys.argv[2] != "GET" else None)
except m.ApiError as e:
    print("ApiError", e)
"""


class Server:
    """Records every request. Paths in `redirects` answer (status, location); the others 404."""

    def __init__(self):
        self.seen, self.redirects = [], {}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                server.seen.append((self.command, self.path, str(self.headers)))
                hit = next((v for k, v in server.redirects.items() if self.path.startswith(k)), None)
                body = b"" if hit else json.dumps({"error": "not here"}).encode()
                self.send_response(hit[0] if hit else 404)
                if hit:
                    self.send_header("Location", hit[1])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = answer

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Case(unittest.TestCase):
    def setUp(self):
        self.trusted, self.other = Server(), Server()
        self.addCleanup(self.trusted.close)
        self.addCleanup(self.other.close)

    def reset(self):
        del self.trusted.seen[:], self.other.seen[:]

    def execute(self, argv, env=None, files=None):
        with tempfile.TemporaryDirectory() as cwd:
            for name, text in (files or {}).items():
                path = pathlib.Path(cwd, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            base = {"PATH": os.environ.get("PATH", ""), "NO_PROXY": "*"}
            if os.environ.get("SYSTEMROOT"):
                base["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            return subprocess.run([sys.executable, "-B", *argv], env={**base, **(env or {})}, cwd=cwd,
                                  capture_output=True, text=True, timeout=60)

    def cli(self, script, target, *extra, **kw):
        """The first command of each skill that makes a request."""
        if script.stem == "populate":
            args = ["--target", target, "--project", "demo", "--dry-run",
                    "--labels", str(script.parent.parent / "labels.default.json")]
        else:
            args = ["projects", "--target", target]
        return self.execute([str(script), *args, *extra], **kw)

    def env(self, target, url=True):
        url_key, token_key, token = TOKENS[target]
        env = {token_key: token}
        if url:
            env[url_key] = self.trusted.url
        if target == "plane":
            env["PLANE_WORKSPACE"] = "demo-ws"
        return env



def for_each(*targets, scripts=(REFERENCE,)):
    """Runs the test once per script and target, each as its own subTest."""
    def decorate(body):
        def test(self):
            self.assertTrue(scripts)
            for script in scripts:
                for target in targets:
                    with self.subTest(skill=script.parent.parent.name, target=target):
                        self.reset()
                        body(self, script, target)
        test.__name__ = body.__name__
        return test
    return decorate


class ServerFromMcpConfigTest(Case):
    @for_each("gitlab")
    def test_the_url_is_never_taken_from_an_mcp_config(self, script, target):
        config = {"gitlab": {"url": self.other.url + "/api/v4/mcp"}}
        for name, section in ((".mcp.json", "mcpServers"), (".vscode/mcp.json", "servers"), ("opencode.json", "mcp")):
            out = self.cli(script, target, env=self.env(target, url=False),
                           files={name: json.dumps({section: config})})
            self.assertEqual(self.other.seen, [], name)
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn("NOT CONFIGURED", out.stderr)


class ServerFromDotenvTest(Case):
    @for_each("gitlab", "plane")
    def test_dotenv_url_never_receives_a_token_from_the_environment(self, script, target):
        url_key, _, token = TOKENS[target]
        out = self.cli(script, target, env=self.env(target, url=False),
                       files={".env": f"{url_key}={self.other.url}\n"})
        self.assertEqual(self.other.seen, [])
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn(f"IGNORED {url_key}", out.stderr)
        self.assertIn("NOT CONFIGURED", out.stderr)
        self.assertNotIn(self.other.url, out.stdout + out.stderr)  # nothing for an agent to copy into --*-url
        self.assertNotIn(token, out.stdout + out.stderr)

    @for_each("gitlab", "plane")
    def test_a_token_in_the_dotenv_does_not_unlock_the_environment_token(self, script, target):
        url_key, token_key, _ = TOKENS[target]
        self.cli(script, target, env=self.env(target, url=False),
                 files={".env": f"{url_key}={self.other.url}\n{token_key}=bait\n"})
        self.assertEqual(self.other.seen, [])

    @for_each("gitlab", "plane")
    def test_an_empty_dotenv_token_unlocks_nothing(self, script, target):
        url_key, token_key, _ = TOKENS[target]
        self.cli(script, target, env=self.env(target, url=False),
                 files={".env": f"{url_key}={self.other.url}\n{token_key}=\n"})
        self.assertEqual(self.other.seen, [])

    @for_each("gitlab", "plane")
    def test_url_and_token_of_the_same_dotenv_work_in_any_order(self, script, target):
        url_key, token_key, token = TOKENS[target]
        for lines in ([f"{url_key}={self.trusted.url}", f"{token_key}={token}"],
                      [f"export {token_key}='{token}'", f"{url_key}={self.trusted.url}"]):
            self.reset()
            if target == "plane":
                lines.append("PLANE_WORKSPACE=demo-ws")
            self.cli(script, target, files={".env": "\n".join(lines)})
            self.assertTrue(self.trusted.seen)
            self.assertIn(token, self.trusted.seen[0][2])

    @for_each("gitlab", "plane")
    def test_environment_url_wins_and_may_use_a_dotenv_token(self, script, target):
        url_key, token_key, token = TOKENS[target]
        env = self.env(target)
        del env[token_key]
        self.cli(script, target, env=env, files={".env": f"{url_key}={self.other.url}\n{token_key}={token}\n"})
        self.assertEqual(self.other.seen, [])
        self.assertIn(token, self.trusted.seen[0][2])



class RedirectTest(Case):
    @for_each("gitlab", "plane")
    def test_redirect_to_another_origin_is_refused(self, script, target):
        self.trusted.redirects = {"/": (302, self.other.url + "/steal")}
        out = self.cli(script, target, env=self.env(target))
        self.assertTrue(self.trusted.seen)
        self.assertEqual(self.other.seen, [])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("redirect", out.stdout + out.stderr)
        self.assertNotIn(TOKENS[target][2], out.stdout + out.stderr)

    @for_each("any")
    def test_every_status_and_method(self, script, target):
        for method, status in (("GET", 301), ("GET", 303), ("GET", 307), ("POST", 301), ("POST", 302)):
            self.reset()
            self.trusted.redirects = {"/start": (status, self.other.url + "/steal")}
            out = self.execute(["-c", CALL_HTTP, str(script), method, self.trusted.url + "/start"])
            self.assertEqual(self.other.seen, [], (method, status))
            self.assertIn("redirect", out.stdout)

    @for_each("any")
    def test_same_host_name_trick_and_second_hop_are_refused(self, script, target):
        userinfo = self.other.url.replace("http://", "http://" + self.trusted.url[len("http://"):].split(":")[0] + "@")
        for redirects in ({"/start": (302, userinfo + "/steal")},
                          {"/start": (302, "/hop"), "/hop": (302, self.other.url + "/steal")}):
            self.reset()
            self.trusted.redirects = redirects
            self.execute(["-c", CALL_HTTP, str(script), "GET", self.trusted.url + "/start"])
            self.assertEqual(self.other.seen, [])

    @for_each("any")
    def test_same_origin_redirect_is_followed_with_the_token(self, script, target):
        self.trusted.redirects = {"/start": (302, "/moved")}
        self.execute(["-c", CALL_HTTP, str(script), "GET", self.trusted.url + "/start"])
        self.assertEqual([path for _, path, _ in self.trusted.seen], ["/start", "/moved"])
        self.assertIn("glpat-secret-123", self.trusted.seen[1][2])

    @for_each("any")
    def test_origins(self, script, target):
        code = ("import importlib.util, sys\n"
                "s = importlib.util.spec_from_file_location('skill', sys.argv[1])\n"
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "guard = sys.modules['hackathon_shared.net']\n"
                "print([guard.safe_redirect(a, b) for a, b in zip(sys.argv[2::2], sys.argv[3::2])])\n")
        pairs = [("http://h/a", "https://h/a", True),          # plain upgrade to TLS
                 ("https://h/a", "https://H:443/b", True),
                 ("https://h/a", "http://h/a", False),          # downgrade
                 ("https://h/a", "https://h.evil/a", False),
                 ("https://h/a", "https://h@evil/a", False),
                 ("https://h/a", "https://h:8443/a", False),
                 ("http://h:8080/a", "https://h/a", False),
                 ("https://h/a", "https://h:bad/a", False),
                 ("https://h/a", "ftp://h/a", False)]
        urls = [u for a, b, _ in pairs for u in (a, b)]
        out = self.execute(["-c", code, str(script), *urls])
        self.assertEqual(out.stdout.strip(), str([ok for _, _, ok in pairs]), out.stderr)


class WorkspaceTest(Case):
    @for_each("plane")
    def test_the_workspace_is_one_path_segment(self, script, target):
        for slug, quoted in (("../x", "..%2Fx"), ("a?b", "a%3Fb"), ("a#b", "a%23b")):
            self.reset()
            self.cli(script, target, env={**self.env(target), "PLANE_WORKSPACE": slug})
            self.assertTrue(self.trusted.seen[0][1].startswith(f"/api/v1/workspaces/{quoted}/"), self.trusted.seen[0][1])

    @for_each("plane")
    def test_dot_segments_are_refused_before_any_request(self, script, target):
        for slug in (".", ".."):
            self.reset()
            out = self.cli(script, target, env={**self.env(target), "PLANE_WORKSPACE": slug})
            self.assertEqual(self.trusted.seen, [])
            self.assertIn("invalid Plane workspace", out.stderr)


class WiringTest(Case):
    """Every script, not only REFERENCE: a skill that stops using a shared guard fails here."""

    @for_each("gitlab", "plane", scripts=SCRIPTS)
    def test_reads_the_dotenv_and_escapes_the_workspace(self, script, target):
        url_key, token_key, token = TOKENS[target]
        dotenv = f"{url_key}={self.trusted.url}\n{token_key}={token}\nPLANE_WORKSPACE=../x\n"
        self.cli(script, target, files={".env": dotenv})
        _, path, headers = self.trusted.seen[0]
        self.assertIn(token, headers)
        if target == "plane":
            self.assertTrue(path.startswith("/api/v1/workspaces/..%2Fx/"), path)

    @for_each("gitlab", "plane", scripts=SCRIPTS)
    def test_stops_when_only_untrusted_files_give_the_url(self, script, target):
        url_key, _, _ = TOKENS[target]
        planted = {".env": f"{url_key}={self.other.url}\n",
                   ".mcp.json": json.dumps({"mcpServers": {"gitlab": {"url": self.other.url}}})}
        out = self.cli(script, target, env=self.env(target, url=False), files=planted)
        self.assertEqual(self.other.seen, [])
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn(f"NOT CONFIGURED: missing {url_key}.", out.stderr)

    @for_each("gitlab", "plane", scripts=SCRIPTS)
    def test_honours_the_url_option_and_refuses_a_redirect(self, script, target):
        self.trusted.redirects = {"/": (302, self.other.url + "/steal")}
        self.cli(script, target, f"--{target}-url", self.trusted.url, env=self.env(target, url=False))
        self.assertTrue(self.trusted.seen)
        self.assertEqual(self.other.seen, [])


class GuardsTest(unittest.TestCase):
    """The guards live in shared/ (tests/unit/shared_modules checks the copies): every script must use them."""

    def test_every_script_imports_the_guards(self):
        for script in SCRIPTS:
            imports = [n for n in ast.parse(script.read_text()).body if isinstance(n, ast.ImportFrom)]
            names = {(n.module, a.name) for n in imports for a in n.names}
            self.assertIn(("hackathon_shared.config", "load_dotenv"), names, script.name)
            # importing net installs the redirect guard, and PlaneClient escapes the workspace
            self.assertIn("hackathon_shared.net", {n.module for n in imports}, script.name)
            self.assertIn(("hackathon_shared.plane", "PlaneClient"), names, script.name)


if __name__ == "__main__":
    unittest.main()
