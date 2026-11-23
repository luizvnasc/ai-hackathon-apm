"""KPI report: one Markdown file with Mermaid charts of the main metrics of a GitLab or Plane project.

    kpis.py projects --target gitlab|plane
    kpis.py report --target gitlab|plane --project <project> [--last 6] [--stale-days N] [--output reports/x.md]

It runs the other tools (velocity, commitment, stability, quality, stale, blocked) with --format json and
draws what they return: it computes no metric of its own, so the numbers match the other skills.
Per period: velocity, commitment x delivered, scope stability, delivery quality over the last finished periods.
Right now: stale and blocked issues of the current milestone/cycle (the whole project when none is running).
The file is written under the current folder (the project the agent runs in); stdout gets a short summary.
"""
import argparse
import contextlib
import io
import json
import pathlib
import re
import sys

from . import blocked, commitment, quality, stability, stale, velocity
from .days import timezone_arg
from .report import md_table

DEFAULT_LAST = 6
REPORTS_FOLDER = "reports"
PERCENT_AXIS = "0 --> 100"
NO_VALUE = "—"


class ToolStop(Exception):
    """A tool ended without a result (NOT CONFIGURED, an API error, a question for the user)."""


# ------------------------------------------------------------------ running the other tools
def run_tool(module, argv):
    """The JSON result of one run of another tool's main(argv)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            module.main(argv)
        except SystemExit as stop:
            if stop.code not in (None, 0):
                raise ToolStop(stop_message(stop, stdout, stderr)) from None
    return json.loads(stdout.getvalue())


def stop_message(stop, stdout, stderr):
    # The NOT CONFIGURED help and the questions for the user are printed before the exit
    parts = [stdout.getvalue().strip(), stderr.getvalue().strip()]
    if isinstance(stop.code, str):
        parts.append(stop.code)
    return "\n".join(part for part in parts if part)


def common_argv(args):
    argv = [f"--target={args.target}", f"--project={args.project}", "--format=json"]
    if args.workspace:
        argv.append(f"--workspace={args.workspace}")
    if args.timezone:
        argv.append(f"--timezone={args.timezone}")
    if args.today:
        argv.append(f"--today={args.today}")
    return argv + passed_through(args)


def passed_through(args):
    """The command-line only options the other tools take as they are (the MCP server never sets them)."""
    argv = []
    if args.gitlab_url:
        argv.append(f"--gitlab-url={args.gitlab_url}")
    if args.plane_url:
        argv.append(f"--plane-url={args.plane_url}")
    if args.rate:
        argv.append(f"--rate={args.rate}")
    return argv


def period_argv(args):
    return ["report", *common_argv(args), f"--last={args.last}"]


def snapshot_argv(args, current_name):
    scope = f"--period={current_name}" if current_name else "--all"
    argv = ["report", *common_argv(args), scope]
    if args.stale_days is not None:
        argv.append(f"--stale-days={args.stale_days}")
    return argv


def collect(args):
    """Every tool's result for the project, keyed by metric."""
    results = {"velocity": run_tool(velocity, period_argv(args)),
               "commitment": run_tool(commitment, period_argv(args)),
               "stability": run_tool(stability, period_argv(args)),
               "quality": run_tool(quality, period_argv(args))}
    current_name = current_period_name(results)
    results["current_name"] = current_name
    results["stale"] = run_tool(stale, snapshot_argv(args, current_name))
    results["blocked"] = run_tool(blocked, snapshot_argv(args, current_name))
    return results


def current_period_name(results):
    """The running milestone/cycle, as the period tools found it, or None."""
    current = results["velocity"].get("current")
    if current:
        return current["name"]
    for row in results["commitment"]["rows"]:
        if row["status"] == "current":
            return row["name"]
    return None


# ------------------------------------------------------------------ numbers to text
def percent(value):
    """0.8125 -> 81 (a chart value)."""
    return round(100 * value)


def percent_text(value):
    return NO_VALUE if value is None else f"{percent(value)}%"


