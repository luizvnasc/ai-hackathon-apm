"""shared/plane.py, with send() and the clock faked: no request is made and nothing sleeps."""
import unittest
from unittest import mock

from tests.unit._load import load_shared

plane = load_shared("plane")
net = load_shared("net")
KEY = "plane-secret"
BASE = "https://plane.example.com/api/v1/workspaces/demo-ws"
DEMO = {"id": "p-1", "identifier": "DEMO", "name": "Demo App"}
OTHER = {"id": "p-2", "identifier": "OPS", "name": "Operations"}


class PlaneTest(unittest.TestCase):
    def setUp(self):
        sleep = mock.patch("time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def client(self, *answers, rate=6000, workspace="demo-ws"):
        """A client whose send() gives `answers` in order (an exception is raised)."""
        made = plane.PlaneClient("https://plane.example.com/", workspace, KEY, rate)
        made.send = mock.Mock(side_effect=answers)
        return made

    def failing(self, status):
        made = self.client()
        made.send.side_effect = net.ApiError(status, "failed", "GET", "url")
        return made


class RequestTest(PlaneTest):
    def test_key_is_only_in_the_header(self):
        made = self.client(({}, {}))
        made.request("/projects/")
        self.assertEqual(made.headers, {"X-API-Key": KEY})
        self.assertNotIn(KEY, repr(made.send.call_args))

    def test_read(self):
        made = self.client(({"ok": 1}, {}))
        self.assertEqual(made.request("/x/", {"expand": "state,labels", "q": "a b&c"}), {"ok": 1})
        made.send.assert_called_once_with("GET", BASE + "/x/?expand=state%2Clabels&q=a+b%26c", None)

    def test_write(self):
        made = self.client(({}, {}))
        made.request("/x/", method="PATCH", body={"name": "y"})
        made.send.assert_called_once_with("PATCH", BASE + "/x/", {"name": "y"})

    def test_workspace_is_one_escaped_segment(self):
        made = self.client(workspace="my ws/../x?y")
        self.assertEqual(made.base, "https://plane.example.com/api/v1/workspaces/my%20ws%2F..%2Fx%3Fy")


class RetryTest(PlaneTest):
    def test_429_waits_ten_seconds_and_tries_again(self):
        made = self.client(net.ApiError(429, "slow down", "GET", "url"), ({"ok": 1}, {}))
        self.assertEqual(made.request("/x/"), {"ok": 1})
        self.sleep.assert_any_call(10)

    def test_429_stops_after_eight_attempts(self):
        made = self.failing(429)
        with self.assertRaises(net.ApiError) as caught:
            made.request("/x/", method="POST")
        self.assertEqual(made.send.call_count, 8)
        self.assertEqual(str(caught.exception), f"POST {BASE}/x/ -> HTTP 429: rate limited too many times")

    def test_server_error_is_retried_three_times(self):
        for status in (500, 502, 503):
            made = self.failing(status)
            with self.assertRaises(net.ApiError) as caught:
                made.request("/x/")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(made.send.call_count, 4)

    def test_server_error_waits_longer_each_time(self):
        self.assertEqual([plane.retry_delay(500, attempt) for attempt in range(4)], [1, 2, 4, None])

    def test_other_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 499):
            made = self.failing(status)
            with self.assertRaises(net.ApiError):
                made.request("/x/")
            self.assertEqual(made.send.call_count, 1, status)


class PacingTest(PlaneTest):
    def test_stays_two_requests_under_the_limit(self):
        self.assertAlmostEqual(self.client(rate=60).min_interval, 60 / 58)

    def test_tiny_limit_still_works(self):
        self.assertEqual(self.client(rate=1).min_interval, 60.0)

    def test_waits_between_requests(self):
        made = self.client(({}, {}), ({}, {}), rate=60)
        with mock.patch("time.monotonic", return_value=100.0):
            made.request("/a/")
            self.sleep.assert_not_called()  # the first one does not wait
            made.request("/b/")
        self.sleep.assert_called_once_with(60 / 58)

    def test_each_retry_is_paced_too(self):
        made = self.client(net.ApiError(429, "slow", "GET", "url"), ({}, {}), rate=60)
        with mock.patch("time.monotonic", return_value=100.0):
            made.request("/a/")
        self.assertEqual(self.sleep.call_args_list, [mock.call(10), mock.call(60 / 58)])


