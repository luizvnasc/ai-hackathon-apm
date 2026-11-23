#!/usr/bin/env python3
"""The skills' scripts as tools of a stdio MCP server (stdlib only, Python 3.8+). Being migrated skill by skill.

Started by the agent from the project folder, as declared in apm.yml:
    python3 apm_modules/luizvnasc/ai-hackathon-apm/mcp/server.py

It imports shared/ from the package itself, so nothing has to be copied into the skills. Credentials are not
in any config file: the agent starts the server with its own environment (checked for Claude Code and
OpenCode), and shared/config.py reads GITLAB_*/PLANE_* from there or from the project's .env.

URLs, the workspace and the request rate are never tool arguments: a tool call comes from the model, and a
prompt injection could otherwise send a token to another server.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import types

sys.dont_write_bytecode = True
PACKAGE = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "hackathon-metrics", "version": "0.0.2"}

# The agent may call a tool without opening its skill, so the rules that matter most travel with every result
REPORT_RULES = ("How to report this: answer in the user's language. Quote the numbers as printed and do not compute new "
                "ones. Relay every note. Per-person figures show how work was spread: never rank or blame people. "
                "This tool only reads: never change issues yourself and do not offer another way to change them. "
                "The {skill} skill has the details.")
ERROR_RULES = ("How to report this: show the message as it is and stop. Do not try other values or look for credentials "
               "elsewhere, and never ask for a token's value in the chat. The {skill} skill has the details.")

COMMON = {
    "command": {"type": "string", "enum": ["projects", "periods", "report"]},
    "target": {"type": "string", "enum": ["gitlab", "plane"]},
    "project": {"type": "string", "description": "GitLab group/project or Plane identifier"},
    "timezone": {"type": "string", "description": "IANA name or +HH:MM, only when the user gives one"},
    "today": {"type": "string", "description": "YYYY-MM-DD, only when the user gives a date"},
    "format": {"type": "string", "enum": ["md", "json"]},
}
LAST = {"type": "integer", "minimum": 1, "description": "finished periods to analyze (default 6)"}
UNIT = {"type": "string", "enum": ["auto", "points", "count"]}

TOOLS = {
    "team_velocity": {
        "skill": "team-velocity", "script": "velocity.py",
        "summary": "Team velocity of a GitLab or Plane project, from its finished milestones or cycles.",
        "options": {"last": LAST, "unit": UNIT, "by": {"type": "string", "enum": ["period", "week"]},
                    "weeks": {"type": "integer", "minimum": 1}},
    },
    "commitment_delivered": {
        "skill": "commitment-delivered", "script": "commitment.py",
        "summary": "Commitment x delivered (say/do) per milestone or cycle of a GitLab or Plane project.",
        "options": {"last": LAST, "unit": UNIT,
                    "period": {"type": "array", "items": {"type": "string"}, "description": "only these milestones/cycles"},
                    "grace_days": {"type": "integer", "minimum": 0, "description": "days after the start day that are "
                                   "still commitment, only when the user says the team plans late"}},
    },
}


def tool_schema(name):
    tool = TOOLS[name]
    description = (f"{tool['summary']} command projects lists the user's projects, periods lists the milestones/cycles, "
                   f"report computes it. Read-only. Load the {tool['skill']} skill first: it says which options to use "
                   "and how to report the result.")
    properties = {**COMMON, **tool["options"]}
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": ["command", "target"],
                            "additionalProperties": False}}


LOADED = {}  # tool name -> its script, imported on the first call


def load_script(name):
    """The tool's script, with hackathon_shared pointing at the package's shared/ instead of the generated copy."""
    if name not in LOADED:
        shared = types.ModuleType("hackathon_shared")
        shared.__path__ = [str(PACKAGE / "shared")]
        sys.modules["hackathon_shared"] = shared
        tool = TOOLS[name]
        path = PACKAGE / ".apm" / "skills" / tool["skill"] / "scripts" / tool["script"]
        spec = importlib.util.spec_from_file_location(f"skill_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        LOADED[name] = module
    return LOADED[name]


def cli_argv(name, arguments):
    """The CLI arguments of a tool call. Every option is passed as --name=value, so a value that starts with
    a dash is a value, never another option."""
    options = TOOLS[name]["options"].keys() | (COMMON.keys() - {"command", "target"})
    unknown = set(arguments) - options - {"command", "target"}
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    argv = [str(arguments["command"]), f"--target={arguments['target']}"]
    for option in sorted(options):
        value = arguments.get(option)
        if value is None:
            continue
        flag = "--" + option.replace("_", "-")
        values = value if isinstance(value, list) else [value]
        argv += [f"{flag}={item}" for item in values]
    return argv


def run_script(script, argv):
    """(text, failed) of one run, with what the script printed."""
    stdout, stderr = io.StringIO(), io.StringIO()
    failed = False
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            script.main(argv)
        except SystemExit as stop:  # NOT CONFIGURED, API errors and bad arguments end like this in the CLI
            failed = stop.code not in (None, 0)
            if isinstance(stop.code, str):
                print(stop.code, file=sys.stderr)
    parts = [part for part in (stdout.getvalue().strip(), stderr.getvalue().strip()) if part]
    return "\n".join(parts), failed


def answer(message, load=load_script):
    """The reply to one JSON-RPC request, or None for a notification."""
    if "id" not in message:
        return None
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        return result(message, {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                                "capabilities": {"tools": {}}, "serverInfo": SERVER})
    if method == "ping":
        return result(message, {})
    if method == "tools/list":
        return result(message, {"tools": [tool_schema(name) for name in TOOLS]})
    if method == "tools/call":
        return call_tool(message, params, load)
    return error(message, -32601, f"unknown method {method}")


def call_tool(message, params, load):
    name = params.get("name")
    if name not in TOOLS:
        return error(message, -32602, "unknown tool")
    try:
        text, failed = run_script(load(name), cli_argv(name, params.get("arguments") or {}))
    except (ValueError, KeyError) as problem:
        text, failed = f"invalid arguments: {problem}", True
    rules = (ERROR_RULES if failed else REPORT_RULES).format(skill=TOOLS[name]["skill"])
    return result(message, {"content": [{"type": "text", "text": text}, {"type": "text", "text": rules}], "isError": failed})


def result(message, value):
    return {"jsonrpc": "2.0", "id": message["id"], "result": value}


def error(message, code, text):
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": text}}


def serve(lines, write):
    for line in lines:
        if not line.strip():
            continue
        reply = answer(json.loads(line))
        if reply is not None:
            write(json.dumps(reply) + "\n")


def main():
    real_stdout = sys.stdout  # the scripts print: only JSON-RPC may reach the agent

    def write(text):
        real_stdout.write(text)
        real_stdout.flush()

    sys.stdout = sys.stderr
    serve(sys.stdin, write)


if __name__ == "__main__":
    main()
