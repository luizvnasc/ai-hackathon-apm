"""Plane and GitLab HTTP clients of migrate.py with urlopen patched (no network)."""
import email.message
import io
import json
import unittest
import urllib.error
from unittest import mock

from tests.unit._load import load

mig = load("gitlab-to-plane", "migrate.py")
KEY, TOKEN = "plane-secret-456", "glpat-secret-123"


class Resp:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status = body, status
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        return (self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b'{"error": "nope"}', headers=None):
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://plane.example.com/x", code, "err", hdrs, io.BytesIO(body))


def plane(dry_run=False):
    p = mig.Plane("https://plane.example.com/", KEY, "demo-ws", rate_per_min=60, dry_run=dry_run)
    p.min_interval = 0.0
    return p


class PlaneRequestTest(unittest.TestCase):
    def call(self, responses, method="GET", path="/projects/", body=None, params=None, dry_run=False):
        p = plane(dry_run)
        with mock.patch.object(mig.urllib.request, "urlopen", side_effect=responses) as urlopen, \
                mock.patch.object(mig.time, "sleep") as sleep:
            try:
                result = p._request(method, path, body=body, params=params)
            except mig.PlaneError as e:
                result = e
        return result, urlopen, sleep, p

    def test_success_sends_the_key_only_in_a_header_and_encodes_params(self):
        (status, data), urlopen, _, p = self.call([Resp({"ok": 1})], params={"per_page": 100, "q": "a b&c"})
        self.assertEqual((status, data), (200, {"ok": 1}))
        req, kwargs = urlopen.call_args[0][0], urlopen.call_args[1]
        self.assertEqual(req.full_url, "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100&q=a+b%26c")
        self.assertNotIn(KEY, req.full_url)
        self.assertEqual(req.get_header("X-api-key"), KEY)
        self.assertGreater(kwargs.get("timeout", 0), 0)
        self.assertEqual(p.calls, 1)

    def test_body_is_json_and_empty_response_is_none(self):
        (status, data), urlopen, _, _ = self.call([Resp(b"", status=204)], method="DELETE", body={"a": 1})
        self.assertIsNone(data)
        self.assertEqual(json.loads(urlopen.call_args[0][0].data), {"a": 1})

    def test_429_waits_for_retry_after_then_retries(self):
        (status, data), urlopen, sleep, _ = self.call([http_error(429, headers={"Retry-After": "3"}), Resp([1])])
        self.assertEqual(data, [1])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_any_call(3.0)

    def test_429_with_an_epoch_reset_and_a_bad_header_are_bounded(self):
        with mock.patch.object(mig.time, "time", return_value=1_700_000_000.0):
            _, _, sleep, _ = self.call([http_error(429, headers={"X-RateLimit-Reset": "1700000500"}), Resp([])])
        sleep.assert_any_call(65)  # capped
        _, _, sleep, _ = self.call([http_error(429, headers={"Retry-After": "soon"}), Resp([])])
        sleep.assert_any_call(10)

    def test_429_forever_gives_up(self):
        err, urlopen, _, _ = self.call([http_error(429) for _ in range(20)])
        self.assertIsInstance(err, mig.PlaneError)
        self.assertEqual(err.status, 429)
        self.assertEqual(urlopen.call_count, 8)

    def test_5xx_is_retried_three_times(self):
        (_, data), urlopen, _, _ = self.call([http_error(503), http_error(502), http_error(500), Resp({"ok": 1})])
        self.assertEqual(data, {"ok": 1})
        err, urlopen, _, _ = self.call([http_error(503) for _ in range(10)])
        self.assertEqual((err.status, urlopen.call_count), (503, 4))

    def test_4xx_raises_with_the_payload_and_without_the_key(self):
        err, urlopen, _, _ = self.call([http_error(409, b'{"error": "dup", "id": "w-1"}')], method="POST", body={})
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual((err.status, err.payload), (409, {"error": "dup", "id": "w-1"}))
        self.assertIn("POST /projects/ -> HTTP 409", str(err))
        self.assertNotIn(KEY, str(err))
        err, _, _, _ = self.call([http_error(401, b"<html>Unauthorized</html>")])
        self.assertIsNone(err.payload)

    def test_unreachable_plane_raises_plane_error(self):
        # Regression: a connection error escaped as a URLError traceback instead of "Plane API error".
        err, _, _, _ = self.call([urllib.error.URLError("connection refused")])
        self.assertIsInstance(err, mig.PlaneError)
        self.assertIn("connection refused", str(err))
        err, _, _, _ = self.call([TimeoutError("timed out")])
        self.assertIsInstance(err, mig.PlaneError)

    def test_workspace_is_quoted_in_the_url(self):
        # Regression: the workspace slug from the user went into the URL path unquoted.
        p = mig.Plane("https://plane.example.com", KEY, "my ws/../admin")
        self.assertEqual(p.base, "https://plane.example.com/api/v1/workspaces/my%20ws%2F..%2Fadmin")


