"""Label set: the bundled labels.default.json, label file validation, scope parsing and the pure helpers."""
import json
import os
import pathlib
import re
import tempfile
import unittest

from tests.unit._load import SKILLS, load

pop = load("populate-project", "populate.py")
DEFAULT = SKILLS / "populate-project" / "labels.default.json"


class DefaultLabelsTest(unittest.TestCase):
    def setUp(self):
        with open(DEFAULT, encoding="utf-8") as fh:
            self.raw = json.load(fh)["labels"]
        self.labels = pop.load_labels(str(DEFAULT))

    def test_every_label_has_a_scoped_name_a_hex_color_and_a_description(self):
        self.assertEqual(len(self.raw), 20)
        names = [l["name"] for l in self.raw]
        self.assertEqual(len(names), len({n.lower() for n in names}), "duplicate names")
        for l in self.raw:
            self.assertRegex(l["name"], r"^[a-z]+::[a-z][a-z-]*$")
            self.assertRegex(l["color"], r"^#[0-9a-fA-F]{6}$")
            self.assertTrue(l["description"].strip(), l["name"])

    def test_scopes_and_order(self):
        by_scope = {}
        for l in self.labels:
            by_scope.setdefault(l["scope"], []).append(l["value"])
        self.assertEqual(list(by_scope), ["workflow", "priority", "type", "area"])
        self.assertEqual(by_scope["workflow"], ["backlog", "ready", "in-progress", "review", "blocked", "done"])
        self.assertEqual(by_scope["priority"], ["critical", "high", "medium", "low"])
        self.assertEqual(by_scope["type"], ["feature", "bug", "chore", "tech-debt", "docs"])
        self.assertEqual(by_scope["area"], ["frontend", "backend", "api", "database", "infra"])

    def test_every_default_workflow_value_maps_to_a_known_plane_state(self):
        states = [pop.plane_state_for(l["value"]) for l in self.labels if pop.is_workflow(l)]
        self.assertEqual(states, [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"),
                                  ("In Review", "started"), ("Blocked", "started"), ("Done", "completed")])

    def test_skill_md_table_matches_the_file(self):
        text = (SKILLS / "populate-project" / "SKILL.md").read_text(encoding="utf-8")
        for scope in ("workflow", "priority", "type", "area"):
            values = ", ".join(l["value"] for l in self.labels if l["scope"] == scope)
            self.assertIn(f"| `{scope}` | {values} |", text)


class ScopeTest(unittest.TestCase):
    def test_split_scope(self):
        self.assertEqual(pop.split_scope("workflow::done"), ("workflow", "done"))
        self.assertEqual(pop.split_scope(" area :: api "), ("area", "api"))
        self.assertEqual(pop.split_scope("team::web::frontend"), ("team::web", "frontend"))  # GitLab nested scope
        self.assertEqual(pop.split_scope("plain"), (None, "plain"))
        self.assertEqual(pop.split_scope("a:b"), (None, "a:b"))

    def test_workflow_and_priority_scopes_ignore_case_and_synonyms(self):
        def label(name):
            scope, value = pop.split_scope(name)
            return {"name": name, "scope": scope, "value": value}

        for name in ("workflow::x", "Status::x", "STATE::x", "stage::x"):
            self.assertTrue(pop.is_workflow(label(name)), name)
        for name in ("priority::x", "Prio::x"):
            self.assertTrue(pop.is_priority(label(name)), name)
        for name in ("type::x", "plain", "workflows::x"):
            self.assertFalse(pop.is_workflow(label(name)), name)
            self.assertFalse(pop.is_priority(label(name)), name)

    def test_slug(self):
        self.assertEqual(pop.slug(" In Progress "), "in-progress")
        self.assertEqual(pop.slug("in_progress"), "in-progress")
        self.assertEqual(pop.slug("Code   Review"), "code-review")