def number_text(value):
    if value is None:
        return NO_VALUE
    if abs(value - round(value)) < 0.05:
        return f"{value:.0f}"
    return f"{value:.1f}"


def whole_days(days):
    return NO_VALUE if days is None else str(int(days))


def chart_number(value):
    return int(round(value)) if abs(value - round(value)) < 0.05 else round(value, 1)


def chart_label(name):
    """A period name as a quoted Mermaid string: no double quotes or line breaks inside."""
    without_quotes = str(name).replace('"', "")
    cleaned = " ".join(without_quotes.split())
    return f'"{cleaned}"'


# ------------------------------------------------------------------ Mermaid
def bar_chart(title, labels, bars, y_axis, line=None):
    """A Mermaid xychart: one bar series, an optional line series (same length as labels)."""
    lines = ["```mermaid", "xychart-beta", f"    title {chart_label(title)}",
             f"    x-axis [{', '.join(chart_label(label) for label in labels)}]",
             f"    y-axis {y_axis}",
             f"    bar [{', '.join(str(value) for value in bars)}]"]
    if line is not None:
        lines.append(f"    line [{', '.join(str(value) for value in line)}]")
    lines.append("```")
    return "\n".join(lines)


def pie_chart(title, slices):
    """A Mermaid pie of the non-zero slices, or None when every slice is zero."""
    kept = [(name, count) for name, count in slices if count]
    if not kept:
        return None
    lines = ["```mermaid", f"pie title {title}"]
    lines += [f"    {chart_label(name)} : {count}" for name, count in kept]
    lines.append("```")
    return "\n".join(lines)


def finished_rows(rows, field):
    """The rows of finished periods that have a value for field (velocity rows have no status)."""
    return [row for row in rows if row.get("status", "finished") == "finished" and row.get(field) is not None]


# ------------------------------------------------------------------ sections
def velocity_section(result):
    rows = result["rows"]
    unit = result["unit"]
    out = [f"## Velocity ({unit})", ""]
    if not rows:
        return out + ["No finished period with issues yet.", ""]
    labels = [row["name"] for row in rows]
    bars = [chart_number(row["velocity"]) for row in rows]
    has_scope = all(row["scope"] is not None for row in rows)
    line = [chart_number(row["scope"]) for row in rows] if has_scope else None
    out.append(bar_chart(f"Delivered on time (bars) vs scope (line), {unit}", labels, bars, f'"{unit}"', line))
    out += ["", md_table(["Period", "Scope", "Delivered", "Completion"],
                         [[row["name"], number_text(row["scope"]), number_text(row["velocity"]),
                           percent_text(row["completion"])] for row in rows]), ""]
    return out


def percent_section(title, rows, field, columns):
    """A chart of one 0-1 KPI per finished period, plus a table of the given (header, field, format) columns."""
    out = [f"## {title}", ""]
    measured = finished_rows(rows, field)
    if not measured:
        return out + ["No finished period with a value yet.", ""]
    labels = [row["name"] for row in measured]
    out.append(bar_chart(f"{title} (%)", labels, [percent(row[field]) for row in measured], f'"%" {PERCENT_AXIS}'))
    table = [[row["name"]] + [show(row.get(key)) for _, key, show in columns] for row in measured]
    out += ["", md_table(["Period"] + [column[0] for column in columns], table), ""]
    return out


def word_text(value):
    return NO_VALUE if value is None else str(value)


def current_section(results):
    name = results["current_name"]
    title = f"Right now: {name}" if name else "Right now: whole project (no milestone/cycle is running)"
    out = [f"## {title}", ""]
    out += pace_lines(results["velocity"].get("current"))
    out += stale_lines(results["stale"])
    out += blocked_lines(results["blocked"])
    return out


def pace_lines(current):
    if not current:
        return []
    return ["**Pace:** " + f"{number_text(current['done'])} of {number_text(current['scope'])} done "
            f"({percent_text(current['done_pct'])}) with {percent_text(current['elapsed_pct'])} of the time gone; "
            f"at the usual pace it ends near {number_text(current['expected_total'])}: **{current['verdict']}**.", ""]


