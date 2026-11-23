"""Delivery quality: how much of what a GitLab milestone or Plane cycle delivered had to go back to "to fix".

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Read-only: nothing is written to GitLab or Plane.

    quality projects --target gitlab|plane
    quality periods  --target ... --project P
    quality report   --target ... --project P [--last 6] [--period NAME ...]
                                [--fix-names "to fix"] [--timezone TZ] [--today YYYY-MM-DD] [--format md|json]

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api). Plane uses
PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY. Unset variables are read from a
.env file in the current directory; the environment wins.

Definitions:
  * Went to fix: GitLab: the label workflow::to-fix (or status::, state::, or no
    scope) was added. Plane: the issue entered a state called To Fix, or got such
    a label. Spelling does not matter (to-fix, to fix, tofix); --fix-names adds
    other names.
  * Delivered: closed, not cancelled, while in the period (also after its end).
  * A pass counts for a period when the issue was in that period at that moment.
  * Quality = 1 - delivered issues that went to fix / delivered issues. An issue
    counts once however many times it went back; every pass is listed.
    >= 90% good, >= 75% attention, otherwise poor.
  * Issues that went to fix and are still open are listed, never counted.
  * Days, today included: Plane project timezone; GitLab: the offset the
    instance writes in its times. --timezone overrides both.
"""
import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import load_dotenv, require_config
from .dates import day_of, parse_dt, start_of_day
from .days import timezone_arg
from .gitlab import GitLabClient
from .labels import has_cancelled_label
from .net import ApiError
from .plane import MAX_ATTEMPTS, PlaneClient, retry_delay
from .report import md_table
from .stats import slope

UA = "delivery-quality-skill/1.0"
GOOD, ATTENTION = 0.9, 0.75
TREND_STEP = 0.05  # quality points per period that make a trend
DEFAULT_FIX_NAMES = ("to fix",)
FLOW_SCOPES = ("workflow", "status", "state")
MAX_LISTED = 50  # issues per period in markdown; the JSON has them all


# ------------------------------------------------------------------ HTTP
def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA, **headers}
    request = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode() or "null"), response.headers
    except urllib.error.HTTPError as error:
        raise ApiError(error.code, error.read().decode(errors="replace"), method, url) from None
    except (urllib.error.URLError, OSError) as error:
        raise ApiError(0, f"cannot reach the server: {getattr(error, 'reason', error)}", method, url) from None
    except ValueError:
        raise ApiError(0, "the response is not JSON", method, url) from None


