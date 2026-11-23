"""mcp/metrics/ holds all the code: one module per tool, plus the modules they share."""
import ast
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "mcp" / "metrics"
TOOLS = sorted(path for path in PACKAGE.glob("*.py") if "\ndef main(" in path.read_text())
SHARED = sorted(path for path in PACKAGE.glob("*.py") if path not in TOOLS and path.name != "__init__.py")
# Names of a shared module that a tool module still defines in its own way, because it behaves differently.
# Remove the entry together with the variant.
VARIANTS = {"assign": {"estimate"}, "stale": {"estimate"}}
spec = importlib.util.spec_from_file_location("mcp_server", ROOT / "mcp" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def call(number, name):
    arguments = {"skill_read": server.TOOLS[name]["key"], "target": "gitlab", "project": "g/app"}
    if server.TOOLS[name]["commands"]:
        arguments["command"] = "projects"
    return {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def defined_names(path):
    """The functions, classes and constants at the top of a file."""
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name) and target.id.isupper()}
    return names


class RepositoryTest(unittest.TestCase):
    def test_eight_tools_and_their_shared_modules(self):
        self.assertEqual([path.stem for path in TOOLS], ["assign", "blocked", "commitment", "populate", "quality",
                                                         "stability", "stale", "velocity"])
        self.assertTrue(SHARED)

    def test_tools_do_not_redefine_shared_code(self):
        shared = set()
        for module in SHARED:
            shared |= defined_names(module)
        for tool in TOOLS:
            self.assertEqual(defined_names(tool) & shared, VARIANTS.get(tool.stem, set()), tool.name)

    def test_no_code_left_in_the_skills(self):
        code = [str(path) for path in (ROOT / ".apm" / "skills").rglob("*.py")]
        self.assertEqual(code, [])
        self.assertFalse((ROOT / "shared").exists())


class InstalledServerTest(unittest.TestCase):
    """What a consumer has: the package as apm installs it, started by the agent."""

    def start(self, *flags, env=None):
        """Every tool called once, without credentials: each module is loaded and stops at NOT CONFIGURED."""
        with tempfile.TemporaryDirectory() as tmp:
            mcp = pathlib.Path(tmp, "mcp")
            shutil.copytree(str(ROOT / "mcp"), str(mcp), ignore=shutil.ignore_patterns("__pycache__"))
            base = {"PATH": os.environ.get("PATH", "")}
            if os.environ.get("SYSTEMROOT"):
                base["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            lines = "".join(json.dumps(call(number, name)) + "\n" for number, name in enumerate(server.TOOLS, 1))
            out = subprocess.run([sys.executable, *flags, str(mcp / "server.py")], input=lines, cwd=tmp,
                                 env={**base, **(env or {})}, capture_output=True, text=True, timeout=60)
            replies = [json.loads(line) for line in out.stdout.splitlines()]
            return out, replies, sorted(str(p) for p in mcp.rglob("*.pyc"))

    def test_loads_every_tool_in_isolated_mode_without_writing_bytecode(self):
        for flags in ((), ("-I",)):
            with self.subTest(flags=flags):
                out, replies, bytecode = self.start(*flags)
                self.assertEqual(out.returncode, 0, out.stderr)
                texts = [reply["result"]["content"][0]["text"] for reply in replies]
                self.assertEqual(len(texts), len(server.TOOLS))
                for text in texts:
                    self.assertIn("NOT CONFIGURED", text)
                self.assertEqual(bytecode, [])

    def test_a_package_of_the_same_name_on_pythonpath_is_not_used(self):
        with tempfile.TemporaryDirectory() as other:
            fake = pathlib.Path(other, "metrics")
            fake.mkdir()
            (fake / "__init__.py").write_text("raise SystemExit('hijacked')\n")
            out, replies, _ = self.start(env={"PYTHONPATH": other})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("NOT CONFIGURED", replies[0]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
