"""Migration against an in-memory Plane (Plane._request replaced by FakePlane): plan, writes, idempotency."""
import argparse
import contextlib
import io
import unittest

from tests.unit._load import load
from tests.unit.gitlab_to_plane.fake_plane import FakePlane, fixture

mig = load("gitlab-to-plane", "migrate.py")
PATH = "demo/inventory-manager"


def make_args(**kw):
    base = dict(identifier="INVM", project_name=None, user_map="bob=robert@example.com", no_comments=False)
    return argparse.Namespace(**{**base, **kw})


def make_plane(fake, dry_run=False):
    p = mig.Plane.__new__(mig.Plane)
    p.base = "https://plane.example.com/api/v1/workspaces/demo-ws"
    p.key, p.dry_run, p.min_interval, p.last, p.calls = "plane-secret-456", dry_run, 0.0, 0.0, 0
    p._request = fake
    return p


def export_items():
    data = fixture("gitlab_export.json")
    return data["project"], [mig.norm_item(r, PATH) for r in data["work_items"]]


def item(iid, labels=(), closed=False, assignees=(), milestone=None, notes=(), url=None):
    return mig.norm_item({"iid": iid, "title": f"Item {iid}", "state": "closed" if closed else "opened",
                          "labels": list(labels), "assignees": list(assignees), "milestone": milestone,
                          "notes": list(notes), "url": url, "created": "2026-01-05T10:00:00Z"}, PATH)


def migration(fake, items=None, dry_run=False, info=None, **kw):
    if items is None:
        info, items = export_items()
    return mig.Migration(make_plane(fake, dry_run), make_args(**kw), info or {"path": PATH, "name": "Demo"}, items)


def run(fake, **kw):
    m = migration(fake, **kw)
    with contextlib.redirect_stdout(io.StringIO()):
        report = m.run()
    return m, report


def by_name(rows):
    return {r["name"]: r for r in rows}


class FullMigrationTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePlane(mig)
        self.m, self.report = run(self.fake)
        self.proj = self.fake.projects[0]
        self.pid = self.proj["id"]

    def test_project_is_created_with_cycles_enabled(self):
        self.assertEqual((self.proj["name"], self.proj["identifier"]), ("Inventory Manager", "INVM"))
        self.assertEqual(self.proj["description"],
                         "Demo inventory service\n\nMigrated from GitLab: https://gitlab.example.com/demo/inventory-manager")
        self.assertTrue(self.proj["cycle_view"])
        self.assertEqual(self.report["project"], {"id": self.pid, "identifier": "INVM", "name": "Inventory Manager"})

    def test_created_and_reused_counts(self):
        self.assertEqual(self.report["created"], {"project": 1, "label": 8, "estimate": 1, "estimate_point": 3,
                                                  "cycle": 2, "work_item": 4, "link": 4, "comment": 1})
        self.assertEqual(self.report["reused"], {"state": 4})
        self.assertEqual(self.report["unmapped_assignees"], {"carol": "no matching Plane workspace member"})
        self.assertEqual(self.report["warnings"], [])

    def test_labels_with_groups_and_name_clashes(self):
        labels = by_name(self.fake.labels[self.pid])
        # "bug" appears under two scopes and "frontend" is also a plain label: they keep the full name
        self.assertEqual(set(labels), {"frontend", "docs", "area", "kind", "type",
                                       "area::frontend", "kind::bug", "type::bug"})
        self.assertEqual(labels["type::bug"]["parent"], labels["type"]["id"])
        self.assertEqual(labels["area::frontend"]["parent"], labels["area"]["id"])
        self.assertNotIn("parent", labels["frontend"])
        self.assertEqual(labels["frontend"]["color"], "#00B140")
        self.assertEqual(labels["kind::bug"]["color"], "#AA0000")
        self.assertEqual(labels["type"]["color"], "#6B7280")
        self.assertEqual(labels["type::bug"]["external_id"], "label:type::bug")
        self.assertEqual(labels["type"]["external_id"], "label-scope:type")
        self.assertFalse(any(n.startswith(("points", "workflow", "priority", "sp")) for n in labels))

    def test_story_points_go_to_the_estimate_not_labels(self):
        est = self.fake.estimates[self.pid]
        self.assertEqual((est["name"], est["type"]), ("Story points", "points"))
        points = self.fake.points[est["id"]]
        self.assertEqual([(p["key"], p["value"]) for p in points], [(1, "1"), (2, "3"), (3, "5")])
        self.assertEqual(self.proj["estimate"], est["id"])
        pid_of = {p["value"]: p["id"] for p in points}
        items = {i["external_id"]: i for i in self.fake.items[self.pid]}
        self.assertEqual(items[f"{PATH}#1"]["estimate_point"], pid_of["3"])
        self.assertEqual(items[f"{PATH}#2"]["estimate_point"], pid_of["5"])
        self.assertEqual(items[f"{PATH}#3"]["estimate_point"], pid_of["1"])
        self.assertNotIn("estimate_point", items[f"{PATH}#4"])

    def test_work_item_bodies(self):
        states = {s["id"]: s["name"] for s in self.fake.states[self.pid]}
        labels = {l["id"]: l["name"] for l in self.fake.labels[self.pid]}
        items = {i["external_id"]: i for i in self.fake.items[self.pid]}
        one = items[f"{PATH}#1"]
        self.assertEqual(one["name"], "Stock count is wrong after a transfer")
        self.assertEqual(states[one["state"]], "In Progress")
        self.assertEqual(one["priority"], "high")
        self.assertEqual([labels[i] for i in one["labels"]], ["type::bug", "frontend"])
        self.assertEqual(one["assignees"], ["u-alice"])
        self.assertEqual(one["external_source"], "gitlab")
        self.assertEqual(one["created_at"], "2026-01-05T10:00:00Z")
        self.assertEqual((one["start_date"], one["target_date"]), ("2026-01-06", "2026-01-16"))
        self.assertIn("<ol><li>Transfer <strong>10</strong> units</li>", one["description_html"])
        two = items[f"{PATH}#2"]
        self.assertEqual((states[two["state"]], two["priority"], two["assignees"]), ("Cancelled", "urgent", ["u-bob"]))
        self.assertEqual([labels[i] for i in two["labels"]], ["area::frontend", "kind::bug"])
        three = items[f"{PATH}#3"]
        self.assertEqual((states[three["state"]], three["assignees"]), ("Done", []))  # carol is unmapped
        four = items[f"{PATH}#4"]
        self.assertEqual((states[four["state"]], four["priority"], four["labels"]), ("Backlog", "none", []))
        self.assertEqual(four["description_html"], "")
        self.assertNotIn("start_date", four)

    def test_members_are_added_to_the_project(self):
        self.assertEqual(self.fake.project_members[self.pid], {"u-alice", "u-bob"})

    def test_cycles_and_membership(self):
        cycles = by_name(self.fake.cycles[self.pid])
        self.assertEqual(list(cycles), ["v0.1", "v0.2"])
        self.assertEqual((cycles["v0.1"]["start_date"], cycles["v0.1"]["end_date"]), ("2026-01-05", "2026-01-18"))
        self.assertEqual(cycles["v0.1"]["external_id"], "milestone:v0.1")
        items = {i["external_id"]: i["id"] for i in self.fake.items[self.pid]}
        self.assertEqual(self.fake.cycle_issues[cycles["v0.1"]["id"]], {items[f"{PATH}#1"]})
        self.assertEqual(self.fake.cycle_issues[cycles["v0.2"]["id"]], {items[f"{PATH}#2"]})

    def test_links_and_comments(self):
        items = {i["external_id"]: i["id"] for i in self.fake.items[self.pid]}
        links = self.fake.links[items[f"{PATH}#1"]]
        self.assertEqual(links, [{"id": links[0]["id"], "url": "https://gitlab.example.com/demo/inventory-manager/-/issues/1",
                                  "title": "GitLab demo/inventory-manager#1"}])
        comments = self.fake.comments[items[f"{PATH}#1"]]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["external_id"], "gid://gitlab/Note/9002")
        self.assertEqual(comments[0]["comment_html"],
                         "<p><em>@bob on GitLab, 2026-01-06 11:30</em></p><p>Reproduced on <strong>staging</strong>.</p>")

    def test_rerun_creates_nothing_new(self):
        before = self.fake.counts()
        start = len(self.fake.calls)
        _, report = run(self.fake)
        self.assertEqual(self.fake.counts(), before)
        self.assertEqual(report["created"], {})
        self.assertEqual(report["reused"], {"project": 1, "state": 4, "label": 8, "estimate": 1, "cycle": 2,
                                            "work_item": 4, "link": 4, "comment": 1})
        writes = [(m, p) for m, p, _ in self.fake.calls[start:] if m != "GET"]
        kinds = sorted({(m, p.rstrip("/").split("/")[-1 if m == "POST" else -2]) for m, p in writes})
        # work items are upserted (POST -> 409 -> PATCH) and cycle membership is re-sent; nothing else is written
        self.assertEqual(kinds, [("PATCH", "work-items"), ("POST", "cycle-issues"), ("POST", "work-items")])
        patches = [b for m, p, b in self.fake.calls[start:] if m == "PATCH"]
        self.assertEqual(len(patches), 4)
        self.assertTrue(all("created_at" not in b for b in patches))


