"""Pure functions of migrate.py: Markdown to HTML, label scopes, story points, item normalization, states."""
import argparse
import unittest

from tests.unit._load import load
from tests.unit.gitlab_to_plane.fake_plane import fixture

mig = load("gitlab-to-plane", "migrate.py")
PATH = "demo/inventory-manager"


class MarkdownTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(mig.md_to_html(""), "")
        self.assertEqual(mig.md_to_html(None), "")

    def test_lists_and_task_lists(self):
        md = "Steps:\n\n1. Transfer **10** units\n2. Open the `stock` page\n\n- [x] reproduced\n- [ ] fixed"
        self.assertEqual(mig.md_to_html(md),
                         "<p>Steps:</p><ol><li>Transfer <strong>10</strong> units</li>"
                         "<li>Open the <code>stock</code> page</li></ol>"
                         "<ul><li>☑ reproduced</li><li>☐ fixed</li></ul>")

    def test_list_type_switch_closes_the_previous_list(self):
        self.assertEqual(mig.md_to_html("- a\n1) b"), "<ul><li>a</li></ul><ol><li>b</li></ol>")

    def test_headings_are_capped_at_h3(self):
        self.assertEqual(mig.md_to_html("# One\n#### Four"), "<h1>One</h1><h3>Four</h3>")

    def test_paragraph_lines_become_line_breaks(self):
        self.assertEqual(mig.md_to_html("a\r\nb\n\nc"), "<p>a<br>b</p><p>c</p>")

    def test_code_fence_is_escaped_and_not_formatted(self):
        self.assertEqual(mig.md_to_html("```py\n<b>**x**</b>\n```"),
                         "<pre><code>&lt;b&gt;**x**&lt;/b&gt;</code></pre>")

    def test_unclosed_code_fence_is_still_emitted(self):
        self.assertEqual(mig.md_to_html("text\n```\ncode"), "<p>text</p><pre><code>code</code></pre>")

    def test_blockquote(self):
        self.assertEqual(mig.md_to_html("> quoted *text*"), "<blockquote><p>quoted <em>text</em></p></blockquote>")

    def test_html_in_text_is_escaped(self):
        self.assertEqual(mig.md_to_html("<script>alert(1)</script>"), "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")

    def test_emphasis_but_not_inside_identifiers(self):
        self.assertEqual(mig.md_to_html("*it* and _it_ and snake_case_name"),
                         "<p><em>it</em> and <em>it</em> and snake_case_name</p>")

    def test_only_http_links_become_anchors(self):
        self.assertEqual(mig.md_to_html("See [the wiki](https://wiki.example.com/api)."),
                         '<p>See <a href="https://wiki.example.com/api">the wiki</a>.</p>')
        self.assertNotIn("<a ", mig.md_to_html("[x](javascript:alert(1))"))

    def test_link_url_cannot_break_out_of_the_href_attribute(self):
        # Regression: a quote in the URL closed href="..." and injected an attribute (onmouseover=...).
        out = mig.md_to_html('[x](https://e.example.com/"onmouseover="alert(1))')
        self.assertNotIn('"onmouseover', out)
        self.assertIn('href="https://e.example.com/&quot;onmouseover=&quot;alert(1"', out)


class ScopeAndPointsTest(unittest.TestCase):
    def test_split_scope(self):
        self.assertEqual(mig.split_scope("plain"), (None, "plain"))
        self.assertEqual(mig.split_scope("type::bug"), ("type", "bug"))
        self.assertEqual(mig.split_scope(" type :: bug "), ("type", "bug"))
        self.assertEqual(mig.split_scope("a::b::c"), ("a::b", "c"))

    def test_point_value(self):
        cases = {"points::3": "3", "points::3.0": "3", "points::2.5": "2.5", "sp::1": "1", "SP::8": "8",
                 "estimate::13": "13", "story-points::5": "5", "points:: 8 ": "8", "points::0": "0",
                 "points::abc": None, "points::-1": None, "type::3": None, "3": None, "priority::high": None}
        for label, want in cases.items():
            with self.subTest(label=label):
                self.assertEqual(mig.point_value(label), want)

    def test_default_identifier(self):
        self.assertEqual(mig.default_identifier({"path": "demo/inventory-manager"}), "INVM")
        self.assertEqual(mig.default_identifier({"path": "g/api"}), "API")
        self.assertEqual(mig.default_identifier({"name": "My cool project here"}), "MYCPH")
        self.assertEqual(mig.default_identifier({"path": "g/---"}), "PRJ")
        self.assertEqual(mig.default_identifier({"path": "g/a-b-c-d-e-f-g-h-i-j-k-l-m-n"}), "ABCDEFGHIJKL")


