"""GitLab and Plane histories turned into moves, estimates and cancellations (no network)."""
import unittest
from unittest import mock

from tests.unit.scope_stability.helpers import at, ss


def gl():
    g = ss.GitLab("https://gl.example.com", "glpat-secret-123", "demo/app")
    g._periods = [{"id": "19", "name": "Sprint 1"}, {"id": "20", "name": "Sprint 2"}]
    return g


def ms(action, when, ident=19, title="Sprint 1"):
    return {"action": action, "created_at": f"2026-{when}", "milestone": {"id": ident, "title": title}}


def label(action, when, name):
    return {"action": action, "created_at": f"2026-{when}", "label": {"name": name} if name else None}


class GitLabMilestoneHistoryTest(unittest.TestCase):
    T0, T1 = "03-05T10:00:00Z", "03-05T10:00:01Z"

    def test_add_then_remove(self):
        self.assertEqual(gl().moves([ms("add", self.T0), ms("remove", "03-08T10:00:00Z")]),
                         (None, [(at(self.T0), "19"), (at("03-08T10:00:00Z"), None)]))

    def test_a_replacement_is_one_move_in_either_order(self):
        removed, added = ms("remove", self.T0), ms("add", self.T1, 20, "Sprint 2")
        self.assertEqual(gl().moves([removed, added]), ("19", [(at(self.T0), "20")]))
        added, removed = ms("add", self.T0, 20, "Sprint 2"), ms("remove", self.T1)
        self.assertEqual(gl().moves([added, removed]), ("19", [(at(self.T0), "20")]))

    def test_undo_within_seconds_is_still_two_moves(self):
        initial, moves = gl().moves([ms("add", self.T0), ms("remove", self.T1)])
        self.assertEqual((initial, [to for _, to in moves]), (None, ["19", None]))

    def test_renamed_milestone_is_matched_by_id(self):
        self.assertEqual(gl().moves([ms("add", self.T0, 19, "Old title")])[1][0][1], "19")

    def test_without_id_the_title_is_used(self):
        event = {"action": "add", "created_at": "2026-" + self.T0, "milestone": {"title": "Sprint 2"}}
        self.assertEqual(gl().moves([event])[1][0][1], "20")

    def test_deleted_milestone(self):
        event = {"action": "add", "created_at": "2026-" + self.T0, "milestone": None}
        self.assertEqual(gl().moves([event])[1][0][1], "?")

    def test_unordered_events_are_sorted(self):
        initial, moves = gl().moves([ms("remove", "03-08T10:00:00Z"), ms("add", self.T0)])
        self.assertEqual((initial, [to for _, to in moves]), (None, ["19", None]))


class GitLabEstimateHistoryTest(unittest.TestCase):
    T0, T1 = "03-05T10:00:00Z", "03-05T10:00:01Z"

    def test_label_swap_is_one_change_in_either_order(self):
        for first, second in ((("remove", "points::3"), ("add", "points::8")), (("add", "points::8"), ("remove", "points::3"))):
            initial, changes = ss.GitLab.estimates([label(first[0], self.T0, first[1]), label(second[0], self.T1, second[1])])
            self.assertEqual(initial, 3)
            self.assertEqual([v for _, v in changes][-1], 8)
            self.assertNotIn(None, [v for _, v in changes])

    def test_other_labels_and_deleted_labels_are_ignored(self):
        events = [label("add", self.T0, "type::bug"), label("add", self.T0, None), label("add", self.T0, "points::XL")]
        self.assertEqual(ss.GitLab.estimates(events), (None, []))

    def test_other_point_scopes(self):
        for name in ("sp::2", "estimate::2", "Points::2"):
            self.assertEqual(ss.GitLab.estimates([label("add", self.T0, name)])[1][0][1], 2)