def stale_lines(result):
    out = [f"### Stale issues: {result['stale_count']} of {result['open']} open",
           f"(no activity for more than {number_text(result['stale_days'])} days, or a stale label/state)", ""]
    chart = pie_chart("Stale issues by kind", list(result["by_kind"].items()))
    if chart:
        out += [chart, ""]
    return out


def blocked_lines(result):
    issues = result["issues"]
    out = [f"### Blocked issues: {len(issues)}", ""]
    if not issues:
        return out
    causes = {}
    for issue in issues:
        main_cause = issue["causes"][0]
        causes[main_cause] = causes.get(main_cause, 0) + 1
    out += [pie_chart("Blocked issues by cause", list(causes.items())), ""]
    rows = [[f"[{issue['key']}]({issue['url']})", issue["title"], issue["causes"][0],
             whole_days(issue["blocked_days"])] for issue in issues]
    out += [md_table(["Issue", "Title", "Cause", "Blocked days"], rows), ""]
    return out


# ------------------------------------------------------------------ summary
def summary_rows(results):
    """One row per KPI: latest, median, trend and rating, as the tools gave them."""
    rows = [velocity_summary(results["velocity"])]
    rows.append(period_summary("Commitment x delivered (say/do)", results["commitment"]["summary"], "reliability"))
    rows.append(period_summary("Scope stability", results["stability"]["summary"], "rating"))
    rows.append(period_summary("Delivery quality", results["quality"]["summary"], "rating"))
    stale_result = results["stale"]
    rows.append(["Stale issues (now)", f"{stale_result['stale_count']} of {stale_result['open']} open",
                 NO_VALUE, NO_VALUE, NO_VALUE])
    rows.append(["Blocked issues (now)", str(len(results["blocked"]["issues"])), NO_VALUE, NO_VALUE, NO_VALUE])
    return rows


def velocity_summary(result):
    title = f"Velocity ({result['unit']})"
    summary = result["summary"]
    if not summary:
        return [title, NO_VALUE, NO_VALUE, NO_VALUE, NO_VALUE]
    latest = result["rows"][-1]["velocity"]
    return [title, number_text(latest), number_text(summary["median"]), result.get("trend_word", NO_VALUE),
            result.get("stability", NO_VALUE)]


def period_summary(title, summary, rating_key):
    if not summary:
        return [title, NO_VALUE, NO_VALUE, NO_VALUE, NO_VALUE]
    return [title, percent_text(summary["latest"]), percent_text(summary["median"]), summary["trend_word"],
            summary.get(rating_key) or NO_VALUE]


SUMMARY_HEAD = ["KPI", "Latest", "Median", "Trend", "Rating"]


def all_notes(results):
    """The tools' notes, each once, with the metric it came from."""
    seen, out = set(), []
    for metric in ("velocity", "commitment", "stability", "quality"):
        for note in results[metric]["notes"]:
            if note not in seen:
                seen.add(note)
                out.append(f"- {metric}: {note}")
    return out


# ------------------------------------------------------------------ the document
def render(results, project_title, today, last):
    kind = results["velocity"]["kind"]
    out = [f"# KPI report — {project_title}", "",
           f"Generated {today} · up to {last} finished {kind}s per chart · days in {results['velocity']['timezone']}", "",
           "## Summary", "", md_table(SUMMARY_HEAD, summary_rows(results)), ""]
    out += velocity_section(results["velocity"])
    out += percent_section("Commitment x delivered", results["commitment"]["rows"], "say_do",
                           [("Committed", "committed", number_text), ("Delivered", "delivered", number_text),
                            ("Say/do", "say_do", percent_text)])
    out += percent_section("Scope stability", results["stability"]["rows"], "stability",
                           [("Initial", "initial", number_text), ("Changed", "changed", number_text),
                            ("Stability", "stability", percent_text), ("Rating", "rating", word_text)])
    out += percent_section("Delivery quality", results["quality"]["rows"], "quality",
                           [("Delivered", "delivered", number_text), ("Went to fix", "went_to_fix", number_text),
                            ("Quality", "quality", percent_text), ("Rating", "rating", word_text)])
    out += current_section(results)
    notes = all_notes(results)
    if notes:
        out += ["## Notes", ""] + notes + [""]
    out += ["Charts are Mermaid blocks: they render in GitLab, GitHub and most Markdown viewers.", ""]
    return "\n".join(out)