class NormItemTest(unittest.TestCase):
    def setUp(self):
        self.raw = {r["iid"]: r for r in fixture("gitlab_export.json")["work_items"]}
        self.compact = fixture("compact_export.json")

    def test_raw_widget_shape(self):
        it = mig.norm_item(self.raw["1"], PATH)
        self.assertEqual(it["iid"], "1")
        self.assertEqual(it["ref"], "demo/inventory-manager#1")
        self.assertFalse(it["closed"])
        self.assertIsNone(it["closed_at"])
        self.assertEqual(it["created"], "2026-01-05T10:00:00Z")
        self.assertEqual(it["url"], "https://gitlab.example.com/demo/inventory-manager/-/issues/1")
        self.assertEqual([l["title"] for l in it["labels"]],
                         ["workflow::in-progress", "priority::high", "points::3", "type::bug", "frontend"])
        self.assertEqual(it["labels"][-1]["color"], "#00B140")
        self.assertEqual(it["assignees"], ["alice"])
        self.assertEqual(it["milestone"], {"title": "v0.1", "start_date": "2026-01-05", "due_date": "2026-01-18"})
        self.assertEqual((it["start_date"], it["due_date"]), ("2026-01-06", "2026-01-16"))
        # the system note and the blank note are dropped
        self.assertEqual(it["notes"], [{"id": "gid://gitlab/Note/9002", "body": "Reproduced on **staging**.",
                                        "author": "bob", "created": "2026-01-06T11:30:00Z", "system": False}])

    def test_raw_closed_and_empty_item(self):
        closed = mig.norm_item(self.raw["2"], PATH)
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["closed_at"], "2026-01-20T10:00:00Z")
        empty = mig.norm_item(self.raw["4"], PATH)
        self.assertEqual((empty["labels"], empty["assignees"], empty["milestone"], empty["notes"]), ([], [], None, []))
        self.assertEqual((empty["start_date"], empty["due_date"], empty["description"]), (None, None, ""))

    def test_compact_shape(self):
        it = mig.norm_item(self.compact[0], PATH)
        self.assertEqual((it["iid"], it["ref"]), ("7", "demo/inventory-manager#7"))
        self.assertFalse(it["closed"])
        self.assertEqual(it["labels"][0], {"title": "workflow::review", "color": None})
        self.assertEqual(it["assignees"], ["alice"])
        self.assertEqual(it["milestone"], {"title": "v0.3", "start_date": "2026-02-02", "due_date": "2026-02-15"})
        self.assertEqual((it["start_date"], it["due_date"]), ("2026-02-03", "2026-02-10"))
        self.assertEqual([n["id"] for n in it["notes"]], ["501"])
        self.assertEqual(it["notes"][0]["author"], "bob")

    def test_compact_minimal_item(self):
        it = mig.norm_item(self.compact[1], PATH)
        self.assertTrue(it["closed"])
        self.assertEqual(it["labels"], [{"title": "legacy", "color": "#123456"}])
        self.assertEqual(it["milestone"], {"title": "v0.0", "start_date": None, "due_date": None})
        self.assertEqual((it["description"], it["url"], it["assignees"], it["notes"]), ("", None, [], []))

    def test_missing_title_and_untitled_milestone(self):
        it = mig.norm_item({"iid": 9, "milestone": {"title": None}, "labels": [{"color": "#fff"}]}, PATH)
        self.assertEqual(it["title"], "#9")
        self.assertIsNone(it["milestone"])
        self.assertEqual(it["labels"], [])


class TargetStateTest(unittest.TestCase):
    def state(self, labels, closed=False):
        m = mig.Migration(None, argparse.Namespace(), {}, [])
        return m.target_state({"labels": [{"title": l} for l in labels], "closed": closed})

    def test_mapping(self):
        cases = [
            ([], False, ("Backlog", "backlog")),
            (["workflow::ready"], False, ("Todo", "unstarted")),
            (["workflow::In Progress"], False, ("In Progress", "started")),
            (["status::review"], False, ("In Review", "started")),
            (["Workflow::QA"], False, ("In Review", "started")),
            (["workflow::blocked"], False, ("Blocked", "started")),
            (["workflow::design"], False, ("Design", "started")),
            (["workflow::done"], False, ("In Progress", "started")),   # open item stays open
            (["type::bug"], False, ("Backlog", "backlog")),
            ([], True, ("Done", "completed")),
            (["workflow::in-progress"], True, ("Done", "completed")),
            (["workflow::wontfix"], True, ("Cancelled", "cancelled")),
            (["workflow::duplicate"], True, ("Cancelled", "cancelled")),
        ]
        for labels, closed, want in cases:
            with self.subTest(labels=labels, closed=closed):
                self.assertEqual(self.state(labels, closed), want)


if __name__ == "__main__":
    unittest.main()