class ListTest(PlaneTest):
    def test_follows_the_cursor(self):
        first = {"results": [1, 2], "next_page_results": True, "next_cursor": "100:1:0"}
        last = {"results": [3], "next_page_results": False, "next_cursor": "100:2:0"}
        made = self.client((first, {}), (last, {}))
        self.assertEqual(made.list("/x/", {"expand": "state"}), [1, 2, 3])
        urls = [call.args[1] for call in made.send.call_args_list]
        self.assertEqual(urls[0], BASE + "/x/?per_page=100&expand=state")
        self.assertEqual(urls[1], BASE + "/x/?per_page=100&expand=state&cursor=100%3A1%3A0")

    def test_accepts_a_plain_list(self):
        self.assertEqual(self.client(([4, 5], {})).list("/x/"), [4, 5])

    def test_stops_without_a_cursor(self):
        made = self.client(({"results": [1], "next_page_results": True}, {}))
        self.assertEqual(made.list("/x/"), [1])


class ProjectTest(PlaneTest):
    def test_selects_by_identifier_name_or_id(self):
        for wanted in ("DEMO", "demo", " Demo App ", "p-1"):
            made = self.client(([OTHER, DEMO], {}))
            self.assertEqual(made.select_project(wanted), DEMO)
            self.assertEqual((made.pid, made.ident, made.name), ("p-1", "DEMO", "Demo App (DEMO)"))

    def test_no_project_only_lists(self):
        made = self.client(([DEMO], {}))
        self.assertIsNone(made.select_project(None))
        self.assertEqual(made.all_projects, [DEMO])
        self.assertFalse(hasattr(made, "pid"))

    def test_unknown_project_lists_the_others_without_the_key(self):
        made = self.client(([DEMO, OTHER], {}))
        with self.assertRaises(SystemExit) as stop:
            made.select_project("NOPE")
        expected = ("Plane project 'NOPE' not found in workspace 'demo-ws'. "
                    "Projects: Demo App (DEMO), Operations (OPS)")
        self.assertEqual(stop.exception.code, expected)

    def test_my_projects_are_active_and_joined(self):
        made = self.client()
        made.all_projects = [DEMO, {**OTHER, "is_member": False}, {**OTHER, "archived_at": "2026-01-01"}]
        url = "https://plane.example.com/demo-ws/projects/p-1/issues"
        self.assertEqual(made.my_projects(), [{"path": "DEMO", "name": "Demo App", "url": url}])


class EstimateTest(unittest.TestCase):
    def test_numeric_value(self):
        for value, expected in (("3", 3.0), (" 0.5 ", 0.5), ("0", 0.0), (0, 0.0)):
            self.assertEqual(plane.estimate({"estimate_point": {"value": value}}), expected)

    def test_unset_or_not_numeric(self):
        for value in ("XL", "", None):
            self.assertIsNone(plane.estimate({"estimate_point": {"value": value}}), value)

    def test_not_expanded(self):
        for issue in ({}, {"estimate_point": None}, {"estimate_point": "ep-3"}, {"estimate_point": "5"}):
            self.assertIsNone(plane.estimate(issue))

    def test_points_label_is_not_an_estimate(self):
        self.assertIsNone(plane.estimate({"labels": [{"name": "points::5"}]}))


class LabelNamesTest(unittest.TestCase):
    def test_label_in_a_group_gets_the_group_as_scope(self):
        labels = [{"name": "backend", "parent": "g-1"}, {"name": "stale", "parent": None}, "only-an-id"]
        self.assertEqual(plane.full_label_names(labels, {"g-1": "area"}), ["area::backend", "stale"])

    def test_no_labels(self):
        self.assertEqual(plane.full_label_names(None, {}), [])


if __name__ == "__main__":
    unittest.main()
