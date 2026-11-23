"""Regressions: every skill uses the project's days, whatever the timezone of the machine, UTC or the API key."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit._load import load
from tests.unit.project_days.fakes import UTC, World, moment

SKILLS = {"assign-issues": "assign.py", "blocked-issues": "blocked.py", "commitment-delivered": "commitment.py",
          "delivery-quality": "quality.py",
          "scope-stability": "stability.py", "stale-issues": "stale.py", "team-velocity": "velocity.py"}
PROJECT_ZONES = ("-03:00", "+00:00", "+05:30", "+09:00")
KEY_OWNER_ZONES = ("+00:00", "-03:00", "+09:00")
TARGETS = ("gitlab", "plane")
ENVIRONMENT = {"GITLAB_URL": "https://gl.example", "GITLAB_TOKEN": "glpat-secret", "PLANE_URL": "https://plane.example",
               "PLANE_API_KEY": "plane-secret", "PLANE_WORKSPACE": "demo"}
AFTER_THE_SPRINT = "2026-03-25"
LOADED = {}  # loading a script costs far more than running it


def load_once(skill):
    """The tool's module and the shared days module it uses."""
    if skill not in LOADED:
        LOADED[skill] = load(skill, SKILLS[skill]), sys.modules["metrics.days"]
    return LOADED[skill]


def run(skill, world, target, *arguments, now=None):
    """Run the script's CLI against the world. Returns (exit code, stdout, stderr)."""
    module, days = load_once(skill)
    project = "demo/app" if target == "gitlab" else "DEMO"
    argv = [*arguments, "--target", target, "--project", project, "--rate", "6000"]
    stdout, stderr, code, here = io.StringIO(), io.StringIO(), 0, os.getcwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            with mock.patch.dict(os.environ, ENVIRONMENT, clear=True), mock.patch("time.sleep"), \
                    mock.patch.object(module, "http", world.http), frozen_now(days, now), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                module.main(argv)
        except SystemExit as stop:
            code = stop.code
        finally:
            os.chdir(here)
    return code, stdout.getvalue(), stderr.getvalue()


def frozen_now(days, now):
    if not now:
        return contextlib.nullcontext()
    return mock.patch.object(days, "today_in", lambda zone: now.astimezone(zone).date())


class DaysTest(unittest.TestCase):
    def output(self, skill, world, target, *arguments, now=None):
        code, stdout, stderr = run(skill, world, target, *arguments, "--format", "json", now=now)
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def report_row(self, skill, world, target, *arguments):
        rows = self.output(skill, world, target, "report", "--today", AFTER_THE_SPRINT, *arguments)["rows"]
        self.assertEqual(len(rows), 1)
        return rows[0]


class CycleDaysTest(DaysTest):
    def test_every_skill_shows_the_planned_days(self):
        for skill in SKILLS:
            for project in PROJECT_ZONES:
                for key_owner in KEY_OWNER_ZONES:
                    with self.subTest(skill=skill, project=project, key_owner=key_owner):
                        world = World(project, key_owner)
                        period = self.output(skill, world, "plane", "periods", "--today", "2026-03-05")["periods"][0]
                        self.assertEqual((period["start"], period["end"]), ("2026-03-02", "2026-03-15"))


