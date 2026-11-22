"""CLI of assign.py: config handling, argparse defaults, the suggest flows end to end, the assign write path."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load

asg = load("assign-issues", "assign.py")

GITLAB_ENV = {"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "glpat-secret-123"}
PLANE_ENV = {"PLANE_URL": "https://plane.example.com", "PLANE_WORKSPACE": "demo-ws", "PLANE_API_KEY": "plane-secret-456"}
SECRETS = ("glpat-secret-123", "plane-secret-456")
TODAY = "2026-03-01"


class Run:
    """Runs main(argv) in an empty temp dir (no .env, no .mcp.json) with only `env` set."""

    def __init__(self, test, env=None):
        self.test, self.env = test, env or {}

    def __call__(self, *argv, files=None):
        out, err, code = io.StringIO(), io.StringIO(), 0
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (files or {}).items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        asg.main(list(argv))
                    except SystemExit as e:
                        code = e.code if isinstance(e.code, int) else 1
                        if not isinstance(e.code, int) and e.code is not None:
                            err.write(str(e.code))
            finally:
                os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()


def issue(key, areas=(), types=(), assignees=(), author=None, commenters=(), open=False, cancelled=False,
          closed="2026-02-01T00:00:00Z", **kw):
    kw.setdefault("status", "Todo" if open else "Done")
    return asg.new_issue(key=key, title=f"Title {key}", url=f"https://plane.example.com/i/{key}", areas=list(areas),
                         types=list(types), assignees=list(assignees), author=author, commenters=list(commenters),
                         open=open, cancelled=cancelled, closed=None if open else closed, updated=closed, **kw)


MEMBERS = [{"id": "u-a", "name": "alice", "display": "Alice", "level": 15},
           {"id": "u-b", "name": "bob", "display": "Bob", "level": 15},
           {"id": "u-c", "name": "carol", "display": "Carol", "level": 15},
           {"id": "u-o", "name": "olga", "display": "Olga", "level": 20}]
PERIODS = [{"name": "S1", "start": "2026-02-16", "end": "2026-03-08"},
           {"name": "S2", "start": "2026-03-09", "end": "2026-03-22"}]
# Hand-computed with --half-life 0. History plus the open work of S1/S2:
#   area backend : alice 3 + 0.5 + 1.5 = 5 | bob 1 + 0.75 = 1.75 | carol 1      -> 100 / 35 / 20
#   area frontend: alice 0.5 | bob 0.75 | carol 2                               -> 25 / 37.5 / 100
#   type feature : alice 3 + 1.5 = 4.5 | bob 1 | carol 3                        -> 100 / 22.2 / 66.7
#   type bug     : alice 1 + 3 = 4 | bob 1.5 | carol 0                          -> 100 / 37.5 / 0
ISSUES = [
    issue("DEMO-1", ["backend"], ["feature"], ["alice"], "bob", ["carol"], id="i-1"),
    issue("DEMO-2", ["backend", "frontend"], ["bug"], ["bob"], "alice", open=True, id="i-2"),
    issue("DEMO-3", ["frontend"], ["feature"], ["carol"], "carol", cancelled=True, id="i-3"),
    issue("DEMO-4", [], ["bug"], ["alice"], None, ["dave"], id="i-4"),
    issue("DEMO-10", ["backend"], ["feature"], ["alice"], open=True, period="S1", points=5, priority="high", id="i-10"),
    issue("DEMO-11", ["frontend"], ["bug"], open=True, period="S1", points=3, priority="urgent", id="i-11"),
    issue("DEMO-12", ["backend"], ["feature"], open=True, period="S1", points=2, priority="low", id="i-12"),
    issue("DEMO-13", ["backend"], [], open=True, period="S1", points=1, id="i-13", status="Blocked"),
    issue("DEMO-14", ["frontend"], [], open=True, period="S2", points=2, id="i-14"),
]


def fake_source(issues=ISSUES, members=MEMBERS, periods=PERIODS, kind="cycle"):
    writes = []

    class Fake:
        blocked = asg.Plane.blocked

        def __init__(self, *args):
            self.args, self.kind, self.project = args, kind, "Demo (DEMO)"

        def my_projects(self):
            return [{"path": "DEMO", "name": "Demo", "url": "https://plane.example.com/demo-ws/projects/p-1/issues",
                     "open_issues": None}]

        def periods(self):
            return [dict(p) for p in periods]

        def members(self):
            return [dict(m) for m in members]

        def issues(self, comments=True):
            return [dict(i, assignees=list(i["assignees"])) for i in issues]

        def normalize_key(self, key):
            return "DEMO-" + key.split("-")[-1].lstrip("#")

        def assign(self, it, member, all_members, replace):
            writes.append((it["key"], member["name"], replace))

    Fake.writes = writes
    return Fake


class ConfigTest(unittest.TestCase):
    def assert_not_configured(self, code, out, err, names):
        self.assertEqual(code, 2)
        self.assertIn("NOT CONFIGURED", err)
        for n in names:
            self.assertIn(n, err)
        self.assertIn("export ", err)
        self.assertIn("$env:", err)
        self.assertIn("setx ", err)
        self.assertIn(".env", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)

    def test_assign_without_config_writes_nothing(self):
        fake = fake_source()
        with mock.patch.object(asg, "Plane", fake):
            code, out, err = Run(self)("assign", "--target", "plane", "--project", "DEMO",
                                       "--issue", "DEMO-11", "--member", "bob", "--yes")
        self.assert_not_configured(code, out, err, ["PLANE_URL"])
        self.assertEqual(fake.writes, [])

    def test_commands_need_a_project(self):
        with mock.patch.object(asg, "Plane", fake_source()):
            code, _, err = Run(self, PLANE_ENV)("scores", "--target", "plane")
        self.assertNotEqual(code, 0)
        self.assertIn("needs --project", err)

    def test_api_error_ends_with_a_message_not_a_traceback(self):
        def boom(*args, **kw):
            raise asg.ApiError(401, '{"detail": "invalid token"}', "GET",
                               "https://plane.example.com/api/v1/workspaces/demo-ws/projects/?per_page=100")

        with mock.patch.object(asg, "http", side_effect=boom), mock.patch("time.sleep"):
            code, out, err = Run(self, PLANE_ENV)("scores", "--target", "plane", "--project", "DEMO")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("401", err)
        self.assertNotIn("Traceback", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)

    def test_gitlab_api_error_on_project_lookup(self):
        def boom(*args, **kw):
            raise asg.ApiError(404, '{"message": "404 Project Not Found"}', "GET", "https://gitlab.example.com/api/v4/x")

        with mock.patch.object(asg, "http", side_effect=boom):
            code, out, err = Run(self, GITLAB_ENV)("projects", "--target", "gitlab")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        for s in SECRETS:
            self.assertNotIn(s, out + err)


class ProjectsTableTest(unittest.TestCase):
    def test_lists_the_projects_with_a_dash_for_an_unknown_count(self):
        with mock.patch.object(asg, "Plane", fake_source()):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane")
        self.assertEqual(code, 0, err)
        self.assertIn("| 1 | [DEMO](https://plane.example.com/demo-ws/projects/p-1/issues) | Demo | — |", out)


class ArgparseTest(unittest.TestCase):
    def test_defaults(self):
        seen, parsed = {}, {}

        def stop(**kw):
            seen.update(kw)
            raise SystemExit(0)

        real = asg.argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            ns = real(self, *a, **kw)
            parsed.update(vars(ns))
            return ns

        with mock.patch.object(asg, "require_config", side_effect=stop), \
                mock.patch.object(asg.argparse.ArgumentParser, "parse_args", capture):
            Run(self, PLANE_ENV)("scores", "--target", "plane", "--project", "DEMO")
        self.assertEqual(parsed["roles"], "developer")
        self.assertEqual(parsed["half_life"], 180)
        self.assertEqual(parsed["limit"], 10)
        self.assertEqual(parsed["format"], "md")
        self.assertEqual(parsed["rate"], 60)
        self.assertFalse(parsed["dry_run"] or parsed["replace"] or parsed["all"] or parsed["no_comments"])
        self.assertFalse(parsed["yes"])
        self.assertIsNone(parsed["today"])
        self.assertEqual((parsed["plane_url"], parsed["workspace"]), ("https://plane.example.com", "demo-ws"))
        self.assertEqual(seen["scope"], "read_api (api to assign)")

    def test_invalid_choice_and_unknown_role(self):
        code, _, err = Run(self, PLANE_ENV)("scores", "--target", "jira")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        code, _, err = Run(self, PLANE_ENV)("scores", "--target", "plane", "--project", "DEMO", "--roles", "boss")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown role", err)


class Flow(unittest.TestCase):
    def run_cli(self, *argv, fake=None, target="plane"):
        fake = fake or fake_source()
        env = PLANE_ENV if target == "plane" else GITLAB_ENV
        with mock.patch.object(asg, "Plane" if target == "plane" else "GitLab", fake):
            code, out, err = Run(self, env)(argv[0], "--target", target, "--project", "DEMO", "--today", TODAY,
                                            "--half-life", "0", *argv[1:])
        for s in SECRETS:
            self.assertNotIn(s, out + err)
        return code, out, err

    def json(self, *argv, **kw):
        code, out, err = self.run_cli(*argv, "--format", "json", **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class ProjectsAndPeriodsTest(Flow):
    def test_projects_json(self):
        with mock.patch.object(asg, "Plane", fake_source()):
            code, out, err = Run(self, PLANE_ENV)("projects", "--target", "plane", "--format", "json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["path"], "DEMO")

    def test_periods_marks_the_current_one(self):
        res = self.json("periods")
        self.assertEqual(res["current"], "S1")
        self.assertEqual(res["kind"], "cycle")
        self.assertEqual([p["name"] for p in res["periods"]], ["S1", "S2"])
        code, out, _ = self.run_cli("periods")
        self.assertIn("S1 **(current)**", out)

    def test_periods_without_a_current_one(self):
        code, out, _ = self.run_cli("periods", fake=fake_source(periods=[{"name": "X", "start": None, "end": None}]))
        self.assertIn("No single cycle contains today", out)

    def test_unknown_period_is_rejected(self):
        code, _, err = self.run_cli("suggest-issue", "--member", "bob", "--period", "S9")
        self.assertNotEqual(code, 0)
        self.assertIn("'S9' is not open", err)


class ScoresTest(Flow):
    def test_scores_json(self):
        res = self.json("scores")
        self.assertEqual(res["developers"], ["alice", "bob", "carol"])  # olga is an admin
        self.assertEqual(res["issue_count"], len(ISSUES))
        area = res["scores"]["area"]
        self.assertAlmostEqual(area["alice"]["backend"], 100)
        self.assertAlmostEqual(area["bob"]["backend"], 35)
        self.assertAlmostEqual(area["carol"]["backend"], 20)
        self.assertAlmostEqual(area["carol"]["frontend"], 100)

    def test_roles_widen_the_developers(self):
        self.assertIn("olga", self.json("scores", "--roles", "member,admin")["developers"])

    def test_no_developer_exits_with_the_members(self):
        code, _, err = self.run_cli("scores", fake=fake_source(members=[MEMBERS[3]]))
        self.assertNotEqual(code, 0)
        self.assertIn("no project member has the role", err)
        self.assertIn("olga", err)

    def test_empty_project(self):
        res = self.json("scores", fake=fake_source(issues=[], periods=[]))
        self.assertEqual(res["issue_count"], 0)
        self.assertEqual(res["scores"]["areas"], [])

    def test_scores_markdown(self):
        _, out, _ = self.run_cli("scores")
        self.assertIn("# Area scores — Demo (DEMO)", out)
        self.assertIn("- **backend:** @alice (1 done, 1 open as assignee, 1 as author/commenter)", out)
        self.assertIn("- **frontend:** @carol", out)


class SuggestMemberTest(Flow):
    def test_ranking_is_75_fit_25_availability_in_the_issue_cycle(self):
        res = self.json("suggest-member", "--issue", "DEMO-12")
        self.assertEqual(res["load_scope"], "S1")
        self.assertEqual(res["load_unit"], "points")
        rank = {r["member"]: r for r in res["ranking"]}
        self.assertEqual([r["member"] for r in res["ranking"]], ["alice", "bob", "carol"])
        self.assertAlmostEqual(rank["alice"]["fit"], 100)
        self.assertAlmostEqual(rank["alice"]["availability"], 0)    # 5 of 5 points in S1
        self.assertAlmostEqual(rank["alice"]["final"], 75)
        self.assertAlmostEqual(rank["bob"]["fit"], 28 + 0.2 * 100 / 4.5)
        self.assertAlmostEqual(rank["bob"]["final"], 0.75 * (28 + 0.2 * 100 / 4.5) + 25)
        self.assertAlmostEqual(rank["carol"]["final"], 0.75 * (16 + 0.2 * 300 / 4.5) + 25)
        self.assertEqual(rank["alice"]["evidence"], {"backend": {"done": 1, "open": 1, "other": 1}})

    def test_issue_key_forms(self):
        self.assertEqual(self.json("suggest-member", "--issue", "12")["issue"]["key"], "DEMO-12")

    def test_notes_for_blocked_unlabelled_and_assigned_issues(self):
        notes = " ".join(self.json("suggest-member", "--issue", "DEMO-13")["notes"])
        self.assertIn("blocked", notes)
        notes = " ".join(self.json("suggest-member", "--issue", "DEMO-4")["notes"])
        self.assertIn("no area label", notes)
        self.assertIn("Already assigned to @alice", notes)

    def test_new_area_note(self):
        issues = ISSUES + [issue("DEMO-20", ["mobile"], ["bug"], open=True, period="S1", points=1)]
        res = self.json("suggest-member", "--issue", "DEMO-20", fake=fake_source(issues=issues))
        self.assertIn("Nobody has history in area mobile", " ".join(res["notes"]))
        self.assertEqual(res["ranking"][0]["member"], "alice")  # bug type decides

    def test_unknown_issue(self):
        code, _, err = self.run_cli("suggest-member", "--issue", "DEMO-999")
        self.assertNotEqual(code, 0)
        self.assertIn("not found", err)
        code, _, err = self.run_cli("suggest-member")
        self.assertIn("needs --issue", err)

    def test_markdown(self):
        _, out, _ = self.run_cli("suggest-member", "--issue", "DEMO-12")
        self.assertIn("# Best developer for DEMO-12 — Demo (DEMO)", out)
        self.assertIn("**Recommendation: @alice** (score 75).", out)
        self.assertIn("Load = open issues assigned in **S1** (in points).", out)


class SuggestIssueTest(Flow):
    def test_candidates_are_open_unassigned_unblocked_issues_of_the_current_cycle(self):
        res = self.json("suggest-issue", "--member", "bob")
        self.assertEqual(res["period"], "S1")
        self.assertEqual([r["key"] for r in res["ranking"]], ["DEMO-11", "DEMO-12"])
        r11, r12 = res["ranking"]
        self.assertAlmostEqual(r11["fit"], 37.5)
        self.assertAlmostEqual(r11["rank"], 57.5)              # urgent +20
        self.assertEqual((r11["best_other"], r11["best_other_fit"]), ("carol", 80))
        self.assertAlmostEqual(r12["rank"], 28 + 0.2 * 100 / 4.5)  # low +0
        self.assertEqual(r12["best_other"], "alice")
        self.assertEqual(res["load"]["issues"], 0)

    def test_named_period_and_whole_project(self):
        self.assertEqual([r["key"] for r in self.json("suggest-issue", "--member", "bob", "--period", "S2")["ranking"]],
                         ["DEMO-14"])
        keys = [r["key"] for r in self.json("suggest-issue", "--member", "bob", "--all")["ranking"]]
        self.assertEqual(sorted(keys), ["DEMO-11", "DEMO-12", "DEMO-14"])

    def test_include_assigned_but_never_own_issues(self):
        keys = [r["key"] for r in self.json("suggest-issue", "--member", "bob", "--include-assigned")["ranking"]]
        self.assertIn("DEMO-10", keys)
        keys = [r["key"] for r in self.json("suggest-issue", "--member", "alice", "--include-assigned")["ranking"]]
        self.assertNotIn("DEMO-10", keys)

    def test_member_without_the_developer_role_is_scored_with_a_note(self):
        res = self.json("suggest-issue", "--member", "Olga")
        self.assertEqual(res["member"], "olga")
        self.assertIn("does not have the developer role", " ".join(res["notes"]))

    def test_no_current_cycle_uses_the_whole_project(self):
        res = self.json("suggest-issue", "--member", "bob", fake=fake_source(periods=[]))
        self.assertIsNone(res["period"])
        self.assertIn("No single cycle contains today", " ".join(res["notes"]))

    def test_no_candidates(self):
        res = self.json("suggest-issue", "--member", "bob", fake=fake_source(issues=ISSUES[:4]))
        self.assertEqual(res["ranking"], [])
        self.assertIn("Try --include-assigned or --all", " ".join(res["notes"]))

    def test_unknown_member(self):
        code, _, err = self.run_cli("suggest-issue", "--member", "zed")
        self.assertNotEqual(code, 0)
        self.assertIn("is not a member", err)

    def test_markdown_and_limit(self):
        _, out, _ = self.run_cli("suggest-issue", "--member", "bob", "--limit", "1")
        self.assertIn("# Best issues for @bob — Demo (DEMO)", out)
        self.assertIn("**Recommendation: DEMO-11**", out)
        self.assertIn("Others fit much better: DEMO-11 (@carol)", out)
        self.assertNotIn("| 2 |", out)


class AssignTest(Flow):
    def assign(self, *argv, fake=None):
        fake = fake or fake_source()
        code, out, err = self.run_cli("assign", *argv, fake=fake)
        return code, out, err, fake.writes

    def test_without_yes_only_shows_the_plan(self):
        # Regression: `assign` wrote as soon as it was called; nothing in the script asked for
        # an explicit confirmation, so a hasty agent call changed the issue.
        code, out, err, writes = self.assign("--issue", "DEMO-11", "--member", "bob")
        self.assertEqual(code, 0, err)
        self.assertEqual(writes, [])
        self.assertIn("nothing written", out)
        self.assertIn("--yes", out)
        self.assertIn("DEMO-11 (Title DEMO-11): assignees none -> @bob", out)

    def test_dry_run_writes_nothing_even_with_yes(self):
        code, out, _, writes = self.assign("--issue", "DEMO-11", "--member", "bob", "--dry-run", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(writes, [])
        self.assertIn("DRY RUN, nothing written", out)

    def test_yes_writes_once(self):
        code, out, err, writes = self.assign("--issue", "DEMO-11", "--member", "bob", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(writes, [("DEMO-11", "bob", False)])
        self.assertIn("assigned: DEMO-11 (Title DEMO-11): assignees none -> @bob", out)

    def test_add_keeps_current_assignees_and_replace_replaces(self):
        _, out, _, writes = self.assign("--issue", "DEMO-10", "--member", "bob", "--yes")
        self.assertIn("assignees @alice -> @alice, @bob", out)
        self.assertEqual(writes, [("DEMO-10", "bob", False)])
        _, out, _, writes = self.assign("--issue", "DEMO-10", "--member", "bob", "--replace", "--yes")
        self.assertIn("assignees @alice -> @bob", out)
        self.assertEqual(writes, [("DEMO-10", "bob", True)])

    def test_rerun_is_idempotent(self):
        issues = [dict(i, assignees=["alice", "bob"]) if i["key"] == "DEMO-10" else i for i in ISSUES]
        code, out, _, writes = self.assign("--issue", "DEMO-10", "--member", "bob", "--yes",
                                           fake=fake_source(issues=issues))
        self.assertEqual(code, 0)
        self.assertEqual(writes, [])
        self.assertIn("nothing to do", out)

    def test_unknown_member_or_issue_writes_nothing(self):
        code, _, err, writes = self.assign("--issue", "DEMO-11", "--member", "zed", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("is not a member", err)
        self.assertEqual(writes, [])
        code, _, err, writes = self.assign("--issue", "DEMO-999", "--member", "bob", "--yes")
        self.assertIn("not found", err)
        self.assertEqual(writes, [])
        code, _, err, writes = self.assign("--issue", "DEMO-11", "--yes")
        self.assertIn("assign needs --member", err)
        self.assertEqual(writes, [])

    def test_ambiguous_member_writes_nothing(self):
        members = MEMBERS + [{"id": "u-al", "name": "alicia", "display": "Alicia", "level": 15}]
        code, _, err, writes = self.assign("--issue", "DEMO-11", "--member", "ali", "--yes",
                                           fake=fake_source(members=members))
        self.assertNotEqual(code, 0)
        self.assertEqual(writes, [])

    def test_gitlab_write_403_ends_with_a_message(self):
        gitlab_members = [dict(m, level=30) for m in MEMBERS]  # GitLab Developer role

        class Denied(fake_source(members=gitlab_members, kind="milestone")):
            def assign(self, *a):
                raise asg.ApiError(403, '{"message": "403 Forbidden - insufficient_scope"}', "PUT",
                                   "https://gitlab.example.com/api/v4/projects/demo/issues/11")

        code, out, err = self.run_cli("assign", "--issue", "DEMO-11", "--member", "bob", "--yes",
                                      fake=Denied, target="gitlab")
        self.assertNotEqual(code, 0)
        self.assertIn("API error", err)
        self.assertIn("403", err)
        self.assertNotIn("assigned:", out)


if __name__ == "__main__":
    unittest.main()
