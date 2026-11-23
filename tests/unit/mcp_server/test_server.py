"""mcp/server.py: the JSON-RPC answers, how a tool call becomes CLI arguments, and what reaches the agent."""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("mcp_server", ROOT / "mcp" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def request(method, params=None, ident=1):
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}


def call(arguments):
    return request("tools/call", {"name": "team_velocity", "arguments": arguments})


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.load = mock.Mock()

    def test_initialize_echoes_the_clients_version(self):
        reply = server.answer(request("initialize", {"protocolVersion": "2024-11-05"}), self.load)
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(reply["result"]["capabilities"], {"tools": {}})

    def test_notifications_get_no_reply(self):
        self.assertIsNone(server.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.load))

    def test_lists_the_tool(self):
        tools = server.answer(request("tools/list"), self.load)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["team_velocity", "commitment_delivered"])
        for tool in tools:
            properties = tool["inputSchema"]["properties"]
            self.assertFalse({"gitlab_url", "plane_url", "workspace", "rate"} & set(properties), tool["name"])

    def test_unknown_method_and_tool(self):
        self.assertEqual(server.answer(request("nope"), self.load)["error"]["code"], -32601)
        reply = server.answer(request("tools/call", {"name": "nope"}), self.load)
        self.assertEqual(reply["error"]["code"], -32602)


class ArgumentsTest(unittest.TestCase):
    def test_every_option_is_bound_to_its_value(self):
        argv = server.cli_argv("team_velocity", {"command": "report", "target": "gitlab", "project": "g/app", "last": 3})
        self.assertEqual(argv, ["report", "--target=gitlab", "--last=3", "--project=g/app"])

    def test_a_value_that_looks_like_an_option_stays_a_value(self):
        argv = server.cli_argv("team_velocity", {"command": "report", "target": "gitlab", "project": "--gitlab-url=https://evil"})
        self.assertEqual(argv[-1], "--project=--gitlab-url=https://evil")

    def test_urls_cannot_be_passed(self):
        for name in ("gitlab_url", "plane_url", "workspace", "rate"):
            with self.assertRaises(ValueError):
                server.cli_argv("team_velocity", {"command": "report", "target": "gitlab", name: "x"})

    def test_a_script_exit_is_a_tool_error_with_its_message(self):
        velocity = mock.Mock()
        velocity.main.side_effect = SystemExit("API error: HTTP 401")
        text, failed = server.run_script(velocity, server.cli_argv("team_velocity", {"command": "projects", "target": "gitlab"}))
        self.assertEqual((text, failed), ("API error: HTTP 401", True))

    def test_every_result_carries_the_reporting_rules(self):
        velocity = mock.Mock()
        reply = server.answer(call({"command": "projects", "target": "gitlab"}), lambda name: velocity)
        self.assertEqual(reply["result"]["content"][1]["text"], server.REPORT_RULES.format(skill="team-velocity"))
        self.assertIn("never rank or blame people", server.REPORT_RULES)
        self.assertIn("do not offer another way", server.REPORT_RULES)

    def test_an_error_carries_the_error_rules(self):
        velocity = mock.Mock()
        velocity.main.side_effect = SystemExit("NOT CONFIGURED: missing GITLAB_URL.")
        reply = server.answer(call({"command": "projects", "target": "gitlab"}), lambda name: velocity)
        self.assertEqual(reply["result"]["content"][1]["text"], server.ERROR_RULES.format(skill="team-velocity"))
        self.assertIn("never ask for a token", server.ERROR_RULES)

    def test_each_tool_points_to_its_skill(self):
        for name, tool in server.TOOLS.items():
            self.assertIn(f"Load the {tool['skill']} skill first", server.tool_schema(name)["description"])

    def test_each_tool_runs_its_own_skills_script(self):
        for name, tool in server.TOOLS.items():
            path = server.PACKAGE / ".apm" / "skills" / tool["skill"] / "scripts" / tool["script"]
            self.assertTrue(path.is_file(), path)

    def test_repeated_and_dashed_options(self):
        argv = server.cli_argv("commitment_delivered", {"command": "report", "target": "plane", "period": ["S1", "S 2"],
                                                       "grace_days": 2})
        self.assertEqual(argv, ["report", "--target=plane", "--grace-days=2", "--period=S1", "--period=S 2"])

    def test_bad_arguments_are_a_tool_error(self):
        reply = server.answer(call({"command": "report"}), lambda name: mock.Mock())
        self.assertTrue(reply["result"]["isError"])


class EndToEndTest(unittest.TestCase):
    """The real server in a subprocess, against the eval mock."""

    def converse(self, *messages, credentials=True):
        env = {"PATH": os.environ.get("PATH", ""), "TZ": "UTC"}
        if credentials:
            fixtures = ROOT / "tests" / "evals"
            env.update(PLANE_URL="http://plane.mock", PLANE_WORKSPACE="eval", PLANE_API_KEY="plane-eval-fake",
                       GITLAB_URL="http://gitlab.mock", GITLAB_TOKEN="glpat-eval-fake",
                       PYTHONPATH=str(fixtures / "mock"), EVAL_FIXTURES=str(fixtures / "fixtures"), EVAL_TODAY="2026-11-12")
        lines = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, str(ROOT / "mcp" / "server.py")], input=lines, env=env, cwd=str(ROOT.parent),
                             capture_output=True, text=True, timeout=120)
        return [json.loads(line) for line in out.stdout.splitlines()]

    def test_commitment_delivered_through_the_tool(self):
        message = request("tools/call", {"name": "commitment_delivered",
                                         "arguments": {"command": "report", "target": "gitlab", "project": "group/inv",
                                                       "format": "json"}})
        report = json.loads(self.converse(message)[0]["result"]["content"][0]["text"])
        self.assertEqual([row["say_do"] for row in report["rows"][:4]], [0.5, 0.6, 0.5, 1.0])

    def test_only_json_rpc_reaches_the_agent(self):
        replies = self.converse(request("initialize"), call({"command": "report", "target": "plane", "project": "INV", "format": "json"}, ))
        self.assertEqual([r["id"] for r in replies], [1, 1])
        report = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual([row["velocity"] for row in report["rows"]], [10, 14, 12, 10])

    def test_not_configured(self):
        replies = self.converse(call({"command": "projects", "target": "plane"}), credentials=False)
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("NOT CONFIGURED: missing PLANE_URL, PLANE_WORKSPACE, PLANE_API_KEY.", replies[0]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
