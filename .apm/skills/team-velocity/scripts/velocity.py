#!/usr/bin/env python3
"""Team velocity for a GitLab or Plane project.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Read-only: nothing is written to GitLab or Plane.

    python3 velocity.py projects --target gitlab|plane
    python3 velocity.py periods  --target ... --project P
    python3 velocity.py report   --target ... --project P [--last 6] [--unit auto|points|count]
                                 [--by period|week] [--weeks 8] [--today YYYY-MM-DD] [--format md|json]

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api). Plane uses
PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY.

Definitions:
  * Period: a GitLab milestone or a Plane cycle with start and end dates. A
    period is finished when GitLab closed it or its end date has passed.
  * Size: the `points::N` label. Plane: the issue's numeric estimate, never a
    label. With
    --unit auto, points are used when every issue of the analyzed periods has
    them, otherwise every issue counts as 1.
  * Velocity of a period: the size of its issues closed (not cancelled) by the
    end of the period. Issues closed later are "late"; open ones "spilled".
    Velocity per week divides by the period length, so periods of different
    lengths compare fairly.
  * Scope: every non-cancelled issue in the period today (the tools do not keep
    the scope at the start). Completion = velocity / scope.
  * Statistics over the last --last finished periods: mean, median, min, max,
    standard deviation, coefficient of variation (stability), the average of the
    last 3 and the trend (least-squares slope per period).
  * Recommendation for the next period: median velocity per week x its length
    (conservative: the lowest of the last 3).
  * Current period: done so far against the weekly median.
  * --by week: weekly throughput over the last --weeks full weeks, for teams
    without milestones/cycles.
"""
import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.request

# APM installs each skill by itself: hackathon_shared/ is generated from the package's shared/.
sys.dont_write_bytecode = True  # never write into an installed skill
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python3 -I leaves it out
from hackathon_shared.config import load_dotenv, require_config  # noqa: E402
from hackathon_shared.dates import to_date  # noqa: E402
from hackathon_shared.gitlab import GitLabClient  # noqa: E402
from hackathon_shared.labels import has_cancelled_label, label_points  # noqa: E402
from hackathon_shared.net import ApiError  # noqa: E402
from hackathon_shared.plane import PlaneClient, estimate, full_label_names  # noqa: E402
from hackathon_shared.report import md_table  # noqa: E402
from hackathon_shared.stats import slope  # noqa: E402

UA = "team-velocity-skill/1.0"
def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA, **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "null"), resp.headers
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"), method, url) from None
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {url}: {e.reason}")


def new_issue(**kw):
    it = {"key": None, "title": "", "url": None, "closed": None, "cancelled": False, "points": None,
          "assignees": [], "period": None}
    it.update(kw)
    return it


