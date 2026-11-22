"""How quality.py turns GitLab and Plane answers into issues."""
import datetime as dt
import unittest
from unittest import mock

from tests.unit._load import load

dq = load("delivery-quality", "quality.py")
UTC = dt.timezone.utc
FIX = {"tofix"}


def gitlab():
    client = dq.GitLab("https://gl.example", "glpat-secret", "demo/app")
    return client


class GitLabHistoryTest(unittest.TestCase):
    def read(self, milestone_events, label_events=()):
        client = gitlab()
        issue = dq.new_issue(key="#1", period="S2", initial="S2")

        def answers(path, params=None):
            return list(milestone_events if path.endswith("milestone_events") else label_events)

        with mock.patch.object(client, "rest_pages", side_effect=answers) as pages:
            client.read_history(issue, "1", FIX)
        self.assertTrue(all("/projects/demo%2Fapp/issues/1/" in c.args[0] for c in pages.call_args_list))
        return issue

    def test_an_issue_created_in_a_milestone_starts_there(self):
        # Its first event removes it from S1: it was in S1 from the start
        issue = self.read([{"action": "remove", "created_at": "2026-03-05T10:00:00Z", "milestone": {"title": "S1"}},
                           {"action": "add", "created_at": "2026-03-05T10:00:01Z", "milestone": {"title": "S2"}}])
        self.assertEqual(issue["initial"], "S1")
        self.assertEqual([to for _, to in issue["moves"]], [None, "S2"])

    def test_an_issue_added_later_starts_nowhere(self):
        issue = self.read([{"action": "add", "created_at": "2026-03-05T10:00:00Z", "milestone": {"title": "S2"}}])
        self.assertIsNone(issue["initial"])

    def test_no_milestone_history_is_noted(self):
        self.assertTrue(self.read([])["missing_history"])

    def test_only_added_fix_labels_are_passes(self):
        labels = [{"action": "add", "created_at": "2026-03-07T10:00:00Z", "label": {"name": "workflow::to-fix"}},
                  {"action": "remove", "created_at": "2026-03-08T10:00:00Z", "label": {"name": "workflow::to-fix"}},
                  {"action": "add", "created_at": "2026-03-06T10:00:00Z", "label": {"name": "type::to-fix"}},
                  {"action": "add", "created_at": "2026-03-04T10:00:00Z", "label": None}]
        issue = self.read([], labels)
        self.assertEqual(issue["fixes"], [dt.datetime(2026, 3, 7, 10, tzinfo=UTC)])

    def test_closed_with_a_cancel_label_is_cancelled(self):
        node = {"iid": "3", "title": "t", "state": "CLOSED", "webUrl": "u", "closedAt": "2026-03-07T10:00:00Z",
                "widgets": [{"type": "LABELS", "labels": {"nodes": [{"title": "workflow::wontfix"}]}},
                            {"type": "MILESTONE", "milestone": {"title": "S1"}}]}
        issue = dq.GitLab.to_issue(node)
        self.assertEqual((issue["cancelled"], issue["period"]), (True, "S1"))


class PlaneIssueTest(unittest.TestCase):
    def plane(self):
        client = dq.Plane.__new__(dq.Plane)
        client.url, client.ws, client.pid, client.ident = "https://plane.example", "demo", "p", "DEMO"
        return client

    def issue(self, group, completed_at="2026-03-07T10:00:00Z"):
        row = {"id": "i", "sequence_id": 3, "name": "t", "state": {"group": group}, "completed_at": completed_at,
               "updated_at": "2026-03-08T10:00:00Z"}
        return self.plane().to_issue(row, "S1")

    def test_completed_is_closed_on_its_completion(self):
        issue = self.issue("completed")
        self.assertEqual((issue["closed"], issue["cancelled"]), (dt.datetime(2026, 3, 7, 10, tzinfo=UTC), False))

    def test_completed_without_a_date_uses_the_last_update(self):
        self.assertEqual(self.issue("completed", None)["closed"], dt.datetime(2026, 3, 8, 10, tzinfo=UTC))

    def test_cancelled_is_never_delivered_even_with_a_completion_date(self):
        issue = self.issue("cancelled")
        self.assertEqual((issue["closed"], issue["cancelled"]), (None, True))
        self.assertFalse(dq.was_delivered(issue, "S1"))

    def test_open_is_not_closed(self):
        self.assertIsNone(self.issue("started", None)["closed"])

    def test_history(self):
        issue = dq.new_issue(key="DEMO-3", period="S2", initial="S2")
        activities = [{"field": "cycles", "verb": "updated", "created_at": "2026-03-05T10:00:00Z", "old_value": "S1", "new_value": "S2"},
                      {"field": "state", "verb": "updated", "created_at": "2026-03-04T10:00:00Z", "new_value": "To Fix"},
                      {"field": "labels", "verb": "updated", "created_at": "2026-03-06T10:00:00Z", "new_value": "to-fix"},
                      {"field": "labels", "verb": "updated", "created_at": "2026-03-07T10:00:00Z", "old_value": "to-fix", "new_value": ""}]
        self.plane().read_history(issue, activities, FIX)
        self.assertEqual(issue["initial"], "S1")
        self.assertEqual(dq.passes_in(issue, "S1"), [dt.datetime(2026, 3, 4, 10, tzinfo=UTC)])
        self.assertEqual(dq.passes_in(issue, "S2"), [dt.datetime(2026, 3, 6, 10, tzinfo=UTC)])

    def test_removed_from_its_cycle(self):
        issue = dq.new_issue(key="DEMO-3", period=None)
        activities = [{"field": "cycles", "verb": "deleted", "created_at": "2026-03-05T10:00:00Z", "old_value": "S1", "new_value": ""}]
        self.plane().read_history(issue, activities, FIX)
        self.assertEqual(dq.period_at(issue, dt.datetime(2026, 3, 4, tzinfo=UTC)), "S1")
        self.assertIsNone(dq.period_at(issue, dt.datetime(2026, 3, 6, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()