def render_summary(path, results, project_title):
    """What the agent gets back: where the file is and the summary table, not the charts."""
    return "\n".join([f"Wrote the KPI report of {project_title} to `{path}`.", "",
                      md_table(SUMMARY_HEAD, summary_rows(results)), "",
                      f"Right now: {results['current_name'] or 'whole project (no milestone/cycle is running)'}."])


# ------------------------------------------------------------------ the file
def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "project"


def default_path(project, today, folder):
    return folder / REPORTS_FOLDER / f"kpi-report-{slug(project)}-{today}.md"


def checked_output(requested, folder):
    """The path the user asked for, always inside folder: relative, ending in .md, without '..'; None if none.
    The path comes from the model, so a prompt injection must not be able to write elsewhere."""
    if not requested:
        return None
    candidate = pathlib.PurePath(requested)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix.lower() != ".md":
        sys.exit(f"--output must be a relative path ending in .md, inside this folder (got {requested!r})")
    path = folder / candidate
    if not is_inside(path, folder):  # a symbolic link inside the folder can still point out of it
        sys.exit(f"--output must stay inside this folder (got {requested!r})")
    return path


def is_inside(path, folder):
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def write_report(path, text):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as problem:
        sys.exit(f"Could not write the report to {path}: {problem.strerror or problem}")


# ------------------------------------------------------------------ main
def positive(value):
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a whole number >= 1")
    return int(value)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["projects", "report"])
    parser.add_argument("--target", required=True, choices=["gitlab", "plane"])
    parser.add_argument("--project")
    parser.add_argument("--workspace")
    parser.add_argument("--last", type=positive, default=DEFAULT_LAST, help="finished periods per chart (default 6)")
    parser.add_argument("--stale-days", type=float, help="idle days after which an issue is stale (tools' default)")
    parser.add_argument("--output", help="relative .md path (default reports/kpi-report-<project>-<today>.md)")
    parser.add_argument("--timezone", type=timezone_arg, help="IANA name or +HH:MM: where a day begins and ends")
    parser.add_argument("--today", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="the summary's format")
    parser.add_argument("--rate", type=positive, help="Plane API calls per minute (the tools' default: 60)")
    parser.add_argument("--gitlab-url", help="instead of GITLAB_URL")
    parser.add_argument("--plane-url", help="instead of PLANE_URL")
    return parser.parse_args(argv)


def list_projects(args):
    """The user's projects, as the velocity tool lists them."""
    argv = ["projects", f"--target={args.target}", *passed_through(args)]
    if args.workspace:
        argv.append(f"--workspace={args.workspace}")
    if args.format == "json":
        argv.append("--format=json")
    velocity.main(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "projects":
        return list_projects(args)
    if not args.project:
        sys.exit("report needs --project (run `projects` to list yours)")
    folder = pathlib.Path.cwd()
    requested = checked_output(args.output, folder)  # before any API call
    try:
        results = collect(args)
    except ToolStop as stop:
        sys.exit(str(stop))
    today = results["velocity"]["today"]
    project_title = results["velocity"]["project"]
    path = requested or default_path(args.project, today, folder)
    write_report(path, render(results, project_title, today, args.last))
    shown = path.relative_to(folder)
    if args.format == "json":
        return print(json.dumps({"path": str(shown), "summary": summary_rows(results),
                                 "current": results["current_name"]}, indent=2))
    print(render_summary(shown, results, project_title))
