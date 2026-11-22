"""shared/ is the only source of the common code: the generated copies match it and each skill runs by itself."""
import ast
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS = sorted((ROOT / ".apm" / "skills").glob("*/scripts/*.py"))


def load_tool():
    spec = importlib.util.spec_from_file_location("sync_shared", ROOT / "tools" / "sync_shared.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryTest(unittest.TestCase):
    def test_generated_copies_match_the_source(self):
        self.assertEqual(load_tool().sync(ROOT, write=False), [],
                         "edit shared/*.py, then run: python3 tools/sync_shared.py")

    def test_scripts_do_not_redefine_shared_code(self):
        shared = {n.name for p in (ROOT / "shared").glob("*.py") for n in ast.parse(p.read_text()).body
                  if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertTrue(shared)
        for script in SCRIPTS:
            own = {getattr(n, "name", None) for n in ast.parse(script.read_text()).body}
            self.assertEqual(own & shared, set(), script.name)


class SyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "shared").mkdir()
        for name in ("one", "two"):
            (self.root / "shared" / f"{name}.py").write_text(f"{name.upper()} = 1\n")
        self.script = self.root / ".apm/skills/demo/scripts/demo.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("from hackathon_shared.one import ONE\n")
        self.copy = self.script.parent / "hackathon_shared"
        self.tool = load_tool()
        self.tool.sync(self.root, write=True)

    def check(self):
        return self.tool.sync(self.root, write=False)

    def test_each_skill_gets_only_what_it_imports(self):
        self.assertEqual(sorted(p.name for p in self.copy.iterdir()), ["__init__.py", "one.py"])
        self.assertEqual(self.check(), [])

    def test_an_edited_copy_is_detected_and_regenerated(self):
        path = self.copy / "one.py"
        path.write_text(path.read_text() + "EDITED = 1\n")
        self.assertEqual(len(self.check()), 1)
        self.tool.sync(self.root, write=True)
        self.assertNotIn("EDITED", path.read_text())

    def test_a_changed_source_is_detected(self):
        (self.root / "shared/one.py").write_text("ONE = 2\n")
        self.assertEqual(len(self.check()), 1)

    def test_a_missing_copy_is_detected(self):
        (self.copy / "one.py").unlink()
        self.assertEqual(len(self.check()), 1)

    def test_a_module_no_longer_imported_is_removed(self):
        self.script.write_text("from hackathon_shared.two import TWO\n")
        self.tool.sync(self.root, write=True)
        self.assertEqual(sorted(p.name for p in self.copy.iterdir()), ["__init__.py", "two.py"])

    def test_a_hand_written_file_is_reported_and_never_deleted(self):
        (self.copy / "mine.py").write_text("MINE = 1\n")
        self.assertEqual(len(self.tool.sync(self.root, write=True)), 1)
        self.assertTrue((self.copy / "mine.py").exists())

    def test_crlf_checkouts_are_not_drift(self):
        path = self.copy / "one.py"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.check(), [])

    def test_an_unknown_module_fails(self):
        self.script.write_text("from hackathon_shared.nope import X\n")
        with self.assertRaises(SystemExit):
            self.check()


class StandaloneTest(unittest.TestCase):
    """What a consumer has: one skill folder and nothing else (apm install --skill)."""

    def run_alone(self, script, *flags, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            skill = pathlib.Path(tmp, "skill")
            shutil.copytree(str(script.parent.parent), str(skill), ignore=shutil.ignore_patterns("__pycache__"))
            base = {"PATH": os.environ.get("PATH", "")}
            if os.environ.get("SYSTEMROOT"):
                base["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            out = subprocess.run([sys.executable, *flags, str(skill / "scripts" / script.name), "--help"], cwd=tmp,
                                 env={**base, **(env or {})}, capture_output=True, text=True, timeout=60)
            return out, sorted(str(p) for p in skill.rglob("*.pyc"))

    def test_runs_alone_and_in_isolated_mode_without_writing_bytecode(self):
        for script in SCRIPTS:
            for flags in ((), ("-I",)):
                with self.subTest(skill=script.stem, flags=flags):
                    out, bytecode = self.run_alone(script, *flags)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertIn("usage:", out.stdout)
                    self.assertEqual(bytecode, [])

    def test_a_package_of_the_same_name_on_pythonpath_is_not_used(self):
        with tempfile.TemporaryDirectory() as other:
            fake = pathlib.Path(other, "hackathon_shared")
            fake.mkdir()
            (fake / "__init__.py").write_text("raise SystemExit('hijacked')\n")
            for script in SCRIPTS:
                with self.subTest(skill=script.stem):
                    out, _ = self.run_alone(script, env={"PYTHONPATH": other})
                    self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
