#!/usr/bin/env python3
"""The skills' tools: a stdio MCP server (stdlib only, Python 3.8+).

Started by the agent from the project folder, as declared in apm.yml:
    python3 apm_modules/luizvnasc/ai-hackathon-apm/mcp/server.py

All the code is in metrics/ next to this file, one module per tool plus the modules they share;
the skills hold only their instructions. Credentials are not in any config file: the agent starts the server
with its own environment (checked for Claude Code and OpenCode), and metrics/config.py reads
GITLAB_*/PLANE_* from there or from the project's .env.

URLs, the workspace and the request rate are never tool arguments: a tool call comes from the model, and a
prompt injection could otherwise send a token to another server.
"""
import contextlib
import importlib
import io
import json
import pathlib
import sys

sys.dont_write_bytecode = True  # never write into the installed package
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I leaves it out
PACKAGE = HERE.parent
PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "hackathon-metrics", "version": "0.1.0"}

# The agent may call a tool without opening its skill, so the rules that matter most travel with every result
REPORT_RULES = ("How to report this: answer in the user's language. Quote the numbers as printed and do not compute new "
                "ones. Relay every note. Per-person figures show how work was spread: never rank or blame people. "
                "This tool only reads: never change issues yourself and do not offer another way to change them. "
                "The {skill} skill has the details.")
ERROR_RULES = ("How to report this: show the message as it is and stop. Do not try other values or look for credentials "
               "elsewhere, and never ask for a token's value in the chat. The {skill} skill has the details.")

# For the two tools that can write: the report rules, with the confirmation rule instead of "only reads"
WRITE_RULES = {
    "assign_issues": ("How to report this: answer in the user's language and quote the numbers as printed. Scores are "
                      "relative to the team: never rank or blame people. Write only with yes: true, and only after the "
                      "user confirmed that exact change in this conversation; before that, show the dry_run plan. The "
                      "assign-issues skill has the details."),
    "populate_project": ("How to report this: answer in the user's language. Every call writes unless dry_run is true: "
                         "run with dry_run: true first, show the plan, and write only in a later turn, after the user "
                         "approved it. The populate-project skill has the details."),
}

REPORT_COMMANDS = ["projects", "periods", "report"]
COMMON = {
    "target": {"type": "string", "enum": ["gitlab", "plane"]},
    "project": {"type": "string", "description": "GitLab group/project or Plane identifier"},
    "timezone": {"type": "string", "description": "IANA name or +HH:MM, only when the user gives one"},
    "format": {"type": "string", "enum": ["md", "json"]},
}
LAST = {"type": "integer", "minimum": 1, "description": "finished periods to analyze (default 6); only when the user "
        "gives a number"}
UNIT = {"type": "string", "enum": ["auto", "points", "count"]}
PERIODS = {"type": "array", "items": {"type": "string"}, "description": "only these milestones/cycles"}
GRACE_DAYS = {"type": "integer", "minimum": 0, "description": "days after the start day that still count, only when "
              "the user says the team plans late"}
ONE_PERIOD = {"type": "string", "description": "milestone title (GitLab) or cycle name (Plane)"}
WHOLE_PROJECT = {"type": "boolean", "description": "the whole project instead of one milestone/cycle"}
STALE_DAYS = {"type": "number", "minimum": 0}

