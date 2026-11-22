"""GitLab: labels (project and group labels reused, never duplicated), the Kanban board and its lists."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.unit._load import SKILLS, load
from tests.unit.populate_project.fakes import GITLAB_TOKEN, FakeGitLab, args, fixture

pop = load("populate-project", "populate.py")
DEFAULT = pop.load_labels(str(SKILLS / "populate-project" / "labels.default.json"))
WORKFLOW = [l["name"] for l in DEFAULT if pop.is_workflow(l)]


def run(server, labels=DEFAULT, **kw):
    """populate_gitlab against the fake server; returns (info, plan, stdout)."""
    plan = pop.Plan(kw.get("dry_run", False))
    out = io.StringIO()
    with mock.patch.object(pop, "http", server), \
            mock.patch.dict(os.environ, {"GITLAB_TOKEN": GITLAB_TOKEN}, clear=True), \
            contextlib.redirect_stdout(out):
        info = pop.populate_gitlab(args(**kw), labels, plan)
    return info, plan, out.getvalue()


def names(items, kind):
    return [x["name"] for x in items if x["kind"] == kind]


class GitLabLabelsTest(unittest.TestCase):
    def test_empty_project_gets_every_label_and_the_board(self):
        server = FakeGitLab(pop)
        info, plan, out = run(server)
        self.assertEqual(names(plan.created, "label"), [l["name"] for l in DEFAULT])
        self.assertEqual(names(plan.created, "board"), ["Kanban"])
        self.assertEqual(names(plan.created, "board list"), WORKFLOW)
        self.assertEqual(plan.reused, [])
        posted = [c[3] for c in server.calls if c[0] == "POST" and c[1].endswith("/labels")]
        self.assertEqual(posted[0], {"name": "workflow::backlog", "color": "#dbd9d9", "description": "Not planned yet"})
        self.assertEqual(len(server.labels), 20)
        self.assertEqual(info["board_url"], f"https://gitlab.example.com/acme/shop/-/boards/{server.boards[0]['id']}")
        self.assertIn("Board URL:", out)

    def test_project_is_looked_up_url_encoded_or_by_numeric_id(self):
        server = FakeGitLab(pop)
        run(server, dry_run=True)
        self.assertEqual(server.calls[0][1], "/api/v4/projects/acme%2Fshop")
        server = FakeGitLab(pop)
        run(server, project="42", dry_run=True)
        self.assertEqual(server.calls[0][1], "/api/v4/projects/42")

    def test_token_goes_in_the_header_never_in_the_url(self):
        server = FakeGitLab(pop)
        run(server)
        for method, path, query, body, headers in server.calls:
            self.assertEqual(headers["PRIVATE-TOKEN"], GITLAB_TOKEN)
            self.assertNotIn(GITLAB_TOKEN, path + str(query) + str(body))

    def test_existing_project_and_group_labels_are_reused_not_modified(self):
        server = FakeGitLab(pop, labels=fixture("gitlab_labels.json"))
        _, plan, out = run(server)
        self.assertEqual(names(plan.reused, "label"), ["workflow::backlog", "priority::high", "type::bug"])
        self.assertNotIn("workflow::backlog", names(plan.created, "label"))
        self.assertIn("= label: priority::high (group label)", out)       # group label from an ancestor group
        self.assertIn("= label: type::bug (already exists, color kept as #ff0000)", out)  # case-insensitive match
        self.assertEqual(len(server.labels), 4 + 17)
        self.assertFalse([c for c in server.calls if c[0] in ("PUT", "DELETE")])
        # the board list for the existing project label uses its id
        board = server.boards[0]
        self.assertEqual(board["lists"][0]["label"], {"id": 101, "name": "workflow::backlog"})

    def test_labels_are_read_from_every_page_including_ancestor_groups(self):
        server = FakeGitLab(pop, labels=fixture("gitlab_labels.json"), page_size=1)
        _, plan, _ = run(server, dry_run=True)
        gets = [c for c in server.calls if c[1].endswith("/labels")]
        self.assertEqual([c[2]["page"] for c in gets], ["1", "2", "3", "4"])
        self.assertTrue(all(c[2]["include_ancestor_groups"] == "true" for c in gets))
        self.assertIn("priority::high", names(plan.reused, "label"))  # found on page 3


class GitLabBoardTest(unittest.TestCase):
    def test_existing_board_is_reused_case_insensitively_and_its_lists_kept(self):
        server = FakeGitLab(pop, labels=fixture("gitlab_labels.json"), boards=fixture("gitlab_boards.json"))
        _, plan, _ = run(server)
        self.assertEqual(names(plan.reused, "board"), ["kanban"])
        self.assertEqual(names(plan.reused, "board list"), ["workflow::backlog"])
        self.assertEqual(names(plan.created, "board list"), WORKFLOW[1:])
        self.assertFalse([c for c in server.calls if c[0] == "POST" and c[1].endswith("/boards")])
        kanban = next(b for b in server.boards if b["id"] == 12)
        self.assertEqual([x["label"]["name"] for x in kanban["lists"]], WORKFLOW)  # file order
        development = next(b for b in server.boards if b["id"] == 11)
        self.assertEqual(len(development["lists"]), 1)  # other boards are never touched

    def test_custom_board_name(self):
        server = FakeGitLab(pop, boards=fixture("gitlab_boards.json"))
        _, plan, _ = run(server, board_name="Team flow")
        self.assertEqual(names(plan.created, "board"), ["Team flow"])
        self.assertEqual(len(server.boards), 3)

    def test_board_on_a_later_page_is_found(self):
        # Regression: boards were read with one GET (GitLab returns 20 per page), so a
        # "Kanban" board on page 2 was missed and a duplicate board was created.
        boards = [{"id": i, "name": f"Board {i}", "lists": []} for i in range(1, 21)]
        boards.append({"id": 99, "name": "Kanban", "lists": []})
        server = FakeGitLab(pop, boards=boards, page_size=20)
        _, plan, _ = run(server)
        self.assertEqual(names(plan.reused, "board"), ["Kanban"])
        self.assertEqual(len(server.boards), 21)
        self.assertEqual(len(server.boards[-1]["lists"]), 6)

    def test_refused_board_stops_without_using_another_board(self):
        server = FakeGitLab(pop, boards=fixture("gitlab_boards.json"), refuse_board=True)
        with self.assertRaises(SystemExit) as cm:
            run(server, board_name="Team flow")
        msg = str(cm.exception.code)
        self.assertIn("did not allow creating the board 'Team flow'", msg)
        self.assertIn("HTTP 403", msg)
        self.assertNotIn(GITLAB_TOKEN, msg)
        self.assertFalse([c for c in server.calls if c[1].endswith("/lists")])

    def test_no_workflow_labels_means_no_board(self):
        labels = [l for l in DEFAULT if not pop.is_workflow(l)]
        server = FakeGitLab(pop)
        info, plan, _ = run(server, labels=labels)
        self.assertEqual(server.boards, [])
        self.assertIn("no workflow labels", plan.notes[0])
        self.assertNotIn("board_url", info)


class GitLabDryRunAndRerunTest(unittest.TestCase):
    def test_dry_run_makes_no_write_calls(self):
        for kw in ({}, {"labels": fixture("gitlab_labels.json"), "boards": fixture("gitlab_boards.json")}):
            server = FakeGitLab(pop, **kw)
            _, plan, out = run(server, dry_run=True)
            self.assertEqual(server.writes(), [])
            self.assertTrue(plan.created)
            self.assertIn("would create", out)

    def test_dry_run_plan_equals_the_real_run(self):
        dry = FakeGitLab(pop, labels=fixture("gitlab_labels.json"))
        real = FakeGitLab(pop, labels=fixture("gitlab_labels.json"))
        _, dry_plan, _ = run(dry, dry_run=True)
        _, real_plan, _ = run(real)
        self.assertEqual(dry_plan.created, real_plan.created)
        self.assertEqual(dry_plan.reused, real_plan.reused)

    def test_second_run_creates_nothing(self):
        server = FakeGitLab(pop, labels=fixture("gitlab_labels.json"))
        run(server)
        before = len(server.writes())
        _, plan, _ = run(server)
        self.assertEqual(plan.created, [])
        self.assertEqual(len(server.writes()), before)
        self.assertEqual(len(server.labels), 21)
        self.assertEqual(len(server.boards), 1)
        self.assertEqual(len(server.boards[0]["lists"]), 6)


class GitLabErrorsTest(unittest.TestCase):
    def test_project_not_found(self):
        with self.assertRaises(SystemExit) as cm:
            run(FakeGitLab(pop), project="acme/nope")
        self.assertIn("'acme/nope' not found", str(cm.exception.code))

    def test_token_refused(self):
        for status in (401, 403):
            with self.assertRaises(SystemExit) as cm, mock.patch.dict(os.environ, {"GITLAB_TOKEN": GITLAB_TOKEN}), \
                    mock.patch.object(pop, "http", side_effect=pop.ApiError(status, "{}", "GET", "u")):
                pop.populate_gitlab(args(), DEFAULT, pop.Plan(False))
            self.assertIn(f"refused the token (HTTP {status})", str(cm.exception.code))
            self.assertNotIn(GITLAB_TOKEN, str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
