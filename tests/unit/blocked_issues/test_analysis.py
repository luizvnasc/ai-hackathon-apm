"""Blocked detection, stale exclusion, blocked duration, causes and next steps (no network)."""
import datetime as dt
import re
import unittest

from tests.unit._load import load

blk = load("blocked-issues", "blocked.py")

TODAY = dt.datetime(2026, 3, 1, 23, 59, tzinfo=dt.timezone.utc)
RECENT = "2026-02-25T10:00:00Z"  # 4.6 days before TODAY


def item(key, **kw):
    kw.setdefault("title", key)
    kw.setdefault("url", f"https://tracker.example.com/{key}")
    kw.setdefault("status", "open")
    kw.setdefault("updated", RECENT)
    kw.setdefault("detailed", True)
    return blk.new_item(key=key, **kw)


def link(kind, key, created=None):
    return {"type": kind, "key": key, "created": created, "resolved": True}


class FakeSource:
    """The interface analyze()/find_blocked() use, over an in-memory dict of items."""

    tz = dt.timezone.utc

    def __init__(self, *items, prefix="DEMO"):
        self.items = {i["key"]: i for i in items}
        self.prefix = prefix
        self.relations = {}      # key -> links added by load_relations (Plane --deep)
        self.loaded_relations = []

    def full(self, key):
        return self.items.get(key)

    def detail(self, it):
        it["detailed"] = True

    def load_relations(self, it):
        self.loaded_relations.append(it["key"])
        it["links"] += self.relations.get(it["key"], [])

    def mentions(self, text):
        return re.findall(rf"\b{self.prefix}-\d+\b", text or "")

    def own_ticket_prefix(self):
        return self.prefix


