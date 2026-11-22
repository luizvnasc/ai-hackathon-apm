#!/usr/bin/env python3
"""Prototype: the team-velocity skill as a tool of a stdio MCP server (stdlib only, Python 3.8+).

Started by the agent from the project folder, as declared in apm.yml:
    python3 apm_modules/luizvnasc/ai-hackathon-apm/mcp/server.py

It imports shared/ from the package itself, so nothing has to be copied into the skill. Credentials are not
in any config file: the agent starts the server with its own environment (checked for Claude Code and
OpenCode), and shared/config.py reads GITLAB_*/PLANE_* from there or from the project's .env.

The URLs are never tool arguments: a tool call comes from the model, and a prompt injection could
otherwise send a token to another server.
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
SERVER = {"name": "hackathon-metrics", "version": "0.0.1"}

# The agent may call the tool without opening the skill, so the rules that matter most travel with every result
REPORT_RULES = ("How to report this: answer in the user's language. Quote the numbers as printed and do not compute new "
                "ones. Relay every note. The per-person split shows how work was spread: never rank or blame people. "
                "This tool only reads: never change issues yourself and do not offer another way to change them. "
                "The team-velocity skill has the details.")
ERROR_RULES = ("How to report this: show the message as it is and stop. Do not try other values or look for credentials "
               "elsewhere, and never ask for a token's value in the chat. The team-velocity skill has the details.")

VELOCITY_TOOL = {
    "name": "team_velocity",
    "description": ("Team velocity of a GitLab or Plane project, from its finished milestones or cycles. "
                    "command projects lists the user's projects, periods lists the milestones/cycles, report "
                    "computes the velocity. Read-only. Load the team-velocity skill first: it says which options "
                    "to use and how to report the result."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["projects", "periods", "report"]},
            "target": {"type": "string", "enum": ["gitlab", "plane"]},
            "project": {"type": "string", "description": "GitLab group/project or Plane identifier"},
            "last": {"type": "integer", "minimum": 1, "description": "finished periods to analyze (default 6)"},
            "unit": {"type": "string", "enum": ["auto", "points", "count"]},
            "by": {"type": "string", "enum": ["period", "week"]},
            "weeks": {"type": "integer", "minimum": 1},
            "timezone": {"type": "string", "description": "IANA name or +HH:MM"},
            "today": {"type": "string", "description": "YYYY-MM-DD, only when the user gives a date"},
            "format": {"type": "string", "enum": ["md", "json"]},
        },
        "required": ["command", "target"],
        "additionalProperties": False,
    },
}
OPTIONS = ("project", "last", "unit", "by", "weeks", "timezone", "today", "format")


def load_velocity():
    """velocity.py, with hackathon_shared pointing at the package's shared/ instead of the generated copy."""
    shared = types.ModuleType("hackathon_shared")
    shared.__path__ = [str(PACKAGE / "shared")]
    sys.modules["hackathon_shared"] = shared
    path = PACKAGE / ".apm" / "skills" / "team-velocity" / "scripts" / "velocity.py"
    spec = importlib.util.spec_from_file_location("velocity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def velocity_argv(arguments):
    """The CLI arguments of a tool call. Every option is passed as --name=value, so a value that starts with
    a dash is a value, never another option."""
    unknown = set(arguments) - set(OPTIONS) - {"command", "target"}
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    argv = [str(arguments["command"]), f"--target={arguments['target']}"]
    for name in OPTIONS:
        if arguments.get(name) is not None:
            argv.append(f"--{name}={arguments[name]}")
    return argv


def run_velocity(velocity, arguments):
    """(text, failed) of one report, with what the script printed."""
    stdout, stderr = io.StringIO(), io.StringIO()
    failed = False
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            velocity.main(velocity_argv(arguments))
        except SystemExit as stop:  # NOT CONFIGURED, API errors and bad arguments end like this in the CLI
            failed = stop.code not in (None, 0)
            if isinstance(stop.code, str):
                print(stop.code, file=sys.stderr)
    text = stdout.getvalue() + (("\n" if stdout.getvalue() else "") + stderr.getvalue() if stderr.getvalue() else "")
    return text.strip(), failed


def answer(message, velocity):
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
        return result(message, {"tools": [VELOCITY_TOOL]})
    if method == "tools/call":
        return call_tool(message, params, velocity)
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"unknown method {method}"}}


def call_tool(message, params, velocity):
    if params.get("name") != VELOCITY_TOOL["name"]:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": "unknown tool"}}
    try:
        text, failed = run_velocity(velocity, params.get("arguments") or {})
    except (ValueError, KeyError) as error:
        text, failed = f"invalid arguments: {error}", True
    rules = ERROR_RULES if failed else REPORT_RULES
    return result(message, {"content": [{"type": "text", "text": text}, {"type": "text", "text": rules}], "isError": failed})


def result(message, value):
    return {"jsonrpc": "2.0", "id": message["id"], "result": value}


def serve(lines, write):
    velocity = load_velocity()
    for line in lines:
        if not line.strip():
            continue
        reply = answer(json.loads(line), velocity)
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
