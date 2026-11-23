import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".apm" / "skills"
MCP = ROOT / "mcp"
PACKAGE = MCP / "hackathon_metrics"
if str(MCP) not in sys.path:
    sys.path.insert(0, str(MCP))


def load(skill, script):
    """The module of a skill's tool, mcp/hackathon_metrics/<script> (the skill name only says which one)."""
    return importlib.import_module(f"hackathon_metrics.{pathlib.Path(script).stem}")


def load_shared(module):
    """A module shared by the tools, mcp/hackathon_metrics/<module>.py."""
    return importlib.import_module(f"hackathon_metrics.{module}")


def tool_command(module_path):
    """The command line that runs a tool module in its own process, as mcp/server.py runs it: its main(argv)."""
    code = (f"import importlib, sys\nsys.path.insert(0, {str(MCP)!r})\n"
            "importlib.import_module('hackathon_metrics.' + sys.argv[1]).main(sys.argv[2:])\n")
    return [sys.executable, "-c", code, pathlib.Path(module_path).stem]