class GitLabIssuesTest(unittest.TestCase):
    def node(self, labels=("points::5",), state="OPEN", updated="2026-03-20T10:00:00Z"):
        return {"iid": "7", "title": "Task", "state": state, "webUrl": "https://gl/7",
                "createdAt": "2026-02-01T10:00:00Z", "updatedAt": updated,
                "closedAt": None if state == "OPEN" else "2026-03-06T10:00:00Z",
                "widgets": [{"type": "LABELS", "labels": {"nodes": [{"title": l} for l in labels]}},
                            {"type": "MILESTONE", "milestone": {"id": "gid://gitlab/Milestone/19", "title": "Sprint 1"}}]}

    def load(self, node, milestone_events=(), label_events=()):
        g = gl()
        page = {"project": {"workItems": {"pageInfo": {"hasNextPage": False}, "nodes": [node]}}}

        def rest(path):
            return list(milestone_events if path.endswith("milestone_events") else label_events)

        with mock.patch.object(g, "graphql", return_value=page), mock.patch.object(g, "rest_pages", side_effect=rest) as calls:
            return g.issues(at("03-01"))[0], calls

    def test_current_values_without_history(self):
        it, _ = self.load(self.node())
        self.assertEqual((it["period"], it["initial"], it["points"], it["missing_history"]), ("19", "19", 5, True))

    def test_untouched_issue_is_not_queried(self):
        it, calls = self.load(self.node(updated="2026-02-10T10:00:00Z"))
        calls.assert_not_called()
        self.assertEqual((it["initial"], it["missing_history"]), ("19", False))

    def test_histories_are_applied(self):
        it, _ = self.load(self.node(), [ms("add", "03-05T10:00:00Z")], [label("add", "03-05T10:00:00Z", "points::5")])
        self.assertEqual((it["initial"], it["moves"][0][1], it["estimates"][0][1], it["missing_history"]), (None, "19", 5, False))

    def test_cancelled_needs_closed_and_label(self):
        self.assertIsNone(self.load(self.node(("workflow::cancelled",)))[0]["cancelled"])
        self.assertIsNone(self.load(self.node(state="CLOSED"))[0]["cancelled"])
        self.assertEqual(self.load(self.node(("status::wontfix",), "CLOSED"))[0]["cancelled"], at("03-06T10:00:00Z"))

    def test_history_that_disagrees_is_flagged(self):
        it, _ = self.load(self.node(), [ms("add", "03-05T10:00:00Z", 20, "Sprint 2")])
        self.assertTrue(it["inconsistent"])

    def test_days_follow_the_instance_offset(self):
        g = gl()
        g.learn_times("2026-03-05T10:00:00.000-03:00")
        self.assertEqual(g.tz.utcoffset(None).total_seconds(), -3 * 3600)

    def test_milestones_are_paginated_and_keyed_by_id(self):
        def page(ident, more):
            return {"project": {"name": "Demo", "milestones": {
                "pageInfo": {"hasNextPage": more, "endCursor": "c"},
                "nodes": [{"id": f"gid://gitlab/Milestone/{ident}", "title": f"M{ident}", "state": "active",
                           "startDate": "2026-03-02", "dueDate": "2026-03-13"}]}}}

        g = gl()
        with mock.patch.object(g, "graphql", side_effect=[page(1, True), page(2, False)]):
            self.assertEqual([p["id"] for p in g.periods()], ["1", "2"])