def fetch(method, url, headers, body=None):
    """http() with the retries of the Plane client, for GitLab. Every request of this script is a read."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return http(method, url, headers, body)
        except ApiError as error:
            delay = retry_delay(error.status, attempt)
            if delay is None or attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(delay)


# ------------------------------------------------------------------ what "to fix" is
def simplified(name):
    """'To-Fix', 'to fix' and 'tofix' are the same name."""
    return re.sub(r"[\s_-]+", "", (name or "").lower())


def is_fix_label(label, fix_names):
    """`fix_names` are simplified names."""
    if "::" not in label:
        return simplified(label) in fix_names
    scope, value = label.rsplit("::", 1)
    return scope.strip().lower() in FLOW_SCOPES and simplified(value) in fix_names


def fix_names_arg(text):
    names = [name.strip() for name in text.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("give at least one name, e.g. \"to fix,rework\"")
    return names


# ------------------------------------------------------------------ issues
def new_issue(**fields):
    """`moves` is the milestone/cycle history [(when, period name or None)], oldest first, and `initial` the period
    before the first move. `fixes` are the moments the issue went to fix."""
    issue = {"key": None, "title": "", "url": None, "closed": None, "cancelled": False, "period": None,
             "initial": None, "moves": [], "fixes": [], "missing_history": False}
    issue.update(fields)
    return issue


def period_at(issue, moment):
    """The period the issue was in at that moment."""
    if not issue["moves"]:
        return issue["period"]  # no history: it has always been where it is now
    period = issue["initial"]
    for when, to in issue["moves"]:
        if when > moment:
            break
        period = to
    return period


def was_delivered(issue, name):
    if not issue["closed"] or issue["cancelled"]:
        return False
    return period_at(issue, issue["closed"]) == name


def passes_in(issue, name):
    return [when for when in issue["fixes"] if period_at(issue, when) == name]


def needs_history(issue, updated, since, chosen):
    """An issue not changed since the analyzed periods began has nothing to read there, unless it is in one of
    them now: it may have gone to fix while planned in it, before it started."""
    return issue["period"] in chosen or updated is None or updated >= since


# ------------------------------------------------------------------ GitLab
GITLAB_ISSUES = """query($p: ID!, $after: String) { project(fullPath: $p) {
  workItems(types: [ISSUE, TASK, INCIDENT], first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { iid title state webUrl createdAt updatedAt closedAt widgets { type
      ... on WorkItemWidgetLabels { labels { nodes { title } } }
      ... on WorkItemWidgetMilestone { milestone { title } } } } } } }"""
GITLAB_MILESTONES = """query($p: ID!, $after: String) { project(fullPath: $p) { name lastActivityAt
  milestones(includeAncestors: true, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor } nodes { title state startDate dueDate } } } }"""


class GitLab(GitLabClient):
    def send(self, method, url, body=None):
        return fetch(method, url, self.headers, body)

    def periods(self):
        periods, after = [], None
        while True:
            project = self.graphql(GITLAB_MILESTONES, {"p": self.project, "after": after})["project"]
            if not project:
                sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
            self.name = project["name"]
            self.learn_times(project.get("lastActivityAt"))
            page = project["milestones"]
            periods += [{"name": m["title"], "start": m["startDate"], "end": m["dueDate"],
                         "closed": m["state"] == "closed"} for m in page["nodes"]]
            info = page.get("pageInfo") or {}  # a page without it is the only one
            if not info.get("hasNextPage"):
                return periods
            after = info["endCursor"]

    def work_items(self):
        nodes, after = [], None
        while True:
            page = self.graphql(GITLAB_ISSUES, {"p": self.project, "after": after})["project"]["workItems"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return nodes
            after = page["pageInfo"]["endCursor"]

    def issues(self, since, chosen, fix_names):
        issues = []
        for node in self.work_items():
            self.learn_times(node.get("createdAt"), node.get("updatedAt"), node.get("closedAt"))
            issue = self.to_issue(node)
            if needs_history(issue, parse_dt(node["updatedAt"]), since, chosen):
                self.read_history(issue, node["iid"], fix_names)
            issues.append(issue)
        return issues

    @staticmethod
    def to_issue(node):
        widgets = {w.get("type"): w for w in node.get("widgets") or []}
        labels = [l["title"] for l in ((widgets.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
        closed = parse_dt(node.get("closedAt")) if node["state"] != "OPEN" else None
        milestone = (widgets.get("MILESTONE") or {}).get("milestone") or {}
        return new_issue(key=f"#{node['iid']}", title=node["title"], url=node["webUrl"], closed=closed,
                         cancelled=bool(closed) and has_cancelled_label(labels), period=milestone.get("title"),
                         initial=milestone.get("title"))

    def read_history(self, issue, iid, fix_names):
        path = f"/projects/{urllib.parse.quote(self.project, safe='')}/issues/{iid}"
        moves = sorted(self.rest_pages(path + "/resource_milestone_events"), key=lambda e: e["created_at"])
        if moves:
            # A milestone is single-valued: "add X" moves the issue to X, "remove X" leaves it with none
            first = moves[0]
            issue["initial"] = (first.get("milestone") or {}).get("title") if first["action"] == "remove" else None
            issue["moves"] = [(parse_dt(e["created_at"]), milestone_after(e)) for e in moves]
        else:
            issue["missing_history"] = bool(issue["period"])
        labels = self.rest_pages(path + "/resource_label_events")
        issue["fixes"] = sorted(parse_dt(e["created_at"]) for e in labels
                                if e["action"] == "add" and is_fix_label((e.get("label") or {}).get("name") or "", fix_names))


def milestone_after(event):
    return (event.get("milestone") or {}).get("title") if event["action"] == "add" else None


# ------------------------------------------------------------------ Plane
class Plane(PlaneClient):
    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        super().__init__(url, workspace, api_key, rate_per_min)
        self.select_project(project)

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def periods(self):
        self.cycles = [c for c in self.list(f"/projects/{self.pid}/cycles/") if not c.get("archived_at")]
        return [{"name": c["name"], "start": self.local_day(c.get("start_date")),
                 "end": self.local_day(c.get("end_date")),
                 "closed": (c.get("status") or "").lower() == "completed"} for c in self.cycles]

    def current_cycles(self):
        """The cycle each issue is in now."""
        current = {}
        for cycle in self.cycles:
            for row in self.list(f"/projects/{self.pid}/cycles/{cycle['id']}/cycle-issues/"):
                current[row.get("id") or row.get("issue")] = cycle["name"]
        return current

    def issues(self, since, chosen, fix_names):
        current = self.current_cycles()
        issues = []
        for row in self.list(f"/projects/{self.pid}/issues/", {"expand": "state"}):
            if row.get("is_draft"):
                continue
            issue = self.to_issue(row, current.get(row["id"]))
            if needs_history(issue, parse_dt(row.get("updated_at")), since, chosen):
                activities = self.list(f"/projects/{self.pid}/issues/{row['id']}/activities/")
                self.read_history(issue, activities, fix_names)
            issues.append(issue)
        return issues

    def to_issue(self, row, cycle):
        group = (row.get("state") if isinstance(row.get("state"), dict) else {}).get("group")
        closed = None
        if group == "completed":  # a cancelled issue is never delivered
            closed = parse_dt(row.get("completed_at")) or parse_dt(row.get("updated_at"))
        return new_issue(key=f"{self.ident}-{row['sequence_id']}", title=row["name"],
                         url=f"{self.url}/{self.ws}/projects/{self.pid}/issues/{row['id']}", closed=closed,
                         cancelled=group == "cancelled", period=cycle, initial=cycle)

    def read_history(self, issue, activities, fix_names):
        ordered = sorted(activities, key=lambda a: parse_dt(a["created_at"]))
        cycles = [a for a in ordered if (a.get("field") or "").lower() in ("cycles", "cycle")]
        if cycles:
            # One cycle at a time: created/updated move it to the new one, deleted takes it out
            issue["initial"] = cycles[0].get("old_value") or None
            issue["moves"] = [(parse_dt(a["created_at"]), None if a.get("verb") == "deleted" else a.get("new_value") or None)
                              for a in cycles]
        else:
            issue["missing_history"] = bool(issue["period"])
        issue["fixes"] = [parse_dt(a["created_at"]) for a in ordered if went_to_fix(a, fix_names)]


def went_to_fix(activity, fix_names):
    field, value = activity.get("field"), activity.get("new_value") or ""
    if field == "state":
        return simplified(value) in fix_names
    return field == "labels" and bool(value) and is_fix_label(value, fix_names)


# ------------------------------------------------------------------ math
def rating(quality):
    if quality is None:
        return None
    if quality >= GOOD:
        return "good"
    return "attention" if quality >= ATTENTION else "poor"


def mark_status(periods, today):
    dated = [p for p in periods if p["start"] and p["end"]]
    for p in dated:
        start, end = dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"])
        if p["closed"] or end < today:
            p["status"] = "finished"
        else:
            p["status"] = "current" if start <= today else "upcoming"
    return sorted(dated, key=lambda p: p["start"])


def select(periods, wanted, last):
    """--period names, or the last `last` finished periods plus the current one."""
    if not wanted:
        finished = [p for p in periods if p["status"] == "finished"][-last:]
        return finished + [p for p in periods if p["status"] == "current"]
    names = {name.strip().lower() for name in wanted}
    chosen = [p for p in periods if p["name"].strip().lower() in names and p["status"] != "upcoming"]
    unknown = names - {p["name"].strip().lower() for p in chosen}
    if unknown:
        available = ", ".join(p["name"] for p in periods if p["status"] != "upcoming")
        sys.exit(f"not found, not started yet or without dates: {', '.join(sorted(unknown))}. Available: {available}")
    return chosen


def listed(issue, passes, zone):
    return {"key": issue["key"], "title": issue["title"], "url": issue["url"],
            "passes": [day_of(when, zone).isoformat() for when in passes]}


def measure(period, issues, zone):
    name = period["name"]
    delivered = [i for i in issues if was_delivered(i, name)]
    returned = [listed(i, passes_in(i, name), zone) for i in delivered if passes_in(i, name)]
    still_open = [listed(i, passes_in(i, name), zone) for i in issues if not i["closed"] and passes_in(i, name)]
    quality = 1 - len(returned) / len(delivered) if delivered else None
    return {"name": name, "start": period["start"], "end": period["end"], "status": period["status"],
            "delivered": len(delivered), "went_to_fix": len(returned), "passes": sum(len(r["passes"]) for r in returned),
            "quality": quality, "rating": rating(quality), "returned": returned, "still_open": still_open}


def summarize(rows):
    values = [r["quality"] for r in rows if r["status"] == "finished" and r["quality"] is not None]
    if not values:
        return None
    trend = slope(values)
    median = statistics.median(values)
    return {"n": len(values), "median": median, "mean": statistics.mean(values), "min": min(values),
            "max": max(values), "latest": values[-1], "rating": rating(median), "trend": trend,
            "trend_word": trend_word(trend)}


def trend_word(trend):
    if trend is None:
        return "not enough periods"
    if trend > TREND_STEP:
        return "improving"
    return "getting worse" if trend < -TREND_STEP else "steady"


def notes_for(rows, summary, issues, kind):
    notes = period_notes(rows, summary)
    if rows and not any(r["passes"] or r["still_open"] for r in rows):
        notes.append("No issue went to fix in these periods. If the team uses another name for it, "
                     "pass --fix-names \"<name>,<name>\".")
    missing = sum(1 for i in issues if i["missing_history"])
    if missing:
        notes.append(f"{missing} issue(s) have no {kind} history: they count as always in their current {kind}, "
                     "so a pass made while they were in another one is counted here.")
    return notes


def period_notes(rows, summary):
    notes = []
    if summary and summary["n"] < 3:
        notes.append(f"Only {summary['n']} finished period(s) with deliveries: treat the median and trend as a rough guide.")
    if any(r["status"] == "current" for r in rows):
        notes.append("The current period is still running: its quality counts only what was delivered so far.")
    empty = [r["name"] for r in rows if not r["delivered"]]
    if empty:
        notes.append(f"Nothing delivered in {', '.join(empty)}: no quality there.")
    return notes


def analyze(project, kind, periods, issues, chosen, zone, fix_names):
    rows = [measure(p, issues, zone) for p in chosen]
    summary = summarize(rows)
    return {"project": project, "kind": kind, "fix_names": list(fix_names), "timezone": str(zone),
            "rows": rows, "summary": summary, "notes": notes_for(rows, summary, issues, kind)}


# ------------------------------------------------------------------ output
def pct(value):
    if value is None:
        return "—"
    shown = round(100 * value + 1e-9)
    if rating(shown / 100) != rating(value):
        shown = int(100 * value)  # 89.6% must not show as 90% next to "attention"
    return f"{shown}%"


def cell(text):
    return re.sub(r"[\r\n]+", " ", str(text)).replace("|", "\\|")


def link(item):
    return f"[{item['key']}]({item['url']}) {cell(item['title'])}"


def history_lines(summary):
    if not summary:
        return []
    table = md_table(["Periods", "Median", "Mean", "Min", "Max", "Latest", "Trend / period"],
                     [[summary["n"], pct(summary["median"]), pct(summary["mean"]), pct(summary["min"]),
                       pct(summary["max"]), pct(summary["latest"]),
                       "—" if summary["trend"] is None else f"{100 * summary['trend']:+.0f} pp"]])
    return ["", "## History (finished periods)", "", table, "",
            f"Typical: **{summary['rating']}** (median) · trend: **{summary['trend_word']}**"]


def issue_lines(row):
    lines = []
    if row["returned"]:
        lines += ["", f"## Went to fix — {cell(row['name'])}", "",
                  md_table(["Issue", "Passes", "Days"],
                           [[link(i), len(i["passes"]), ", ".join(i["passes"])] for i in row["returned"][:MAX_LISTED]])]
    if row["still_open"]:
        lines += ["", f"Went to fix and still open in {cell(row['name'])}, not counted: "
                  + ", ".join(f"{link(i)} ({len(i['passes'])}×)" for i in row["still_open"][:MAX_LISTED])]
    return lines


def render(res):
    kind = res["kind"]
    out = [f"# Delivery quality — {cell(res['project'])}", "",
           f"To fix: {', '.join(res['fix_names'])} · days in {res['timezone']} · today {res['today']}", ""]
    if not res["rows"]:
        out.append(f"No started {kind} with start and end dates to analyze.")
        return "\n".join(out + [f"- {n}" for n in res["notes"]])
    out.append(md_table(
        [kind.capitalize(), "Dates", "Status", "Delivered", "Went to fix", "Passes", "Quality", "Rating"],
        [[cell(r["name"]), f"{r['start']} → {r['end']}", "current (partial)" if r["status"] == "current" else r["status"],
          r["delivered"], r["went_to_fix"], r["passes"], f"**{pct(r['quality'])}**", r["rating"] or "—"]
         for r in res["rows"]]))
    out += history_lines(res["summary"])
    for row in res["rows"]:
        out += issue_lines(row)
    return "\n".join(out + [""] + [f"- {n}" for n in res["notes"]])


def render_periods(res):
    rows = [[n, cell(p["name"]), p["start"], p["end"], p["status"]] for n, p in enumerate(res["periods"], 1)]
    return "\n".join([f"# {res['kind'].capitalize()}s — {cell(res['project'])}", "",
                      md_table(["#", res["kind"].capitalize(), "Start", "End", "Status"], rows)])


def render_projects(target, rows):
    title = "GitLab" if target == "gitlab" else "Plane"
    return "\n".join([f"# Your {title} projects", "",
                      md_table(["#", "Project", "Name"],
                               [[n, f"[{r['path']}]({r['url']})", cell(r["name"])] for n, r in enumerate(rows, 1)])])


# ------------------------------------------------------------------ main
def positive(value):
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a whole number >= 1")
    return int(value)


def date_arg(value):
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from None


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "report"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project")
    ap.add_argument("--last", type=positive, default=6, help="finished periods to analyze (default 6)")
    ap.add_argument("--period", action="append", help="analyze this milestone/cycle (repeatable)")
    ap.add_argument("--fix-names", type=fix_names_arg, default=list(DEFAULT_FIX_NAMES),
                    help='names of the "to fix" state or label, comma separated (default "to fix")')
    ap.add_argument("--timezone", type=timezone_arg, help="IANA name or +HH:MM: where a day begins and ends")
    ap.add_argument("--today", type=date_arg)
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=positive, default=60)
    return ap.parse_args(argv)


def connect(a):
    if a.target == "gitlab":
        return GitLab(a.gitlab_url, os.environ["GITLAB_TOKEN"], (a.project or "").strip("/") or None)
    return Plane(a.plane_url, a.workspace, os.environ["PLANE_API_KEY"], a.project, a.rate)


def show(a, result, markdown):
    print(json.dumps(result, indent=2, default=str) if a.format == "json" else markdown(result))


def report(a, src):
    """(result) of the report command. Reads the periods, then the issues and their history."""
    periods = mark_status(src.periods(), src.today())
    chosen = select(periods, a.period, a.last)
    issues = []
    if chosen:
        first = min(dt.date.fromisoformat(p["start"]) for p in chosen)
        # A day earlier: the instance's offset is only fully known after the issues are read
        since = start_of_day(first, src.tz) - dt.timedelta(days=1)
        names = {p["name"] for p in chosen}
        issues = src.issues(since, names, {simplified(name) for name in a.fix_names})
    result = analyze(src.name, src.kind, periods, issues, chosen, src.tz, a.fix_names)
    result["today"] = src.today().isoformat()
    if src.tz_note:
        result["notes"].append(src.tz_note)
    return result


def main(argv=None):
    load_dotenv()
    a = parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace)
    if a.command != "projects" and not a.project:
        sys.exit(f"{a.command} needs --project (run `projects` to list yours)")
    try:
        src = connect(a)
        src.set_days(a.timezone, a.today)
        if a.command == "projects":
            return show(a, src.my_projects(), lambda rows: render_projects(a.target, rows))
        if a.command == "periods":
            periods = mark_status(src.periods(), src.today())
            src.warn_about_days()
            return show(a, {"project": src.name, "kind": src.kind, "periods": periods}, render_periods)
        result = report(a, src)
    except ApiError as error:
        sys.exit(f"API error: {error}")
    show(a, result, render)