class HelpersTest(unittest.TestCase):
    def test_blocked_names(self):
        for name in ("workflow::blocked", "Blocked", "status::On Hold", "on-hold", "Impeded", "bloqueado",
                     "Bloqueada", " BLOCKED "):
            self.assertTrue(blk.is_blocked_name(name), name)
        for name in (None, "", "workflow::in progress", "blocker", "unblocked", "Blocked by vendor"):
            self.assertFalse(blk.is_blocked_name(name), name)

    def test_stale_names(self):
        self.assertTrue(blk.is_stale_name("stale"))
        self.assertTrue(blk.is_stale_name("workflow::Stale"))
        self.assertFalse(blk.is_stale_name("stalemate"))
        self.assertFalse(blk.is_stale_name(None))

    def test_parse_dt_and_age(self):
        self.assertIsNone(blk.parse_dt(None))
        self.assertIsNone(blk.parse_dt(""))
        self.assertEqual(blk.parse_dt("2026-02-01"), dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(blk.parse_dt("2026-02-01T10:00:00Z"), dt.datetime(2026, 2, 1, 10, tzinfo=dt.timezone.utc))
        # an offset is kept, so the difference is computed in absolute time
        local = blk.parse_dt("2026-02-01T10:00:00-03:00")
        self.assertEqual(local, dt.datetime(2026, 2, 1, 13, tzinfo=dt.timezone.utc))
        self.assertEqual(blk.parse_dt("2026-02-01T10:00:00"), dt.datetime(2026, 2, 1, 10, tzinfo=dt.timezone.utc))
        self.assertIsNone(blk.age_days(None, TODAY))
        self.assertAlmostEqual(blk.age_days(blk.parse_dt("2026-02-28T23:59:00Z"), TODAY), 1.0)
        self.assertEqual(blk.age_days(blk.parse_dt("2026-03-05"), TODAY), 0)  # future dates clamp to 0
        self.assertEqual(blk.fmt_days(None), "?")
        self.assertEqual(blk.fmt_days(3.6), "4 d")

    def test_strip_html_and_one_line(self):
        self.assertEqual(blk.strip_html("<p>A &amp; B</p><p>C<br/>D</p>"), "A & B\nC\nD")
        self.assertEqual(blk.strip_html(None), "")
        self.assertEqual(blk.one_line("a\n\n b  c"), "a b c")
        self.assertEqual(blk.one_line("x" * 10, 5), "xxxx…")


class FindBlockedTest(unittest.TestCase):
    def run_find(self, src, items, target="plane", deep=False, stale_days=30):
        return blk.find_blocked(src, items, deep, TODAY, stale_days, target)

    def test_marked_by_state_or_label(self):
        by_state = item("DEMO-1", status="Blocked")
        by_label = item("DEMO-2", labels=["backend", "workflow::on hold"])
        plain = item("DEMO-3", status="In Progress")
        closed = item("DEMO-4", status="Blocked", open=False)
        src = FakeSource(by_state, by_label, plain, closed)
        marked, link_only, stale = self.run_find(src, [by_state, by_label, plain, closed])
        self.assertEqual([i["key"] for i in marked], ["DEMO-1", "DEMO-2"])
        self.assertEqual((link_only, stale), ([], []))

    def test_stale_left_out_by_label_state_or_idle_days(self):
        label = item("DEMO-1", status="Blocked", labels=["stale"])
        state = item("DEMO-2", status="Stale", labels=["blocked"])
        idle = item("DEMO-3", status="Blocked", updated="2026-01-20T10:00:00Z")   # 41 days idle
        border = item("DEMO-4", status="Blocked", updated="2026-01-30T23:59:00Z")  # exactly 30 days: not stale
        src = FakeSource(label, state, idle, border)
        marked, _, stale = self.run_find(src, [label, state, idle, border])
        self.assertEqual([i["key"] for i in marked], ["DEMO-4"])
        self.assertEqual([(s["key"], s["why"]) for s in stale], [
            ("DEMO-1", "stale label/state"), ("DEMO-2", "stale label/state"), ("DEMO-3", "no activity for 41 days")])

    def test_stale_days_zero_turns_the_time_rule_off(self):
        idle = item("DEMO-3", status="Blocked", updated="2025-01-01T00:00:00Z")
        label = item("DEMO-1", status="Blocked", labels=["stale"])
        marked, _, stale = self.run_find(FakeSource(idle, label), [idle, label], stale_days=0)
        self.assertEqual([i["key"] for i in marked], ["DEMO-3"])
        self.assertEqual([s["key"] for s in stale], ["DEMO-1"])

    def test_stale_threshold_is_configurable(self):
        it = item("DEMO-1", status="Blocked")  # 4.6 days idle
        self.assertEqual(len(self.run_find(FakeSource(it), [it], stale_days=4)[2]), 1)
        self.assertEqual(len(self.run_find(FakeSource(it), [it], stale_days=5)[0]), 1)

    def test_gitlab_link_only_needs_an_open_blocker(self):
        blocker = item("#2")
        done = item("#3", open=False)
        a = item("#1", links=[link("blocks", "#2")])
        b = item("#4", links=[link("blocks", "#3")])
        c = item("#5", links=[link("relates_to", "#2")])

        class GitLabLike(FakeSource):
            def resolve_links(self, it):
                for l in it["links"]:
                    if l["type"] == "blocks":
                        l["type"] = "blocked_by"   # the REST link says the other issue is the source

        src = GitLabLike(blocker, done, a, b, c)
        marked, link_only, _ = self.run_find(src, [a, b, c], target="gitlab")
        self.assertEqual(marked, [])
        self.assertEqual([i["key"] for i in link_only], ["#1"])

    def test_gitlab_link_where_this_issue_is_the_blocker_is_not_blocked(self):
        a = item("#1", links=[link("blocks", "#2")])

        class GitLabLike(FakeSource):
            def resolve_links(self, it):
                pass  # direction confirmed: #1 blocks #2

        _, link_only, _ = self.run_find(GitLabLike(a, item("#2")), [a], target="gitlab")
        self.assertEqual(link_only, [])

    def test_plane_relation_only_block_needs_deep(self):
        a = item("DEMO-7", status="Todo")
        src = FakeSource(a, item("DEMO-2"))
        src.relations = {"DEMO-7": [link("blocked_by", "DEMO-2")]}
        self.assertEqual(self.run_find(src, [a]), ([], [], []))
        self.assertEqual(src.loaded_relations, [])
        _, link_only, _ = self.run_find(src, [a], deep=True)
        self.assertEqual([i["key"] for i in link_only], ["DEMO-7"])

    def test_link_only_and_stale_is_left_out(self):
        a = item("DEMO-7", status="Todo", updated="2025-12-01T00:00:00Z")
        src = FakeSource(a, item("DEMO-2"))
        src.relations = {"DEMO-7": [link("blocked_by", "DEMO-2")]}
        marked, link_only, stale = self.run_find(src, [a], deep=True)
        self.assertEqual((marked, link_only, [s["key"] for s in stale]), ([], [], ["DEMO-7"]))


class AnalyzeTest(unittest.TestCase):
    def analyze(self, it, *others, marked=True, stale_days=30):
        src = FakeSource(it, *others)
        return blk.analyze(src, it, TODAY, stale_days, marked)

    # ------------------------------------------------------------ causes
    def test_dependency_on_an_open_blocker(self):
        blocker = item("DEMO-2", status="In Progress", assignees=["dev-b"], updated="2026-02-27T23:59:00Z")
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], links=[link("blocked_by", "DEMO-2")])
        r = self.analyze(it, blocker)
        self.assertEqual(r["causes"], ["dependency"])
        self.assertEqual(r["next_steps"], [
            "Follow up on DEMO-2 (In Progress, assigned to @dev-b, last update 2 d ago)."])
        self.assertEqual(r["blocked_by"][0]["key"], "DEMO-2")
        self.assertTrue(r["blocked_by"][0]["open"])

    def test_blocker_chain_abandoned_and_unassigned(self):
        blocker = item("DEMO-2", status="Blocked", updated="2026-01-01T00:00:00Z")
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], links=[link("blocked_by", "DEMO-2")])
        r = self.analyze(it, blocker)
        self.assertEqual(r["causes"], ["dependency"])
        step = r["next_steps"][0]
        self.assertIn("assigned to nobody", step)
        self.assertIn("it is blocked too (chain)", step)
        self.assertIn("it looks abandoned", step)
        self.assertIn("it has no assignee", step)
        self.assertTrue(r["blocked_by"][0]["blocked"])

    def test_every_blocker_closed_is_dependency_resolved(self):
        done = item("DEMO-2", open=False, status="Done")
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], links=[link("blocked_by", "DEMO-2")])
        r = self.analyze(it, done)
        self.assertEqual(r["causes"], ["dependency resolved"])
        self.assertIn("move this issue back", r["next_steps"][0])

    def test_parent_blocked(self):
        parent = item("DEMO-6", status="Blocked")
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], parent="DEMO-6")
        r = self.analyze(it, parent)
        self.assertEqual(r["causes"], ["parent blocked"])
        self.assertTrue(r["parent"]["blocked"])
        self.assertIn("The parent DEMO-6 is blocked as well", r["next_steps"][0])

    def test_closed_parent_questions_the_issue(self):
        parent = item("DEMO-6", status="Done", open=False)
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], parent="DEMO-6")
        r = self.analyze(it, parent)
        self.assertIn("The parent DEMO-6 is already closed: check whether this issue is still needed.",
                      r["next_steps"])
        self.assertEqual(r["causes"], ["unknown"])

    def test_children_blocked(self):
        kid = item("DEMO-5", status="Blocked")
        ok = item("DEMO-8", status="In Progress")
        it = item("DEMO-6", status="Blocked", assignees=["dev-a"], children=["DEMO-5", "DEMO-8"])
        r = self.analyze(it, kid, ok)
        self.assertEqual(r["causes"], ["children blocked"])
        self.assertIn("Blocked children: DEMO-5.", r["next_steps"])
        self.assertEqual([c["key"] for c in r["children"]], ["DEMO-5", "DEMO-8"])

    def test_mentioned_issue_not_linked(self):
        other = item("DEMO-9", status="In Progress")
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"],
                  comments=[{"author": "dev-a", "created": "2026-02-20T10:00:00Z", "body": "Waiting for DEMO-9"}])
        r = self.analyze(it, other)
        self.assertEqual(r["causes"], ["mentioned issue (not linked)"])
        self.assertEqual([m["key"] for m in r["mentioned_not_linked"]], ["DEMO-9"])
        self.assertIn('add a "blocked by" link', r["next_steps"][0])
        self.assertEqual(r["external_refs"], [])  # own prefix is not external

    def test_linked_mention_is_not_repeated(self):
        blocker = item("DEMO-2", status="In Progress", assignees=["dev-b"])
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], links=[link("blocked_by", "DEMO-2")],
                  comments=[{"author": "dev-a", "created": "2026-02-20T10:00:00Z", "body": "blocked by DEMO-2, DEMO-1"}])
        r = self.analyze(it, blocker)
        self.assertEqual(r["mentioned_not_linked"], [])

    def test_external_from_comment_with_ticket(self):
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"],
                  comments=[{"author": "dev-a", "created": "2026-02-20T10:00:00Z",
                             "body": "Aguardando o time de infra (INFRA-312)."}])
        r = self.analyze(it)
        self.assertEqual(r["causes"], ["external"])
        self.assertEqual(r["external_refs"], ["INFRA-312"])
        self.assertIn("External cause (INFRA-312)", r["next_steps"][0])

    def test_external_from_description_only(self):
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"],
                  description="Context.\nWaiting for the vendor contract.\nMore.")
        r = self.analyze(it)
        self.assertEqual(r["causes"], ["external"])
        self.assertEqual(r["reason_in_description"], "Waiting for the vendor contract.")
        self.assertEqual(r["blocked_since_source"], "last update (approx.)")

    def test_unknown_with_and_without_owner(self):
        r = self.analyze(item("DEMO-1", status="Blocked", assignees=["dev-a"],
                              comments=[{"author": "x", "created": "2026-02-20T10:00:00Z", "body": "LGTM"}]))
        self.assertEqual(r["causes"], ["unknown"])
        self.assertIn("ask @dev-a to write the cause", r["next_steps"][0])
        self.assertEqual(len(r["next_steps"]), 1)  # no extra "nobody assigned" step for unknown
        r = self.analyze(item("DEMO-1", status="Blocked"))
        self.assertEqual(r["causes"], ["unknown"])
        self.assertEqual(r["next_steps"], ["Nothing says why it is blocked and nobody is assigned: assign an owner first."])

    def test_unassigned_and_not_marked_steps(self):
        blocker = item("DEMO-2", status="In Progress", assignees=["dev-b"])
        it = item("DEMO-7", status="Todo", links=[link("blocked_by", "DEMO-2")])
        r = self.analyze(it, blocker, marked=False)
        self.assertFalse(r["marked_blocked"])
        self.assertEqual(r["next_steps"][-2:], [
            "Nobody is assigned: assign an owner to drive the unblocking.",
            "It has an open blocker but is not marked as blocked: update its workflow label / state."])

    def test_cause_priority_order(self):
        blocker = item("DEMO-2", status="In Progress", assignees=["dev-b"])
        parent = item("DEMO-6", status="Blocked")
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], parent="DEMO-6",
                  links=[link("blocked_by", "DEMO-2")],
                  comments=[{"author": "a", "created": "2026-02-20T10:00:00Z", "body": "blocked, see VENDOR-1"}])
        r = self.analyze(it, blocker, parent)
        self.assertEqual(r["causes"], ["dependency", "parent blocked"])  # external only without open blockers

    def test_unreadable_linked_issue(self):
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], links=[link("relates_to", "DEMO-99")])
        r = self.analyze(it)
        self.assertEqual(r["related"], [{"key": "DEMO-99", "title": "(not readable)", "open": None}])

    # ------------------------------------------------------------ blocked duration
    def test_blocked_since_from_the_source(self):
        it = item("DEMO-1", status="Blocked", blocked_since="2026-02-19T23:59:00Z", blocked_since_source="state changed",
                  comments=[{"author": "a", "created": "2026-02-10T10:00:00Z", "body": "blocked"}])
        r = self.analyze(it)
        self.assertEqual((r["blocked_since"], r["blocked_since_source"]), ("2026-02-19", "state changed"))
        self.assertAlmostEqual(r["blocked_days"], 10.0)

    def test_blocked_since_falls_back_to_first_block_comment(self):
        it = item("DEMO-1", status="Blocked", comments=[
            {"author": "a", "created": "2026-02-10T10:00:00Z", "body": "hello"},
            {"author": "a", "created": "2026-02-15T23:59:00Z", "body": "We are stuck on this"},
            {"author": "a", "created": "2026-02-20T10:00:00Z", "body": "still blocked"}])
        r = self.analyze(it)
        self.assertEqual((r["blocked_since"], r["blocked_since_source"]), ("2026-02-15", "first block comment"))
        self.assertAlmostEqual(r["blocked_days"], 14.0)
        self.assertEqual(r["comment_count"], 3)
        self.assertEqual(r["last_comment"]["body"], "still blocked")

    def test_blocked_since_falls_back_to_the_earliest_link(self):
        blocker = item("DEMO-2")
        other = item("DEMO-3")
        it = item("DEMO-1", status="Blocked", links=[link("blocked_by", "DEMO-2", "2026-02-21T23:59:00Z"),
                                                    link("blocked_by", "DEMO-3", "2026-02-18T23:59:00Z"),
                                                    link("relates_to", "DEMO-2", "2026-01-01T00:00:00Z")])
        r = self.analyze(it, blocker, other)
        self.assertEqual((r["blocked_since"], r["blocked_since_source"]), ("2026-02-18", "link created"))
        self.assertAlmostEqual(r["blocked_days"], 11.0)

    def test_blocked_since_falls_back_to_last_update(self):
        r = self.analyze(item("DEMO-1", status="Blocked", updated="2026-02-22T23:59:00Z"))
        self.assertEqual((r["blocked_since"], r["blocked_since_source"]), ("2026-02-22", "last update (approx.)"))
        self.assertAlmostEqual(r["blocked_days"], 7.0)
        self.assertAlmostEqual(r["idle_days"], 7.0)

    def test_no_date_at_all(self):
        r = self.analyze(item("DEMO-1", status="Blocked", updated=None))
        self.assertIsNone(r["blocked_since"])
        self.assertIsNone(r["blocked_days"])