class PlaneStateMappingTest(unittest.TestCase):
    def test_synonyms(self):
        cases = {"icebox": ("Backlog", "backlog"), "To Do": ("Todo", "unstarted"), "doing": ("In Progress", "started"),
                 "WIP": ("In Progress", "started"), "QA": ("In Review", "started"),
                 "on_hold": ("Blocked", "started"), "closed": ("Done", "completed"),
                 "won't-fix": ("Cancelled", "cancelled"), "canceled": ("Cancelled", "cancelled")}
        for value, expected in cases.items():
            self.assertEqual(pop.plane_state_for(value), expected, value)

    def test_unknown_value_keeps_its_name_in_the_started_group(self):
        self.assertEqual(pop.plane_state_for("ready-for-deploy"), ("Ready For Deploy", "started"))

    def test_every_group_has_a_color(self):
        for _, group in pop.STATE_SYNONYMS.values():
            self.assertRegex(pop.GROUP_COLORS[group], r"^#[0-9a-fA-F]{6}$")

    def test_default_identifier(self):
        self.assertEqual(pop.default_identifier("Shop"), "SHOP")
        self.assertEqual(pop.default_identifier("Inventory Manager"), "IM")
        self.assertEqual(pop.default_identifier("Supercalifragilistic"), "SUPER")
        self.assertEqual(pop.default_identifier("!!!"), "PRJ")


class LoadLabelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, content):
        path = os.path.join(self.tmp.name, "labels.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def assert_rejected(self, content, *fragments):
        path = self.write(content)
        with self.assertRaises(SystemExit) as cm:
            pop.load_labels(path)
        msg = str(cm.exception.code)
        self.assertIsInstance(cm.exception.code, str, "must end with a message, not a traceback")
        for f in fragments:
            self.assertIn(f, msg)
        return msg

    def test_list_and_object_forms_strings_and_defaults(self):
        expected = [{"name": "workflow::todo", "color": "#6B7280", "description": "", "scope": "workflow",
                     "value": "todo"},
                    {"name": "urgent", "color": "#AABBCC", "description": "Now", "scope": None, "value": "urgent"}]
        rows = ["workflow::todo", {"name": " urgent ", "color": " #AABBCC ", "description": " Now "}]
        self.assertEqual(pop.load_labels(self.write(rows)), expected)
        self.assertEqual(pop.load_labels(self.write({"labels": rows})), expected)

    def test_empty_or_wrong_shape(self):
        self.assert_rejected([], "non-empty list")
        self.assert_rejected({"labels": []}, "non-empty list")
        self.assert_rejected({"items": [{"name": "a"}]}, "non-empty list")
        self.assert_rejected("42", "non-empty list")

    def test_label_without_name(self):
        self.assert_rejected([{"color": "#ffffff"}], "label #1 has no name")
        self.assert_rejected(["ok", {"name": "  "}], "label #2 has no name")

    def test_duplicate_names_ignore_case(self):
        self.assert_rejected(["type::bug", "Type::Bug"], "duplicate label 'Type::Bug'")

    def test_invalid_colors(self):
        for color in ("red", "#fff", "#12345g", "#1234567", "123456"):
            self.assert_rejected([{"name": "a", "color": color}], "invalid color", "#rrggbb")

    def test_missing_file_ends_with_a_message(self):
        # Regression: a missing --labels file raised FileNotFoundError with a traceback.
        with self.assertRaises(SystemExit) as cm:
            pop.load_labels(os.path.join(self.tmp.name, "nope.json"))
        self.assertIn("cannot read", str(cm.exception.code))

    def test_invalid_json_ends_with_a_message(self):
        # Regression: a malformed --labels file raised JSONDecodeError with a traceback.
        self.assert_rejected('{"labels": [ {"name": "a",} ]}', "not valid JSON")

    def test_wrong_types_end_with_a_message(self):
        # Regression: a number as label, or a number as name/color, raised AttributeError.
        self.assert_rejected([42], "label #1", "object")
        self.assert_rejected([{"name": 7}], "label #1", "name")
        self.assert_rejected([{"name": "a", "color": 123456}], "invalid color")
        self.assert_rejected([{"name": "a", "description": ["x"]}], "description")


if __name__ == "__main__":
    unittest.main()