class TodayTest(DaysTest):
    # 01:00 UTC of the day after the sprint: still its last day at -03:00, already over at +09:00
    NOW = moment("03-16T01:00", UTC)

    def is_running(self, skill, world, target):
        result = self.output(skill, world, target, "periods", now=self.NOW)
        if skill == "assign-issues":
            return result["current"] == "Sprint"
        if skill in ("blocked-issues", "stale-issues"):
            return bool(result["periods"])  # they list only the cycles that have not ended
        return result["periods"][0]["status"] == "current"

    def test_the_last_evening_is_still_in_the_sprint(self):
        for skill in SKILLS:
            for target in TARGETS:
                with self.subTest(skill=skill, target=target):
                    self.assertTrue(self.is_running(skill, World("-03:00"), target))

    def test_the_sprint_ends_at_the_projects_midnight(self):
        for skill in SKILLS:
            # blocked and stale list every active milestone, also past ones, so only their cycles end by date
            targets = ("plane",) if skill in ("blocked-issues", "stale-issues") else TARGETS
            for target in targets:
                with self.subTest(skill=skill, target=target):
                    self.assertFalse(self.is_running(skill, World("+09:00"), target))

    def test_the_morning_after_the_last_day_is_out_of_the_sprint(self):
        # A cycle of a -03:00 project is stored as ending at 02:59 UTC of the next day: it is over all the same
        self.NOW = moment("03-16T12:00", UTC)
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self.assertFalse(self.is_running(skill, World("-03:00"), "plane"))

    def test_reports_say_the_projects_today(self):
        for skill in ("blocked-issues", "commitment-delivered", "scope-stability", "stale-issues", "team-velocity"):
            scope = ("--all",) if skill in ("blocked-issues", "stale-issues") else ()
            for target in TARGETS:
                with self.subTest(skill=skill, target=target):
                    result = self.output(skill, World("-03:00"), target, "report", *scope, now=self.NOW)
                    self.assertEqual((result["today"], result["timezone"]), ("2026-03-15", "UTC-03:00"))


class ClosingTest(DaysTest):
    CASES = (("03-15T22:00", True), ("03-15T23:58", True), ("03-16T00:30", False), ("03-16T10:00", False))

    def each_case(self):
        for target in TARGETS:
            for project in PROJECT_ZONES:
                for closed, counts in self.CASES:
                    world = World(project)
                    world.closed = world.updated = moment(closed, world.project)
                    yield target, world, counts, f"{target} {project} closed {closed}"

    def test_velocity_counts_until_the_last_day_ends(self):
        for target, world, counts, case in self.each_case():
            with self.subTest(case):
                row = self.report_row("team-velocity", world, target)
                self.assertEqual((row["velocity"], row["late"]), (3, 0) if counts else (0, 3))

    def test_commitment_counts_until_the_last_day_ends(self):
        for target, world, counts, case in self.each_case():
            with self.subTest(case):
                row = self.report_row("commitment-delivered", world, target)
                self.assertEqual((row["committed"], row["delivered"]), (3, 3 if counts else 0))

    def test_weekly_throughput_uses_the_projects_week(self):
        for target in TARGETS:
            with self.subTest(target=target):
                world = World("-03:00")
                world.closed = world.updated = moment("03-15T22:00", world.project)  # Sunday, Monday in UTC
                rows = self.output("team-velocity", world, target, "report", "--by", "week", "--today", AFTER_THE_SPRINT)["rows"]
                weeks = [row["end"] for row in rows if row["velocity"]]
                self.assertEqual(weeks, ["2026-03-15"])


class StartDayTest(DaysTest):
    CASES = (("03-02T00:30", True), ("03-02T22:00", True), ("03-03T00:30", False))

    def each_case(self):
        for target in TARGETS:
            for project in PROJECT_ZONES:
                for joined, planned in self.CASES:
                    world = World(project)
                    world.joined = world.updated = moment(joined, world.project)
                    yield target, world, planned, f"{target} {project} joined {joined}"

    def test_commitment_is_the_scope_at_the_end_of_the_start_day(self):
        for target, world, planned, case in self.each_case():
            with self.subTest(case):
                row = self.report_row("commitment-delivered", world, target)
                self.assertEqual((row["committed"], row["added"]), (3, 0) if planned else (0, 3))

    def test_stability_is_the_scope_at_the_end_of_the_start_day(self):
        for target, world, planned, case in self.each_case():
            with self.subTest(case):
                row = self.report_row("scope-stability", world, target)
                self.assertEqual((row["initial"], row["added"]), (3, 0) if planned else (0, 3))