class RenderTest(unittest.TestCase):
    def result(self, issues, stale=()):
        return {"project": "Demo (DEMO)", "scope": "Cycle 2", "today": "2026-03-01", "timezone": "UTC", "stale_days": 30.0,
                "issues": issues, "stale_excluded": list(stale)}

    def test_empty_report(self):
        out = blk.render_md(self.result([]))
        self.assertIn("# Blocked issues — Demo (DEMO)", out)
        self.assertIn("**0 blocked issue(s)**, 0 stale left out.", out)
        self.assertNotIn("| Issue |", out)

    def test_full_report(self):
        blocker = item("DEMO-2", status="In Progress", assignees=["dev-b"])
        it = item("DEMO-1", status="Blocked", assignees=["dev-a"], priority="high", period="Cycle 2",
                  links=[link("blocked_by", "DEMO-2")],
                  comments=[{"author": "dev-a", "created": "2026-02-20T10:00:00Z", "body": "Waiting for DEMO-2"}])
        r = blk.analyze(FakeSource(it, blocker), it, TODAY, 30, True)
        out = blk.render_md(self.result([r], [{"key": "DEMO-3", "title": "Old", "url": "u3", "why": "stale label/state"}]))
        self.assertIn("Main cause: dependency 1", out)
        self.assertIn("| [DEMO-1](https://tracker.example.com/DEMO-1) | DEMO-1 | @dev-a | high |", out)
        self.assertIn("**Milestone/cycle:** Cycle 2", out)
        self.assertIn("- **Blocked by:**\n  - [DEMO-2]", out)
        self.assertIn("@dev-a: “Waiting for DEMO-2”", out)
        self.assertIn("- **Diagnosis:** dependency", out)
        self.assertIn("### Stale, left out\n- [DEMO-3](u3) Old — stale label/state", out)

    def test_last_comment_without_author(self):
        # Regression: a Plane comment whose author cannot be resolved was rendered as "@None".
        it = item("DEMO-1", status="Blocked", comments=[
            {"author": None, "created": "2026-02-20T10:00:00Z", "body": "LGTM"}])
        r = blk.analyze(FakeSource(it), it, TODAY, 30, True)
        out = blk.render_md(self.result([r]))
        self.assertIn("- **Last comment:** 2026-02-20: “LGTM”", out)
        self.assertNotIn("@None", out)

    def test_periods_and_projects_tables(self):
        p = {"project": "Demo", "kind": "cycle", "periods": [], "whole_project": {"open_issues": 3, "blocked_label": 1}}
        out = blk.render_periods_md(p)
        self.assertIn("| 1 | **whole project** | | | 3 | 1 |", out)
        self.assertIn("No open cycles.", out)
        self.assertIn("You are not a member of any project.", blk.render_projects_md("plane", []))


if __name__ == "__main__":
    unittest.main()