# ------------------------------------------------------------------ GitLab
class GitLab(GitLabClient):
    def __init__(self, url, token, project):
        if not url:
            sys.exit("GitLab URL missing: set GITLAB_URL or pass --gitlab-url")
        if not token:
            sys.exit("GITLAB_TOKEN is not set (a personal access token with the read_api scope)")
        super().__init__(url, token, project)

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def periods(self):
        q = """query($p: ID!) { project(fullPath: $p) { name milestones(includeAncestors: true, first: 100) {
                 nodes { title state startDate dueDate } } } }"""
        proj = self.graphql(q, {"p": self.project})["project"]
        if not proj:
            sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
        self.name = proj["name"]
        return [{"name": m["title"], "start": m["startDate"], "end": m["dueDate"], "closed": m["state"] == "closed"}
                for m in proj["milestones"]["nodes"]]

    def issues(self):
        q = """query($p: ID!, $after: String) { project(fullPath: $p) {
                 workItems(types: [ISSUE, TASK, INCIDENT], first: 100, after: $after) {
                   pageInfo { hasNextPage endCursor }
                   nodes { iid title state webUrl closedAt widgets { type
                     ... on WorkItemWidgetLabels { labels { nodes { title } } }
                     ... on WorkItemWidgetAssignees { assignees { nodes { username } } }
                     ... on WorkItemWidgetMilestone { milestone { title } } } } } } }"""
        out, after = [], None
        while True:
            page = self.graphql(q, {"p": self.project, "after": after})["project"]["workItems"]
            for n in page["nodes"]:
                w = {x.get("type"): x for x in n.get("widgets") or []}
                labels = [l["title"] for l in ((w.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
                closed = n["state"] != "OPEN"
                out.append(new_issue(
                    key=f"#{n['iid']}", title=n["title"], url=n["webUrl"],
                    closed=n.get("closedAt") if closed else None,
                    cancelled=closed and has_cancelled_label(labels),
                    points=label_points(labels),
                    assignees=[a["username"] for a in ((w.get("ASSIGNEES") or {}).get("assignees") or {}).get("nodes", [])],
                    period=((w.get("MILESTONE") or {}).get("milestone") or {}).get("title")))
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]


# ------------------------------------------------------------------ Plane
class Plane(PlaneClient):
    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        if not (url and workspace and api_key):
            sys.exit("Plane needs PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY (or --plane-url/--workspace)")
        super().__init__(url, workspace, api_key, rate_per_min)
        self.select_project(project)

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def periods(self):
        self._cycles = [c for c in self.list(f"/projects/{self.pid}/cycles/") if not c.get("archived_at")]
        return [{"name": c["name"], "id": c["id"], "start": (c.get("start_date") or "")[:10] or None,
                 "end": (c.get("end_date") or "")[:10] or None,
                 "closed": (c.get("status") or "").lower() == "completed"} for c in self._cycles]

    def issues(self):
        label_names = {l["id"]: l["name"] for l in self.list(f"/projects/{self.pid}/labels/")}
        out, seen = [], set()
        for c in self._cycles:
            for r in self.list(f"/projects/{self.pid}/cycles/{c['id']}/cycle-issues/", {"expand": "state,labels,assignees,estimate_point"}):
                seen.add(r["id"])
                out.append(self.to_issue(r, label_names, c["name"]))
        return out

    def to_issue(self, r, label_names, period):
        state = r.get("state") if isinstance(r.get("state"), dict) else {}
        group = state.get("group")
        labels = full_label_names(r.get("labels"), label_names)
        return new_issue(
            key=f"{self.ident}-{r['sequence_id']}", title=r["name"],
            url=f"{self.url}/{self.ws}/projects/{self.pid}/issues/{r['id']}",
            closed=(r.get("completed_at") or r.get("updated_at")) if group in ("completed", "cancelled") else None,
            cancelled=group == "cancelled",
            points=estimate(r),
            assignees=[a.get("display_name") or a.get("email") for a in r.get("assignees") or [] if isinstance(a, dict)],
            period=period)


# ------------------------------------------------------------------ math
def weeks_between(start, end):
    return max(1, (end - start).days + 1) / 7.0


def choose_unit(issues, unit):
    if unit != "auto":
        return unit
    real = [i for i in issues if not i["cancelled"]]
    return "points" if real and all(i["points"] is not None for i in real) else "count"


def size(it, unit):
    return (it["points"] or 0) if unit == "points" else 1


def period_stats(p, issues, unit, today):
    start, end = to_date(p["start"]), to_date(p["end"])
    mine = [i for i in issues if i["period"] == p["name"] and not i["cancelled"]]
    on_time = [i for i in mine if i["closed"] and to_date(i["closed"]) <= end]
    late = [i for i in mine if i["closed"] and to_date(i["closed"]) > end]
    open_ = [i for i in mine if not i["closed"]]
    scope = sum(size(i, unit) for i in mine)
    velocity = sum(size(i, unit) for i in on_time)
    per_member = {}
    for i in on_time:
        share = size(i, unit) / max(1, len(i["assignees"]))
        for a in i["assignees"] or ["(unassigned)"]:
            per_member[a] = per_member.get(a, 0) + share
    weeks = weeks_between(start, end)
    return {"name": p["name"], "start": p["start"], "end": p["end"], "weeks": weeks,
            "issues": len(mine), "scope": scope, "velocity": velocity, "per_week": velocity / weeks,
            "late": sum(size(i, unit) for i in late), "spilled": sum(size(i, unit) for i in open_),
            "completion": (velocity / scope) if scope else None, "per_member": per_member,
            "unpointed": sum(1 for i in mine if i["points"] is None)}


def summarize(rows):
    v = [r["velocity"] for r in rows]
    w = [r["per_week"] for r in rows]
    if not v:
        return None
    mean = statistics.mean(v)
    sd = statistics.pstdev(v) if len(v) > 1 else 0.0
    comp = [r["completion"] for r in rows if r["completion"] is not None]
    return {"n": len(v), "mean": mean, "median": statistics.median(v), "min": min(v), "max": max(v), "stdev": sd,
            "cv": (sd / mean) if mean else None, "last3": statistics.mean(v[-3:]), "trend": slope(v),
            "median_per_week": statistics.median(w), "min_last3_per_week": min(w[-3:]),
            "completion": statistics.mean(comp) if comp else None}


def weekly(issues, unit, today, weeks):
    monday = today - dt.timedelta(days=today.weekday())
    out = []
    for k in range(weeks, 0, -1):
        ws = monday - dt.timedelta(weeks=k)
        we = ws + dt.timedelta(days=6)
        done = [i for i in issues if i["closed"] and not i["cancelled"] and ws <= to_date(i["closed"]) <= we]
        out.append({"name": f"{ws.isoformat()} – {we.isoformat()}", "start": ws.isoformat(), "end": we.isoformat(),
                    "weeks": 1.0, "issues": len(done), "velocity": sum(size(i, unit) for i in done),
                    "per_week": sum(size(i, unit) for i in done), "scope": None, "late": None, "spilled": None,
                    "completion": None, "per_member": {}, "unpointed": sum(1 for i in done if i["points"] is None)})
    return out


# ------------------------------------------------------------------ output
def f1(x, pct=False):
    if x is None:
        return "—"
    return f"{100 * x:.0f}%" if pct else (f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}")


def render(res):
    u = res["unit"]
    out = [f"# Team velocity — {res['project']}", "",
           f"Unit: **{u}** · basis: **{res['basis']}** · today {res['today']}", ""]
    rows = res["rows"]
    if not rows:
        out.append(f"No finished {res['kind']} with dates and issues yet. Try `--by week`.")
        return "\n".join(out)
    if res["basis"] == "week":
        out.append(md_table(["Week", "Issues done", f"Velocity ({u})"],
                            [[r["name"], r["issues"], f1(r["velocity"])] for r in rows]))
    else:
        out.append(md_table([res["kind"].capitalize(), "Dates", "Weeks", "Issues", f"Scope", "Velocity", "Per week",
                             "Completion", "Closed late", "Still open"],
                            [[r["name"], f"{r['start']} → {r['end']}", f1(r["weeks"]), r["issues"], f1(r["scope"]),
                              f"**{f1(r['velocity'])}**", f1(r["per_week"]), f1(r["completion"], True),
                              f1(r["late"]), f1(r["spilled"])] for r in rows]))
    s = res["summary"]
    out += ["", "## Summary", "",
            md_table(["Periods", "Mean", "Median", "Min", "Max", "Std dev", "Variation", "Last 3 avg", "Trend / period",
                      "Median per week", "Avg completion"],
                     [[s["n"], f1(s["mean"]), f1(s["median"]), f1(s["min"]), f1(s["max"]), f1(s["stdev"]),
                       f1(s["cv"], True), f1(s["last3"]),
                       "—" if s["trend"] is None else f"{s['trend']:+.1f}", f1(s["median_per_week"]),
                       f1(s["completion"], True)]]), ""]
    out.append(f"Stability: **{res['stability']}** · trend: **{res['trend_word']}**")
    if res["members"]:
        names = res["members"]
        out += ["", "## Velocity per person (on-time work, shared evenly between assignees)", "",
                md_table(["Person"] + [r["name"] for r in rows] + ["Total", "Share"],
                         [["@" + m if not m.startswith("(") else m] + [f1(r["per_member"].get(m, 0)) for r in rows]
                          + [f1(res["member_totals"][m]), f1(res["member_totals"][m] / res["total"], True)]
                          for m in names])]
    nxt = res.get("next")
    if nxt:
        out += ["", "## Next period", "",
                f"**{nxt['name']}** ({nxt['start']} → {nxt['end']}, {f1(nxt['weeks'])} weeks): plan about "
                f"**{f1(nxt['recommended'])} {u}** (median per week × length); conservative "
                f"{f1(nxt['conservative'])}. It holds {f1(nxt['scope'])} {u} today"
                + (f" — **{f1(nxt['scope'] - nxt['recommended'])} over** the recommendation." if nxt["scope"] > nxt["recommended"] * 1.1
                   else " — within the recommendation." if nxt["scope"] >= nxt["recommended"] * 0.7
                   else " — room for more.")]
    cur = res.get("current")
    if cur:
        out += ["", "## Current period", "",
                f"**{cur['name']}** ({cur['start']} → {cur['end']}): {f1(cur['done'])} of {f1(cur['scope'])} {u} done "
                f"({f1(cur['done_pct'], True)}) with {f1(cur['elapsed_pct'], True)} of the time gone. "
                f"Pace so far: {f1(cur['pace'])} {u}/week against a median of {f1(cur['median_pace'])}"
                + (f" (**{f1(1 - cur['pace'] / cur['median_pace'], True)} slower**). " if cur['median_pace'] and cur['pace'] < 0.8 * cur['median_pace']
                   else ". ") +
                f"At the median pace the team delivers about {f1(cur['expected_total'])} {u} by the end, so "
                f"**{cur['verdict']}**" + (f" (about {f1(cur['gap'])} {u} short)." if cur["gap"] > 0 else ".")]
    for n in res["notes"]:
        out.append(f"- {n}")
    return "\n".join(out)


def render_periods(res):
    rows = [[n, p["name"], p["start"] or "—", p["end"] or "—", p["status"]] for n, p in enumerate(res["periods"], 1)]
    return "\n".join([f"# {res['kind'].capitalize()}s — {res['project']}", "",
                      md_table(["#", res["kind"].capitalize(), "Start", "End", "Status"], rows)])


# ------------------------------------------------------------------ main


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "report"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project")
    ap.add_argument("--last", type=int, default=6, help="finished periods to analyze (default 6)")
    ap.add_argument("--unit", choices=["auto", "points", "count"], default="auto")
    ap.add_argument("--by", choices=["period", "week"], default="period")
    ap.add_argument("--weeks", type=int, default=8, help="--by week: full weeks to analyze (default 8)")
    ap.add_argument("--today")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=int, default=60)
    a = ap.parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace, scope="read_api")

    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    if a.command != "projects" and not a.project:
        sys.exit(f"{a.command} needs --project (run `projects` to list yours)")
    try:
        src = (GitLab(a.gitlab_url, os.environ.get("GITLAB_TOKEN"), (a.project or "").strip("/") or None)
               if a.target == "gitlab" else
               Plane(a.plane_url, a.workspace, os.environ.get("PLANE_API_KEY"), a.project, a.rate))
        if a.command == "projects":
            rows = src.my_projects()
            if a.format == "json":
                return print(json.dumps(rows, indent=2))
            return print("\n".join([f"# Your {'GitLab' if a.target == 'gitlab' else 'Plane'} projects", "",
                                    md_table(["#", "Project", "Name"],
                                             [[n, f"[{r['path']}]({r['url']})", r["name"]] for n, r in enumerate(rows, 1)])]))

        periods = [p for p in src.periods() if p["start"] and p["end"]]
        for p in periods:
            s, e = to_date(p["start"]), to_date(p["end"])
            p["finished"] = p["closed"] or e < today
            p["status"] = "finished" if p["finished"] else ("current" if s <= today <= e else "upcoming")
        periods.sort(key=lambda p: p["start"])
        if a.command == "periods":
            res = {"project": src.name, "kind": src.kind, "periods": periods}
            return print(json.dumps(res, indent=2) if a.format == "json" else render_periods(res))

        issues = src.issues()
    except ApiError as e:
        sys.exit(f"API error: {e}")

    notes = []
    finished = [p for p in periods if p["finished"]][-a.last:]
    basis = a.by
    in_scope = [i for i in issues if i["period"] in {p["name"] for p in finished}] if basis == "period" else issues
    unit = choose_unit(in_scope, a.unit)
    rows = []
    if basis == "period":
        rows = [period_stats(p, issues, unit, today) for p in finished]
        rows = [r for r in rows if r["issues"]]
        if not rows:
            notes.append(f"No finished {src.kind} has issues: showing weekly throughput instead.")
            # The unit was chosen from the finished periods' issues; pick it again from
            # every issue, as --by week does.
            basis, in_scope = "week", issues
            unit = choose_unit(in_scope, a.unit)
    if basis == "week":
        rows = weekly(issues, unit, today, a.weeks)
    if a.unit == "auto" and unit == "count" and in_scope:
        missing = sum(1 for i in in_scope if i["points"] is None and not i["cancelled"])
        where = "a `points::N` label" if a.target == "gitlab" else "an Estimate"
        notes.append(f"{missing} issue(s) have no points ({where}), so every issue counts as 1. "
                     "Use --unit points to count only pointed work.")

    summary = summarize(rows)
    res = {"project": src.name, "kind": src.kind, "unit": unit, "basis": basis, "today": today.isoformat(),
           "rows": rows, "summary": summary, "notes": notes, "members": [], "member_totals": {}, "total": 0}
    if summary:
        cv = summary["cv"] or 0
        res["stability"] = "stable" if cv < 0.2 else "variable" if cv < 0.4 else "unstable"
        tr = summary["trend"]
        res["trend_word"] = ("not enough periods" if tr is None else
                             "rising" if tr > 0.1 * max(summary["mean"], 1) else
                             "falling" if tr < -0.1 * max(summary["mean"], 1) else "flat")
        if summary["n"] < 3:
            notes.append(f"Only {summary['n']} {basis}(s) of history: treat the averages as a rough guide.")
        totals = {}
        for r in rows:
            for m, v in r["per_member"].items():
                totals[m] = totals.get(m, 0) + v
        res["member_totals"], res["total"] = totals, sum(totals.values()) or 1
        res["members"] = sorted(totals, key=lambda m: -totals[m])

        if basis == "period":
            mpw = summary["median_per_week"]
            current = [p for p in periods if p["status"] == "current"]
            upcoming = [p for p in periods if p["status"] == "upcoming"]
            if current:
                p = current[0]
                st = period_stats(p, issues, unit, today)
                s, e = to_date(p["start"]), to_date(p["end"])
                elapsed = min(1.0, max(0.0, ((today - s).days + 1) / ((e - s).days + 1)))
                remaining_weeks = max(0.0, (e - today).days) / 7.0
                done_now = sum(size(i, unit) for i in issues if i["period"] == p["name"] and not i["cancelled"]
                               and i["closed"] and to_date(i["closed"]) <= today)
                expected = done_now + mpw * remaining_weeks
                gap = st["scope"] - expected
                elapsed_weeks = max(1, (min(today, e) - s).days + 1) / 7.0
                res["current"] = {"name": p["name"], "start": p["start"], "end": p["end"], "scope": st["scope"],
                                  "pace": done_now / elapsed_weeks, "median_pace": mpw,
                                  "done": done_now, "done_pct": done_now / st["scope"] if st["scope"] else None,
                                  "elapsed_pct": elapsed, "expected_total": expected, "gap": max(0.0, gap),
                                  "verdict": "on track" if gap <= 0 else "at risk" if gap <= 0.15 * st["scope"]
                                  else "off track"}
            nxt = upcoming[0] if upcoming else None
            if nxt:
                st = period_stats(nxt, issues, unit, today)
                res["next"] = {"name": nxt["name"], "start": nxt["start"], "end": nxt["end"], "weeks": st["weeks"],
                               "scope": st["scope"], "recommended": mpw * st["weeks"],
                               "conservative": summary["min_last3_per_week"] * st["weeks"]}
    print(json.dumps(res, indent=2, default=str) if a.format == "json" else render(res))


if __name__ == "__main__":
    main()