# "commands" are the first argument of the module's command line; None for a module without one. "key" is written only in the
# skill's SKILL.md: a call must pass it as skill_read, so the agent reads the skill before using the tool.
TOOLS = {
    "team_velocity": {
        "skill": "team-velocity", "key": "dacb2b", "module": "velocity", "commands": REPORT_COMMANDS,
        "summary": "Team velocity of a GitLab or Plane project, from its finished milestones or cycles.",
        "options": {"last": LAST, "unit": UNIT, "by": {"type": "string", "enum": ["period", "week"]},
                    "weeks": {"type": "integer", "minimum": 1}},
    },
    "commitment_delivered": {
        "skill": "commitment-delivered", "key": "0139f6", "module": "commitment", "commands": REPORT_COMMANDS,
        "summary": "Commitment x delivered (say/do) per milestone or cycle of a GitLab or Plane project.",
        "options": {"last": LAST, "unit": UNIT, "period": PERIODS, "grace_days": GRACE_DAYS},
    },
    "scope_stability": {
        "skill": "scope-stability", "key": "be7a6d", "module": "stability", "commands": REPORT_COMMANDS,
        "summary": "Scope stability: how much a GitLab milestone or Plane cycle changed while it ran.",
        "options": {"last": LAST, "unit": UNIT, "period": PERIODS, "grace_days": GRACE_DAYS},
    },
    "delivery_quality": {
        "skill": "delivery-quality", "key": "e38097", "module": "quality", "commands": REPORT_COMMANDS,
        "summary": "Delivery quality: how many delivered issues of a milestone or cycle went back to to fix.",
        "options": {"last": LAST, "period": PERIODS,
                    "fix_names": {"type": "string", "description": "the team's names for the to fix step, comma "
                                  "separated; replaces the default, only when the user names it"}},
    },
    "stale_issues": {
        "skill": "stale-issues", "key": "618fef", "module": "stale", "commands": REPORT_COMMANDS,
        "summary": "Stale issues of a GitLab milestone, Plane cycle or whole project.",
        "options": {"period": ONE_PERIOD, "all": WHOLE_PROJECT, "stale_days": STALE_DAYS},
    },
    "blocked_issues": {
        "skill": "blocked-issues", "key": "cd2ab2", "module": "blocked", "commands": REPORT_COMMANDS,
        "summary": "Blocked issues of a GitLab milestone, Plane cycle or whole project, with cause and next step.",
        "options": {"period": ONE_PERIOD, "all": WHOLE_PROJECT, "stale_days": STALE_DAYS,
                    "deep": {"type": "boolean", "description": "also read every open issue's relations (slow in Plane)"}},
    },
    "assign_issues": {
        "skill": "assign-issues", "key": "6eb483", "module": "assign",
        "commands": ["projects", "periods", "scores", "suggest-member", "suggest-issue", "assign"],
        "summary": ("Area scores of a team and who should take an issue, or which issue fits a developer; assign "
                    "writes only with yes after the user's confirmation."),
        "options": {"issue": {"type": "string"}, "member": {"type": "string"}, "period": ONE_PERIOD, "all": WHOLE_PROJECT,
                    "roles": {"type": "string", "description": "comma list (default developer)"},
                    "half_life": {"type": "number", "minimum": 0}, "include_assigned": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1}, "replace": {"type": "boolean"},
                    "dry_run": {"type": "boolean"}, "no_comments": {"type": "boolean"},
                    "yes": {"type": "boolean", "description": "write the change; only after the user confirmed it"}},
    },
    "populate_project": {
        "skill": "populate-project", "key": "9a261d", "module": "populate", "commands": None,
        "summary": ("Populate a GitLab or Plane project with a label set and a Kanban board. It writes unless "
                    "dry_run is true."),
        "options": {"labels": {"type": "string", "description": "path of the approved label set (JSON); default: "
                               "the skill's labels.default.json"},
                    "board_name": {"type": "string"}, "create_project": {"type": "boolean"},
                    "identifier": {"type": "string"}, "priority_labels": {"type": "boolean"},
                    "dry_run": {"type": "boolean", "description": "read and print the plan, write nothing"}},
    },
}


def tool_schema(name):
    tool = TOOLS[name]
    reads_only = "" if name in WRITE_RULES else " Read-only."
    description = (f"{tool['summary']}{reads_only} Load the {tool['skill']} skill first: it says which command and "
                   "options to use and how to report the result.")
    properties = {"skill_read": {"type": "string", "description": f"the value given in the {tool['skill']} skill"}}
    properties.update(COMMON)
    required = ["skill_read", "target"]
    if tool["commands"]:
        properties["command"] = {"type": "string", "enum": tool["commands"]}
        required.insert(0, "command")
    else:
        required.append("project")
    properties.update(tool["options"])
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False}}


def load_module(name):
    """The tool's module in metrics/."""
    return importlib.import_module(f"metrics.{TOOLS[name]['module']}")


def cli_argv(name, arguments):
    """The CLI arguments of a tool call. Every option is passed as --name=value, so a value that starts with
    a dash is a value, never another option."""
    tool = TOOLS[name]
    options = tool["options"].keys() | (COMMON.keys() - {"target"})
    unknown = set(arguments) - options - {"command", "target"}
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    argv = [str(arguments["command"])] if tool["commands"] else []
    argv.append(f"--target={arguments['target']}")
    for option in sorted(options):
        argv += option_flags(option, arguments.get(option))
    if name == "populate_project" and not arguments.get("labels"):
        argv.append(f"--labels={default_labels()}")
    return argv


def option_flags(option, value):
    """--name=value for each value; a true boolean is the bare flag, a false or missing one adds nothing."""
    flag = "--" + option.replace("_", "-")
    if value is None or value is False:
        return []
    if value is True:
        return [flag]
    values = value if isinstance(value, list) else [value]
    return [f"{flag}={item}" for item in values]


def default_labels():
    return PACKAGE / ".apm" / "skills" / "populate-project" / "labels.default.json"


def run_script(script, argv):
    """(text, failed) of one run of a module's main(), with what it printed."""
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


def answer(message, load=load_module):
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
    arguments = dict(params.get("arguments") or {})
    if arguments.pop("skill_read", None) != TOOLS[name]["key"]:
        return result(message, {"content": [{"type": "text", "text": skill_first(name)}], "isError": True})
    try:
        text, failed = run_script(load(name), cli_argv(name, arguments))
    except (ValueError, KeyError) as problem:
        text, failed = f"invalid arguments: {problem}", True
    rules = rules_for(name, failed)
    return result(message, {"content": [{"type": "text", "text": text}, {"type": "text", "text": rules}], "isError": failed})


def skill_first(name):
    skill = TOOLS[name]["skill"]
    return (f"Nothing was run: load the {skill} skill first (it gives the skill_read value), follow it, then call "
            "this tool again.")


def rules_for(name, failed):
    if failed:
        return ERROR_RULES.format(skill=TOOLS[name]["skill"])
    return WRITE_RULES.get(name) or REPORT_RULES.format(skill=TOOLS[name]["skill"])


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