class DryRunTest(unittest.TestCase):
    def test_new_project_makes_no_write_calls(self):
        fake = FakePlane(mig)
        m, report = run(fake, dry_run=True)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(fake.counts()["projects"], 0)
        self.assertEqual(report["created"], {"project": 1, "label": 8, "estimate": 1, "estimate_point": 3,
                                             "cycle": 2, "work_item": 4, "link": 4, "comment": 1})
        self.assertEqual(report["unmapped_assignees"], {"carol": "no matching Plane workspace member"})

    def test_existing_project_with_missing_objects_makes_no_write_calls(self):
        fake = FakePlane(mig)
        fake.add_project()  # no labels, no estimate, no members, cycles disabled
        _, report = run(fake, dry_run=True)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(report["reused"], {"project": 1, "state": 4})
        self.assertEqual(report["created"]["work_item"], 4)

    def test_after_a_real_run_makes_no_write_calls(self):
        fake = FakePlane(mig)
        run(fake)
        start = len(fake.calls)
        run(fake, dry_run=True)
        self.assertEqual([c for c in fake.calls[start:] if c[0] != "GET"], [])

    def test_dry_run_counts_every_comment_like_a_real_run(self):
        # Regression: the dry run counted one comment per work item instead of one per note.
        notes = [{"id": n, "body": f"note {n}", "author": "bob", "created": "2026-01-06T11:30:00Z"} for n in (1, 2)]
        items = [item(1, notes=notes, url="https://gitlab.example.com/x/-/issues/1")]
        _, dry = run(FakePlane(mig), items=items, dry_run=True)
        _, real = run(FakePlane(mig), items=items)
        self.assertEqual(dry["created"]["comment"], 2)
        self.assertEqual(dry["created"], real["created"])


class EstimateTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePlane(mig)
        self.proj = self.fake.add_project()
        self.pid = self.proj["id"]

    def estimates(self, items, dry_run=False):
        m = migration(self.fake, items=items, dry_run=dry_run)
        with contextlib.redirect_stdout(io.StringIO()):
            return m, m.estimates(self.proj)

    def add_estimate(self, type_="points", values=()):
        est = {"id": "est-x", "name": "Story points", "type": type_}
        self.fake.estimates[self.pid] = est
        self.fake.points["est-x"] = [{"id": f"pt-{v}", "key": n, "value": v} for n, v in enumerate(values, 1)]
        return est

    def test_no_points_means_no_estimate_calls(self):
        _, ids = self.estimates([item(1, labels=["type::bug"])])
        self.assertEqual(ids, {})
        self.assertFalse(any("estimate" in p for _, p, _ in self.fake.calls))

    def test_creates_and_enables_the_estimate(self):
        _, ids = self.estimates([item(1, labels=["points::5"]), item(2, labels=["sp::2"])])
        est = self.fake.estimates[self.pid]
        self.assertEqual(set(ids), {"2", "5"})
        self.assertEqual(self.proj["estimate"], est["id"])
        self.assertIn(("PATCH", f"/projects/{self.pid}/"), self.fake.writes())

    def test_reuses_the_estimate_and_adds_only_missing_points_in_one_list(self):
        self.add_estimate(values=["1", "3.0"])
        self.proj["estimate"] = "est-x"
        m, ids = self.estimates([item(1, labels=["points::3"]), item(2, labels=["points::5"]),
                                 item(3, labels=["points::8"])])
        posts = [(p, b) for meth, p, b in self.fake.calls if meth == "POST"]
        self.assertEqual(posts, [(f"/projects/{self.pid}/estimates/est-x/estimate-points/",
                                  [{"key": 3, "value": "5"}, {"key": 4, "value": "8"}])])
        self.assertEqual(ids["3"], "pt-3.0")   # "3.0" in Plane matches points::3
        self.assertEqual(ids["1"], "pt-1")
        self.assertEqual(m.report["reused"], {"estimate": 1})
        self.assertEqual(m.report["created"], {"estimate_point": 2})
        self.assertNotIn("PATCH", [meth for meth, _, _ in self.fake.calls])  # already enabled

    def test_non_points_estimate_gets_a_warning(self):
        self.add_estimate(type_="categories", values=["S", "M"])
        m, ids = self.estimates([item(1, labels=["points::2"])])
        self.assertEqual(len(m.report["warnings"]), 1)
        self.assertIn("'categories'", m.report["warnings"][0])
        self.assertIn("2", ids)

    def test_other_estimate_errors_are_raised(self):
        self.fake.fail("GET", r"/estimates/$", 500)
        with self.assertRaises(mig.PlaneError):
            self.estimates([item(1, labels=["points::2"])])

    def test_dry_run_with_missing_points_writes_nothing(self):
        self.add_estimate(values=["1"])
        _, ids = self.estimates([item(1, labels=["points::1"]), item(2, labels=["points::2"])], dry_run=True)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(ids, {"1": "pt-1", "2": "<new:point:2>"})


class LabelTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePlane(mig)
        self.pid = self.fake.add_project()["id"]

    def labels(self, items):
        m = migration(self.fake, items=items)
        with contextlib.redirect_stdout(io.StringIO()):
            return m, m.labels(self.pid)

    def test_existing_label_is_reused_case_insensitively(self):
        self.fake.labels[self.pid].append({"id": "lab-old", "name": "Frontend"})
        m, ids = self.labels([item(1, labels=["frontend"])])
        self.assertEqual(ids, {"frontend": "lab-old"})
        self.assertEqual(m.report["reused"], {"label": 1})
        self.assertEqual(self.fake.writes(), [])

    def test_external_id_is_dropped_when_plane_rejects_it(self):
        self.fake.fail("POST", r"/labels/$", 400)
        self.labels([item(1, labels=["docs"])])
        bodies = [b for meth, _, b in self.fake.calls if meth == "POST"]
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]["external_id"], "label:docs")
        self.assertNotIn("external_id", bodies[1])

    def test_other_label_errors_are_raised(self):
        self.fake.fail("POST", r"/labels/$", 403)
        with self.assertRaises(mig.PlaneError):
            self.labels([item(1, labels=["docs"])])


class MemberTest(unittest.TestCase):
    def members(self, usernames, user_map=None, members=None, fail=None):
        fake = FakePlane(mig, members=members)
        pid = fake.add_project()["id"]
        if fail:
            fake.fail("POST", r"/project-members/$", fail)
        m = migration(fake, items=[item(1, assignees=usernames)], user_map=user_map)
        with contextlib.redirect_stdout(io.StringIO()):
            return m, m.members(pid), fake

    def test_matching_by_user_map_display_name_and_email_local_part(self):
        members = [{"id": "u-1", "email": "robert@example.com", "display_name": "rob"},
                   {"id": "u-2", "email": "ana@example.com", "display_name": "ana"},
                   {"id": "u-3", "email": "carol@example.com", "display_name": "cee"}]
        m, mapped, fake = self.members(["bob", "Ana", "carol", "zed"], user_map=" @Bob = Robert@Example.com ,junk",
                                       members=members)
        self.assertEqual(mapped, {"bob": "u-1", "Ana": "u-2", "carol": "u-3"})
        self.assertEqual(m.report["unmapped_assignees"], {"zed": "no matching Plane workspace member"})

    def test_no_assignees_reads_nothing(self):
        fake = FakePlane(mig)
        m = migration(fake, items=[item(1)])
        self.assertEqual(m.members("proj-x"), {})
        self.assertEqual(fake.calls, [])

    def test_failing_to_add_a_member_leaves_the_item_unassigned_with_a_warning(self):
        m, mapped, _ = self.members(["alice"], fail=403)
        self.assertEqual(mapped, {})
        self.assertEqual(len(m.report["warnings"]), 1)
        self.assertIn("alice@example.com", m.report["warnings"][0])
        self.assertNotIn("plane-secret-456", m.report["warnings"][0])