class PlaneListWriteTest(unittest.TestCase):
    def test_follows_the_cursor_until_the_last_page(self):
        p = plane()
        pages = [{"results": [1, 2], "next_page_results": True, "next_cursor": "100:1:0"},
                 {"results": [3], "next_page_results": False, "next_cursor": "100:2:0"}]
        with mock.patch.object(p, "get", side_effect=pages) as get:
            self.assertEqual(p.list("/projects/"), [1, 2, 3])
        self.assertEqual(get.call_args_list[0][0][1], {"per_page": 100, "cursor": "100:1:0"})  # same dict, mutated
        self.assertEqual(get.call_count, 2)

    def test_plain_list_and_missing_results(self):
        p = plane()
        with mock.patch.object(p, "get", return_value=[{"id": 1}]):
            self.assertEqual(p.list("/members/"), [{"id": 1}])
        with mock.patch.object(p, "get", return_value={"next_page_results": True}):
            self.assertEqual(p.list("/x/"), [])  # no cursor: stops

    def test_dry_run_write_does_not_call_the_api(self):
        p = plane(dry_run=True)
        with mock.patch.object(p, "_request") as req:
            out = p.write("POST", "/projects/", {"name": "X"})
            other = p.write("POST", "/projects/", {"name": "Y"})
        req.assert_not_called()
        self.assertEqual(out, {"id": "<new:/projects/#1>", "name": "X"})
        # every new object gets its own fake id, so the plan keeps them apart (one cycle per milestone)
        self.assertNotEqual(out["id"], other["id"])
        self.assertTrue(other["id"].startswith("<new"))

    def test_write_returns_the_body(self):
        p = plane()
        with mock.patch.object(p, "_request", return_value=(201, {"id": "p-1"})) as req:
            self.assertEqual(p.write("POST", "/projects/", {"name": "X"}), {"id": "p-1"})
        req.assert_called_once_with("POST", "/projects/", body={"name": "X"})


def node(iid, notes=None, more_notes=False):
    widgets = [{"type": "LABELS", "labels": {"nodes": []}}]
    if notes is not None:
        widgets.append({"type": "NOTES", "notes": {"pageInfo": {"hasNextPage": more_notes, "endCursor": "n1"},
                                                   "nodes": notes}})
    return {"id": f"gid://gitlab/WorkItem/{iid}", "iid": str(iid), "title": f"T{iid}", "widgets": widgets}


def gl_page(nodes, has_next, cursor=None):
    return {"data": {"project": {"fullPath": "demo/app", "name": "App", "description": None,
                                 "webUrl": "https://gitlab.example.com/demo/app",
                                 "workItems": {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                               "nodes": nodes}}}}


class GitLabExportTest(unittest.TestCase):
    def export(self, responses, comments=True):
        with mock.patch.object(mig.urllib.request, "urlopen", side_effect=responses) as urlopen, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            result = mig.gitlab_export("https://gitlab.example.com/", TOKEN, "demo/app", comments=comments)
        return result, urlopen

    def test_pages_items_and_notes_and_sorts_by_iid(self):
        responses = [
            Resp(gl_page([node(3, notes=[{"id": "a"}], more_notes=True)], True, "c1")),
            Resp(gl_page([node(1, notes=[])], False)),
            Resp({"data": {"workItem": {"widgets": [{}, {"notes": {"pageInfo": {"hasNextPage": False},
                                                                   "nodes": [{"id": "b"}]}}]}}}),
        ]
        (info, items), urlopen = self.export(responses)
        self.assertEqual(info, {"path": "demo/app", "name": "App", "description": "",
                                "url": "https://gitlab.example.com/demo/app"})
        self.assertEqual([i["iid"] for i in items], ["1", "3"])
        notes = next(w for w in items[1]["widgets"] if w["type"] == "NOTES")["notes"]
        self.assertEqual([n["id"] for n in notes["nodes"]], ["a", "b"])
        reqs = [c[0][0] for c in urlopen.call_args_list]
        self.assertEqual({r.full_url for r in reqs}, {"https://gitlab.example.com/api/graphql"})
        self.assertEqual(json.loads(reqs[1].data)["variables"], {"p": "demo/app", "after": "c1"})
        self.assertEqual(json.loads(reqs[2].data)["variables"], {"id": "gid://gitlab/WorkItem/3", "after": "n1"})
        self.assertEqual(reqs[0].get_header("Private-token"), TOKEN)

    def test_without_comments_the_notes_widget_is_not_requested(self):
        (_, items), urlopen = self.export([Resp(gl_page([node(1)], False))], comments=False)
        self.assertNotIn("WorkItemWidgetNotes", json.loads(urlopen.call_args[0][0].data)["query"])

    def test_missing_project_exits_with_a_message(self):
        with self.assertRaises(SystemExit) as cm:
            self.export([Resp({"data": {"project": None}})])
        self.assertIn("not found", str(cm.exception.code))

    def test_http_and_graphql_errors_exit_without_the_token(self):
        for resp in (http_error(401, b'{"message": "401 Unauthorized"}'), Resp({"errors": [{"message": "bad"}]}),
                     urllib.error.URLError("no route"), TimeoutError("timed out")):
            with self.subTest(resp=type(resp).__name__), self.assertRaises(SystemExit) as cm:
                self.export([resp])
            self.assertIsInstance(cm.exception.code, str)
            self.assertNotIn(TOKEN, cm.exception.code)


class GitLabUrlDefaultTest(unittest.TestCase):
    def test_env_wins_then_mcp_json_fallback(self):
        import os
        import tempfile
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with open(".mcp.json", "w") as fh:
                    json.dump({"mcpServers": {"gitlab": {"url": "https://gl.example.com/api/v4/mcp"}}}, fh)
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(mig.gitlab_url_default(), "https://gl.example.com")
                with mock.patch.dict(os.environ, {"GITLAB_URL": "https://env.example.com"}, clear=True):
                    self.assertEqual(mig.gitlab_url_default(), "https://env.example.com")
                os.remove(".mcp.json")
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertIsNone(mig.gitlab_url_default())
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