class PlaneTest(unittest.TestCase):
    def plane(self):
        p = ss.Plane.__new__(ss.Plane)
        p.url, p.ws, p.pid, p.ident, p.tz, p.tz_note = "https://plane.example.com", "ws", "p1", "DEMO", ss.UTC, None
        p._periods = [{"id": "c1", "name": "Cycle 1"}, {"id": "c2", "name": "Cycle 2"}]
        return p

    def act(self, field, verb="updated", when="03-05T10:00:00Z", **kw):
        return {"field": field, "verb": verb, "created_at": f"2026-{when}", "old_identifier": None,
                "new_identifier": None, "old_value": "", "new_value": "", **kw}

    def read(self, activities, **kw):
        it = ss.new_issue(**{"period": "c1", "initial": "c1", "points": 5.0, "points_initial": 5.0, **kw})
        self.plane().apply_history(it, activities, {"x"})
        return it

    def test_created_updated_deleted(self):
        it = self.read([self.act("cycles", "created", new_identifier="c1"),
                        self.act("cycles", "updated", "03-06T10:00:00Z", old_identifier="c1", new_identifier="c2"),
                        self.act("cycles", "deleted", "03-07T10:00:00Z", old_identifier="c2")], period=None)
        self.assertEqual((it["initial"], [to for _, to in it["moves"]], it["inconsistent"]), (None, ["c1", "c2", None], False))

    def test_first_event_tells_the_initial_cycle(self):
        it = self.read([self.act("cycles", "deleted", old_identifier="c1")], period=None)
        self.assertEqual(it["initial"], "c1")

    def test_estimate_history(self):
        it = self.read([self.act("estimate_points", old_value="3", new_value="8")], points=8.0)
        self.assertEqual((it["points_initial"], it["estimates"][0][1]), (3, 8))

    def test_non_numeric_estimates_are_not_points(self):
        it = self.read([self.act("estimate_categories", old_value="S", new_value="XL")], points=None, points_initial=None)
        self.assertEqual((it["points_initial"], it["estimates"][0][1]), (None, None))

    def test_cancellation_time_is_the_last_change_into_a_cancelled_state(self):
        it = self.read([self.act("state", when="03-04T10:00:00Z", new_identifier="x"),
                        self.act("state", when="03-06T10:00:00Z", new_identifier="x")], cancelled=at("03-20"))
        self.assertEqual((it["cancelled"], it["approx_cancel"]), (at("03-06T10:00:00Z"), False))

    def test_cancellation_without_activity_is_flagged(self):
        self.assertTrue(self.read([], cancelled=at("03-20"))["approx_cancel"])

    def test_issues_use_expand_and_never_the_estimates_endpoint_or_labels(self):
        p = self.plane()
        row = {"id": "i1", "sequence_id": 1, "name": "Task", "created_at": "2026-02-01T00:00:00Z",
               "updated_at": "2026-02-02T00:00:00Z", "state": {"id": "s", "group": "started"},
               "labels": [{"name": "points::8"}], "estimate_point": {"value": "3"}}

        def listing(path, params=None):
            return [row] if path.endswith("/projects/p1/issues/") else [{"id": "i1"}] if "cyc" in path and "c1" in path else []

        with mock.patch.object(p, "list", side_effect=listing) as calls:
            it = p.issues(at("03-01"))[0]
        self.assertEqual((it["points"], it["period"]), (3, "c1"))
        paths = [c.args[0] for c in calls.call_args_list]
        self.assertFalse([x for x in paths if "/estimates/" in x or "/activities/" in x])
        self.assertIn(mock.call("/projects/p1/issues/", {"expand": "state,estimate_point"}), calls.call_args_list)

    def test_unset_expanded_estimate_is_no_points(self):
        for value in ({"value": ""}, {"value": "XL"}, None, "ep-id"):
            self.assertIsNone(ss.estimate({"estimate_point": value}))

    def test_cycle_dates_are_days_in_the_project_timezone(self):
        p = self.plane()
        p.tz = ss.parse_dt("2026-01-01T00:00:00-03:00").tzinfo
        cycle = {"id": "c1", "name": "C", "start_date": "2026-03-02T03:00:01Z", "end_date": "2026-03-14T02:59:00Z"}
        with mock.patch.object(p, "list", return_value=[cycle]):
            got = p.periods()[0]
        self.assertEqual((got["start"], got["end"]), ("2026-03-02", "2026-03-13"))


class ClientTest(unittest.TestCase):
    def test_request_is_paced_once_and_retried_by_fetch(self):
        p = ss.Plane.__new__(ss.Plane)
        p.base, p.headers = "https://p/api", {}
        error = ss.ApiError(502, "x", "GET", "u")
        with mock.patch.object(p, "pace") as pace, mock.patch.object(ss.time, "sleep"), \
                mock.patch.object(ss, "http", side_effect=[error, ({"ok": 1}, {})]):
            self.assertEqual(p.request("/x/"), {"ok": 1})
        pace.assert_called_once_with()

    def test_retries_are_bounded(self):
        for status, attempts in ((429, 6), (502, 4), (404, 1)):
            error = ss.ApiError(status, "x", "GET", "u")
            with mock.patch.object(ss, "http", side_effect=error) as call, mock.patch.object(ss.time, "sleep"):
                with self.assertRaises(ss.ApiError):
                    ss.fetch("GET", "u", {})
            self.assertEqual(call.call_count, attempts, status)

    def test_bad_json_and_timeouts_are_api_errors(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>"
        for patch in ({"return_value": response}, {"side_effect": TimeoutError("slow")}):
            with mock.patch.object(ss.urllib.request, "urlopen", **patch):
                with self.assertRaises(ss.ApiError) as cm:
                    ss.http("GET", "https://x", {"PRIVATE-TOKEN": "glpat-secret-123"})
            self.assertNotIn("glpat-secret-123", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