class CycleTest(unittest.TestCase):
    def test_existing_cycle_is_matched_by_name_and_undated_milestone_has_no_dates(self):
        fake = FakePlane(mig)
        pid = fake.add_project()["id"]
        fake.cycles[pid].append({"id": "cyc-old", "name": "v0.1"})
        items = [item(1, milestone={"title": "v0.1", "start_date": "2026-01-05", "due_date": "2026-01-18"}),
                 item(2, milestone={"title": "later"}), item(3)]
        m = migration(fake, items=items)
        with contextlib.redirect_stdout(io.StringIO()):
            ids = m.cycles(pid)
        self.assertEqual(ids["v0.1"], "cyc-old")
        new = by_name(fake.cycles[pid])["later"]
        self.assertNotIn("start_date", new)
        self.assertEqual(m.report["created"], {"cycle": 1})
        self.assertEqual(m.report["reused"], {"cycle": 1})

    def test_no_milestones_means_no_cycle_calls(self):
        fake = FakePlane(mig)
        m = migration(fake, items=[item(1)])
        self.assertEqual(m.cycles("proj-x"), {})
        self.assertEqual(fake.calls, [])


class WorkItemTest(unittest.TestCase):
    def test_priorities(self):
        cases = {"critical": "urgent", "blocker": "urgent", "P0": "urgent", "high": "high", "major": "high",
                 "medium": "medium", "normal": "medium", "low": "low", "trivial": "low", "whatever": "none"}
        items = [item(n, labels=[f"priority::{v}"]) for n, v in enumerate(cases, 1)]
        fake = FakePlane(mig)
        run(fake, items=items)
        got = {i["external_id"]: i["priority"] for i in fake.items[fake.projects[0]["id"]]}
        for n, want in enumerate(cases.values(), 1):
            self.assertEqual(got[f"{PATH}#{n}"], want)

    def test_conflict_without_an_id_is_raised(self):
        fake = FakePlane(mig)
        fake.fail("POST", r"/work-items/$", 409, {"error": "conflict"})
        with self.assertRaises(mig.PlaneError):
            run(fake, items=[item(1)])

    def test_server_error_is_raised(self):
        fake = FakePlane(mig)
        fake.fail("POST", r"/work-items/$", 500)
        with self.assertRaises(mig.PlaneError) as cm:
            run(fake, items=[item(1)])
        self.assertEqual(cm.exception.status, 500)

    def test_no_url_means_no_link_and_no_comments_flag_skips_comments(self):
        fake = FakePlane(mig)
        notes = [{"id": 1, "body": "hi", "author": "bob", "created": "2026-01-06T11:30:00Z"}]
        _, report = run(fake, items=[item(1, notes=notes)], no_comments=True)
        self.assertFalse(any(p.endswith(("/links/", "/comments/")) for _, p, _ in fake.calls))
        self.assertNotIn("link", report["created"])
        self.assertNotIn("comment", report["created"])

    def test_existing_comment_is_matched_by_external_id(self):
        fake = FakePlane(mig)
        notes = [{"id": n, "body": f"note {n}", "author": "bob", "created": "2026-01-06T11:30:00Z"} for n in (1, 2)]
        run(fake, items=[item(1, notes=notes[:1])])
        _, report = run(fake, items=[item(1, notes=notes)])
        wid = fake.items[fake.projects[0]["id"]][0]["id"]
        self.assertEqual([c["external_id"] for c in fake.comments[wid]], ["1", "2"])
        self.assertEqual((report["created"].get("comment"), report["reused"].get("comment")), (1, 1))

    def test_comment_author_is_escaped(self):
        fake = FakePlane(mig)
        notes = [{"id": 1, "body": "x", "author": "<b>eve</b>", "created": None}]
        run(fake, items=[item(1, notes=notes)])
        wid = fake.items[fake.projects[0]["id"]][0]["id"]
        self.assertTrue(fake.comments[wid][0]["comment_html"].startswith("<p><em>@&lt;b&gt;eve&lt;/b&gt; on GitLab, </em></p>"))


if __name__ == "__main__":
    unittest.main()