class AgeTest(DaysTest):
    def test_idle_time_goes_to_the_end_of_the_projects_today(self):
        for target in TARGETS:
            for project in PROJECT_ZONES:
                with self.subTest(target=target, project=project):
                    world = World(project)
                    world.updated = moment("03-10T23:59", world.project)
                    result = self.output("stale-issues", world, target, "report", "--all", "--stale-days", "14", "--today", "2026-03-20")
                    self.assertEqual([issue["idle_days"] for issue in result["aging"]], [10.0])

    def test_blocked_time_goes_to_the_end_of_the_projects_today(self):
        for target in TARGETS:
            for project in PROJECT_ZONES:
                with self.subTest(target=target, project=project):
                    world = World(project)
                    world.blocked = True
                    world.updated = moment("03-10T23:59", world.project)
                    issue = self.output("blocked-issues", world, target, "report", "--all", "--today", "2026-03-20")["issues"][0]
                    self.assertEqual(issue["idle_days"], 10.0)

    def test_scores_fade_until_the_end_of_the_projects_today(self):
        world = World("-03:00")
        world.members = True
        world.updated = moment("03-10T23:59", world.project)
        arguments = ("scores", "--half-life", "10", "--no-comments", "--today", "2026-03-20")
        module, _ = load_once("assign-issues")
        with mock.patch.object(module, "build_scores", wraps=module.build_scores) as build:
            self.output("assign-issues", world, "plane", *arguments)
        self.assertEqual(build.call_args.args[2], moment("03-20T23:59", world.project))

    def test_work_not_started_is_idle_from_the_start_day(self):
        world = World("-03:00")
        world.updated = moment("02-20T12:00", world.project)
        result = self.output("stale-issues", world, "gitlab", "report", "--period", "Sprint", "--stale-days", "1", "--today", "2026-03-11")
        self.assertAlmostEqual(result["stale"][0]["idle_days"], 9 + (23 * 60 + 59) / (24 * 60))

    def test_blocked_dates_are_the_projects_days(self):
        for target in TARGETS:
            with self.subTest(target=target):
                world = World("-03:00")
                world.blocked = True
                world.comment = moment("03-09T22:00", world.project)  # the 10th in UTC
                issue = self.output("blocked-issues", world, target, "report", "--all", "--today", "2026-03-12")["issues"][0]
                self.assertEqual(issue["blocked_since"], "2026-03-09")
                self.assertEqual(issue["block_comments"][0]["day"], "2026-03-09")

    def test_blocked_report_prints_the_projects_day_of_a_comment(self):
        world = World("-03:00")
        world.blocked = True
        world.comment = moment("03-09T22:00", world.project)
        code, stdout, stderr = run("blocked-issues", world, "plane", "report", "--all", "--today", "2026-03-12")
        self.assertEqual(code, 0, stderr)
        self.assertIn("2026-03-09", stdout)
        self.assertNotIn("2026-03-10", stdout)


class ChoiceTest(DaysTest):
    def test_timezone_option_replaces_the_projects(self):
        world = World("-03:00")
        world.closed = world.updated = moment("03-15T22:00", world.project)
        gitlab = self.report_row("team-velocity", world, "gitlab", "--timezone", "+00:00")
        self.assertEqual(gitlab["velocity"], 0)  # the 16th in UTC, and a milestone's dates do not move
        plane = self.report_row("team-velocity", world, "plane", "--timezone", "+00:00")
        self.assertEqual(plane["end"], "2026-03-16")  # a cycle's dates are times, so they follow the option

    def test_every_skill_has_the_option(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                world = World("+09:00")
                result = self.output(skill, world, "plane", "periods", "--timezone", "+00:00", "--today", "2026-03-05")
                self.assertEqual(result["periods"][0]["start"], "2026-03-01")

    def test_an_invalid_timezone_is_refused(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                code, _, stderr = run(skill, World(), "plane", "periods", "--timezone", "+25:00")
                self.assertEqual(code, 2)
                self.assertIn("unknown timezone", stderr)

    def test_an_unknown_project_timezone_is_utc_with_a_note(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                code, _, stderr = run(skill, World(zone_name="No/Where"), "plane", "periods", "--today", "2026-03-05")
                self.assertEqual(code, 0, stderr)
                self.assertIn("days are in UTC", stderr)

    def test_reports_carry_the_note(self):
        for skill in ("commitment-delivered", "scope-stability", "team-velocity"):
            with self.subTest(skill=skill):
                result = self.output(skill, World(zone_name="No/Where"), "plane", "report", "--today", AFTER_THE_SPRINT)
                self.assertTrue(any("days are in UTC" in note for note in result["notes"]))


if __name__ == "__main__":
    unittest.main()
