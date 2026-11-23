"""Commitment x delivered (say/do ratio) for a GitLab or Plane project.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Read-only: nothing is written to GitLab or Plane.

    commitment projects --target gitlab|plane
    commitment periods  --target ... --project P
    commitment report   --target ... --project P [--last 6] [--period NAME ...]
                                   [--unit auto|points|count] [--grace-days 0]
                                   [--today YYYY-MM-DD] [--format md|json]

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api). Plane uses
PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY. Unset variables are read from a
.env file in the current directory; the environment wins.

Definitions:
  * Period: a GitLab milestone or a Plane cycle with start and end dates.
  * Commitment: the issues that were in the period at the end of its start day
    (plus --grace-days), rebuilt from the history: GitLab milestone events,
    Plane cycle activity. An issue with no history counts as in its current
    period since it was created.
  * Delivered (committed): committed issues closed, not cancelled, by the end
    of the period while still in it. Say/do = delivered committed / commitment.
  * Added: issues that joined after the commitment cutoff and before the end
    (unplanned work), and were still in the period at the end or delivered in
    it; issues that joined and left again count nowhere. Removed: committed issues taken out of the period before
    its end. Cancelled: committed issues closed as cancelled/won't fix/duplicate.
  * Carried over: committed issues neither delivered nor removed nor cancelled.
  * Total delivered: delivered committed + delivered added.
    Delivered vs commitment = total delivered / commitment (can pass 100%).
  * Size: GitLab: the `points::N` label. Plane: the issue's estimate (the
    Estimates field, when it is numeric), never a label. Both as
    they are today. With --unit auto, points are used when every issue has them,
    otherwise every issue counts as 1.
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

from .config import load_dotenv, require_config
from .dates import end_of_day, parse_dt, start_of_day, to_date
from .days import timezone_arg
from .gitlab import GitLabClient
from .labels import has_cancelled_label, label_points
from .net import ApiError
from .plane import PlaneClient, estimate, full_label_names
from .report import md_table
from .stats import slope

UA = "commitment-delivered-skill/1.0"
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
    """`events` is the period history: [(datetime, period name or None)], oldest first;
    `initial` is the period the issue had when it was created."""
    it = {"key": None, "title": "", "url": None, "created": None, "updated": None, "closed": None,
          "cancelled": False, "points": None, "assignees": [], "period": None, "initial": None, "events": None}
    it.update(kw)
    return it


# ------------------------------------------------------------------ GitLab
class GitLab(GitLabClient):
    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def periods(self):
        q = """query($p: ID!) { project(fullPath: $p) { name lastActivityAt milestones(includeAncestors: true, first: 100) {
                 nodes { title state startDate dueDate } } } }"""
        proj = self.graphql(q, {"p": self.project})["project"]
        if not proj:
            sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
        self.name = proj["name"]
        self.learn_times(proj.get("lastActivityAt"))
        return [{"name": m["title"], "start": m["startDate"], "end": m["dueDate"], "closed": m["state"] == "closed"}
                for m in proj["milestones"]["nodes"]]

    def issues(self, since):
        q = """query($p: ID!, $after: String) { project(fullPath: $p) {
                 workItems(types: [ISSUE, TASK, INCIDENT], first: 100, after: $after) {
                   pageInfo { hasNextPage endCursor }
                   nodes { iid title state webUrl createdAt updatedAt closedAt widgets { type
                     ... on WorkItemWidgetLabels { labels { nodes { title } } }
                     ... on WorkItemWidgetAssignees { assignees { nodes { username } } }
                     ... on WorkItemWidgetMilestone { milestone { title } } } } } } }"""
        out, after = [], None
        while True:
            page = self.graphql(q, {"p": self.project, "after": after})["project"]["workItems"]
            for n in page["nodes"]:
                self.learn_times(n.get("createdAt"), n.get("updatedAt"), n.get("closedAt"))
                w = {x.get("type"): x for x in n.get("widgets") or []}
                labels = [l["title"] for l in ((w.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
                closed = n["state"] != "OPEN"
                out.append(new_issue(
                    iid=n["iid"], key=f"#{n['iid']}", title=n["title"], url=n["webUrl"],
                    created=n["createdAt"], updated=n["updatedAt"], closed=n.get("closedAt") if closed else None,
                    cancelled=closed and has_cancelled_label(labels),
                    points=label_points(labels),
                    assignees=[a["username"] for a in ((w.get("ASSIGNEES") or {}).get("assignees") or {}).get("nodes", [])],
                    period=((w.get("MILESTONE") or {}).get("milestone") or {}).get("title")))
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        pid = urllib.parse.quote(self.project, safe="")
        for it in out:
            # An issue not touched since the first analyzed period began kept its milestone all along.
            if parse_dt(it["updated"]) < since:
                continue
            ev = self.rest_pages(f"/projects/{pid}/issues/{it['iid']}/resource_milestone_events")
            ev.sort(key=lambda e: e["created_at"])
            if ev:
                # A milestone is single-valued: "add X" moves the issue to X, "remove X" leaves it with none.
                first = ev[0]
                it["initial"] = (first.get("milestone") or {}).get("title") if first["action"] == "remove" else None
                it["events"] = [(parse_dt(e["created_at"]),
                                 (e.get("milestone") or {}).get("title") if e["action"] == "add" else None) for e in ev]
        return out


# ------------------------------------------------------------------ Plane
class Plane(PlaneClient):
    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        super().__init__(url, workspace, api_key, rate_per_min)
        self.select_project(project)

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def periods(self):
        self._cycles = [c for c in self.list(f"/projects/{self.pid}/cycles/") if not c.get("archived_at")]
        return [{"name": c["name"], "id": c["id"], "start": self.local_day(c.get("start_date")),
                 "end": self.local_day(c.get("end_date")),
                 "closed": (c.get("status") or "").lower() == "completed"} for c in self._cycles]

    def issues(self, since):
        label_names = {l["id"]: l["name"] for l in self.list(f"/projects/{self.pid}/labels/")}
        cycle_names = {c["id"]: c["name"] for c in self._cycles}
        current = {}
        for c in self._cycles:
            for r in self.list(f"/projects/{self.pid}/cycles/{c['id']}/cycle-issues/"):
                current[r.get("id") or r.get("issue")] = c["name"]
        out = []
        for r in self.list(f"/projects/{self.pid}/issues/", {"expand": "state,labels,assignees,estimate_point"}):
            it = self.to_issue(r, label_names, current.get(r["id"]))
            if parse_dt(it["updated"]) >= since:
                self.history(it, r["id"], cycle_names)
            out.append(it)
        return out

    def history(self, it, issue_id, cycle_names):
        def name(ident, value):
            return cycle_names.get(ident) or value or None
        ev = [a for a in self.list(f"/projects/{self.pid}/issues/{issue_id}/activities/")
              if (a.get("field") or "").lower() in ("cycles", "cycle")]
        ev.sort(key=lambda a: a["created_at"])
        if not ev:
            return
        # An issue belongs to one cycle at a time: created/updated move it to the new one, deleted takes it out.
        it["initial"] = name(ev[0].get("old_identifier"), ev[0].get("old_value"))
        it["events"] = [(parse_dt(a["created_at"]),
                         None if (a.get("verb") or "") == "deleted" else name(a.get("new_identifier"), a.get("new_value")))
                        for a in ev]

    def to_issue(self, r, label_names, period):
        state = r.get("state") if isinstance(r.get("state"), dict) else {}
        group = state.get("group")
        labels = full_label_names(r.get("labels"), label_names)
        return new_issue(
            key=f"{self.ident}-{r['sequence_id']}", title=r["name"],
            url=f"{self.url}/{self.ws}/projects/{self.pid}/issues/{r['id']}",
            created=r.get("created_at"), updated=r.get("updated_at"),
            closed=(r.get("completed_at") or r.get("updated_at")) if group in ("completed", "cancelled") else None,
            cancelled=group == "cancelled", points=estimate(r),
            assignees=[a.get("display_name") or a.get("email") for a in r.get("assignees") or [] if isinstance(a, dict)],
            period=period, initial=period)


# ------------------------------------------------------------------ math
def period_at(it, when):
    """The period the issue was in at `when` (None before it existed)."""
    created = parse_dt(it["created"])
    if created and when < created:
        return None
    if it["events"] is None:
        return it["period"]
    current = it["initial"]
    for t, p in it["events"]:
        if t > when:
            break
        current = p
    return current


def ever_in(it, name, after, until):
    """True when the issue joined period `name` in (after, until]."""
    if it["events"] is None:
        created = parse_dt(it["created"])
        return it["period"] == name and created is not None and after < created <= until
    return any(p == name and after < t <= until for t, p in it["events"])


def choose_unit(issues, unit):
    if unit != "auto":
        return unit
    return "points" if issues and all(i["points"] is not None for i in issues) else "count"


def size(it, unit):
    return (it["points"] or 0) if unit == "points" else 1


def closed_by(it, until):
    return it["closed"] is not None and parse_dt(it["closed"]) <= until


def delivered(it, name, until):
    """Closed (not cancelled) by `until` while still in the period."""
    if it["cancelled"] or not closed_by(it, until):
        return False
    return period_at(it, parse_dt(it["closed"])) == name


def classify(p, issues, grace, today, zone):
    start, end = to_date(p["start"]), to_date(p["end"])
    cutoff = end_of_day(start + dt.timedelta(days=grace), zone)
    stop = end_of_day(min(end, today), zone)
    name = p["name"]
    committed = [i for i in issues if period_at(i, cutoff) == name]
    cids = {id(i) for i in committed}
    # Added work must still be in the period at the end, or have been delivered in it: an issue that came
    # and went again inside the period changed nothing and would inflate the scope change.
    added = [i for i in issues if id(i) not in cids and ever_in(i, name, cutoff, stop)
             and (period_at(i, stop) == name or delivered(i, name, stop))]
    rows = {"committed": committed, "added": added, "delivered": [], "removed": [], "cancelled": [],
            "carried": [], "added_done": [i for i in added if delivered(i, name, stop)]}
    for i in committed:
        if delivered(i, name, stop):
            rows["delivered"].append(i)
        elif i["cancelled"] and closed_by(i, stop):
            rows["cancelled"].append(i)
        elif period_at(i, stop) != name:
            rows["removed"].append(i)
        else:
            rows["carried"].append(i)
    return rows


def period_row(p, groups, unit):
    def sz(key):
        return sum(size(i, unit) for i in groups[key])
    commit, done, added_done = sz("committed"), sz("delivered"), sz("added_done")
    per_member = {}
    for key, col in (("committed", 0), ("delivered", 1)):
        for i in groups[key]:
            share = size(i, unit) / max(1, len(i["assignees"]))
            for a in i["assignees"] or ["(unassigned)"]:
                per_member.setdefault(a, [0.0, 0.0])[col] += share
    return {"name": p["name"], "start": p["start"], "end": p["end"], "status": p["status"],
            "committed": commit, "committed_issues": len(groups["committed"]), "delivered": done,
            "say_do": done / commit if commit else None, "added": sz("added"), "added_issues": len(groups["added"]),
            "added_delivered": added_done, "removed": sz("removed"), "cancelled": sz("cancelled"),
            "carried": sz("carried"), "total_delivered": done + added_done,
            "vs_commitment": (done + added_done) / commit if commit else None,
            "scope_change": (sz("added") - sz("removed")) / commit if commit else None,
            "per_member": per_member,
            "carried_items": [{"key": i["key"], "title": i["title"], "url": i["url"], "size": size(i, unit)}
                              for i in groups["carried"]],
            # separate lists, so a reader of the JSON cannot take a cancelled issue for a removed one
            "removed_items": [{"key": i["key"], "title": i["title"], "url": i["url"], "size": size(i, unit)}
                              for i in groups["removed"]],
            "cancelled_items": [{"key": i["key"], "title": i["title"], "url": i["url"], "size": size(i, unit)}
                                for i in groups["cancelled"]]}


def summarize(rows):
    # Periods without a commitment (only added work) have no say/do and stay out of the statistics.
    rows = [r for r in rows if r["say_do"] is not None]
    sd = [r["say_do"] for r in rows]
    if not sd:
        return None
    vs = [r["vs_commitment"] for r in rows if r["vs_commitment"] is not None]
    sc = [r["scope_change"] for r in rows if r["scope_change"] is not None]
    total_c = sum(r["committed"] for r in rows)
    med = statistics.median(sd)
    last3 = statistics.mean(sd[-3:])
    tr = slope(sd)
    # The median alone hides a recent collapse, so rate on the lower of the median and the last-3 average,
    # and drop one more level when the latest period is below 70% and far (> 20 pp) under the median.
    basis = min(med, last3)
    level = 0 if basis >= 0.9 else 1 if basis >= 0.7 else 2
    latest = sd[-1]
    drop = len(sd) > 1 and latest < 0.7 and latest < med - 0.2
    if drop:
        level = min(2, level + 1)
    return {"n": len(sd), "mean": statistics.mean(sd), "median": med, "min": min(sd), "max": max(sd),
            "last3": last3, "latest": latest, "latest_name": rows[-1]["name"], "trend": tr,
            "trend_word": "not enough periods" if tr is None else "rising" if tr > 0.05 else
                          "falling" if tr < -0.05 else "flat",
            "pooled": sum(r["delivered"] for r in rows) / total_c if total_c else None,
            "vs_commitment": statistics.median(vs) if vs else None,
            "scope_change": statistics.median(sc) if sc else None,
            "median_delivered": statistics.median([r["delivered"] for r in rows]),
            "rating_basis": basis, "recent_drop": drop,
            "reliability": ("reliable", "fairly reliable", "overcommits")[level]}


# ------------------------------------------------------------------ output
def f1(x, pct=False):
    if x is None:
        return "—"
    return f"{100 * x:.0f}%" if pct else (f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}")


def render(res):
    u, kind = res["unit"], res["kind"]
    out = [f"# Commitment x delivered — {res['project']}", "",
           f"Unit: **{u}** · commitment cutoff: end of the start day"
           + (f" + {res['grace_days']} day(s)" if res["grace_days"] else "") + f" · days in {res['timezone']} · today {res['today']}", ""]
    rows = res["rows"]
    if not rows:
        out.append(f"No {kind} with dates and a commitment to analyze.")
        return "\n".join(out + [f"- {n}" for n in res["notes"]])
    out.append(md_table(
        [kind.capitalize(), "Dates", "Status", "Committed", "Delivered", "Say/do", "Removed", "Cancelled",
         "Carried over", "Added", "Added delivered", "Total delivered", "vs commitment", "Scope change"],
        [[r["name"], f"{r['start']} → {r['end']}", r["status"], f"{f1(r['committed'])} ({r['committed_issues']})",
          f1(r["delivered"]), f"**{f1(r['say_do'], True)}**", f1(r["removed"]), f1(r["cancelled"]), f1(r["carried"]),
          f"{f1(r['added'])} ({r['added_issues']})", f1(r["added_delivered"]), f1(r["total_delivered"]),
          f1(r["vs_commitment"], True), "—" if r["scope_change"] is None else f"{100 * r['scope_change']:+.0f}%"]
         for r in rows]))
    s = res["summary"]
    if s:
        out += ["", "## Summary (finished periods)", "",
                md_table(["Periods", "Mean say/do", "Median", "Min", "Max", "Last 3 avg", "Latest", "Pooled",
                          "Trend / period", "Median vs commitment", "Median scope change"],
                         [[s["n"], f1(s["mean"], True), f1(s["median"], True), f1(s["min"], True), f1(s["max"], True),
                           f1(s["last3"], True), f1(s["latest"], True), f1(s["pooled"], True),
                           "—" if s["trend"] is None else f"{100 * s['trend']:+.0f} pp",
                           f1(s["vs_commitment"], True),
                           "—" if s["scope_change"] is None else f"{100 * s['scope_change']:+.0f}%"]]), "",
                f"Reliability: **{s['reliability']}** (rated on {f1(s['rating_basis'], True)}, the lower of the median "
                f"and the last-3 average) · trend: **{s['trend_word']}**"]
        if s["recent_drop"]:
            out += ["", f"**Recent drop:** {s['latest_name']} kept only {f1(s['latest'], True)} of its commitment, "
                        f"{100 * (s['median'] - s['latest']):.0f} pp under the median of {f1(s['median'], True)}. "
                        "The rating was lowered one level."]
    if res["members"]:
        fin = [r for r in rows if r["status"] == "finished"]
        out += ["", "## Per person (finished periods; shared evenly between assignees)", "",
                md_table(["Person", "Committed", "Delivered", "Say/do"],
                         [[("@" + m) if not m.startswith("(") else m, f1(c), f1(d), f1(d / c if c else None, True)]
                          for m, (c, d) in res["members"]])] if fin else []
    for r in rows:
        if r["carried_items"] or r["removed_items"] or r["cancelled_items"]:
            out += ["", f"### {r['name']} — not delivered from the commitment", ""]
            open_word = "still open" if r["status"] == "current" else "carried over"
            out += [f"- {open_word}: [{i['key']}]({i['url']}) {i['title']} ({f1(i['size'])})" for i in r["carried_items"]]
            out += [f"- removed: [{i['key']}]({i['url']}) {i['title']} ({f1(i['size'])})"
                    for i in r["removed_items"]]
            out += [f"- cancelled: [{i['key']}]({i['url']}) {i['title']} ({f1(i['size'])})"
                    for i in r["cancelled_items"]]
    nxt = res.get("next")
    if nxt:
        out += ["", "## Next period", "",
                (f"**{nxt['name']}** holds nothing yet. " if not nxt["scope"] else
                 f"**{nxt['name']}** holds {f1(nxt['scope'])} {u} today. ") + f"The team delivered a median of "
                f"{f1(nxt['median_delivered'])} {u} of its commitment per period"
                + (f" — committing {f1(nxt['scope'])} is **{f1(nxt['scope'] / nxt['median_delivered'], True)}** of that."
                   if nxt["median_delivered"] and nxt["scope"] else ".")]
    out += [""] + [f"- {n}" for n in res["notes"]]
    return "\n".join(out)


def render_periods(res):
    rows = [[n, p["name"], p["start"] or "—", p["end"] or "—", p["status"]] for n, p in enumerate(res["periods"], 1)]
    return "\n".join([f"# {res['kind'].capitalize()}s — {res['project']}", "",
                      md_table(["#", res["kind"].capitalize(), "Start", "End", "Status"], rows)])


# ------------------------------------------------------------------ main
def mark_status(periods, today):
    periods = [p for p in periods if p["start"] and p["end"]]
    for p in periods:
        s, e = to_date(p["start"]), to_date(p["end"])
        p["status"] = "finished" if p["closed"] or e < today else ("current" if s <= today <= e else "upcoming")
    return sorted(periods, key=lambda p: p["start"])


def select(periods, a):
    """The periods to analyze: --period names, or the last --last finished ones plus the current one."""
    notes = []
    if a.period:
        wanted = {n.strip().lower() for n in a.period}
        chosen = [p for p in periods if p["name"].lower() in wanted]
        unknown = wanted - {p["name"].lower() for p in chosen}
        if unknown:
            notes.append(f"Not found (or without dates): {', '.join(sorted(unknown))}.")
    else:
        chosen = [p for p in periods if p["status"] == "finished"][-a.last:]
        chosen += [p for p in periods if p["status"] == "current"]
    return [p for p in chosen if p["status"] != "upcoming"], notes


def analyze(src_name, kind, periods, issues, a, today, zone):
    """Pure calculation over loaded periods and issues (no API calls)."""
    chosen, notes = select(periods, a)

    groups = [(p, classify(p, issues, a.grace_days, today, zone)) for p in chosen]
    involved = [i for _, g in groups for i in g["committed"] + g["added"] if not i["cancelled"]]
    unit = choose_unit(involved, a.unit)
    if a.unit == "auto" and unit == "count" and involved:
        where = "an Estimate" if kind == "cycle" else "a `points::N` label"
        notes.append(f"{sum(1 for i in involved if i['points'] is None)} issue(s) have no points ({where}), "
                     "so every issue counts as 1. Use --unit points to count only pointed work.")
    rows = [period_row(p, g, unit) for p, g in groups if g["committed"] or g["added"]]
    empty = [p["name"] for p, g in groups if not g["committed"]]
    if empty:
        notes.append(f"No commitment at the start of: {', '.join(empty)} (every issue was added later).")
    finished = [r for r in rows if r["status"] == "finished"]
    summary = summarize(finished)
    if summary and summary["n"] < 3:
        notes.append(f"Only {summary['n']} finished period(s): treat the averages as a rough guide.")
    if any(r["status"] == "current" for r in rows):
        notes.append("The current period is still running: its say/do only counts work closed so far.")
    if not any(i["events"] for i in issues):
        notes.append("No milestone/cycle history was found, so every issue counts as in its current period "
                     "since it was created.")
    notes.append("Points are read as they are today, not as they were at the start.")

    members = {}
    for r in finished:
        for m, (c, d) in r["per_member"].items():
            members.setdefault(m, [0.0, 0.0])
            members[m][0] += c
            members[m][1] += d
    res = {"project": src_name, "kind": kind, "unit": unit, "grace_days": a.grace_days, "today": today.isoformat(),
           "timezone": str(zone),
           "rows": rows, "summary": summary, "notes": notes,
           "members": sorted(members.items(), key=lambda kv: -kv[1][0])}
    upcoming = [p for p in periods if p["status"] == "upcoming"]
    if summary and upcoming and not a.period:
        p = upcoming[0]
        scope = sum(size(i, unit) for i in issues if i["period"] == p["name"] and not i["cancelled"])
        res["next"] = {"name": p["name"], "scope": scope, "median_delivered": summary["median_delivered"]}
    return res


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "report"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project")
    ap.add_argument("--last", type=int, default=6, help="finished periods to analyze (default 6)")
    ap.add_argument("--period", action="append", help="analyze this milestone/cycle (repeatable)")
    ap.add_argument("--unit", choices=["auto", "points", "count"], default="auto")
    ap.add_argument("--grace-days", type=int, default=0,
                    help="days after the start day that still count as commitment (default 0)")
    ap.add_argument("--timezone", type=timezone_arg, help="IANA name or +HH:MM: where a day begins and ends")
    ap.add_argument("--today")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=int, default=60)
    a = ap.parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace)

    chosen_today = dt.date.fromisoformat(a.today) if a.today else None
    if a.command != "projects" and not a.project:
        sys.exit(f"{a.command} needs --project (run `projects` to list yours)")
    try:
        src = (GitLab(a.gitlab_url, os.environ["GITLAB_TOKEN"], (a.project or "").strip("/") or None)
               if a.target == "gitlab" else
               Plane(a.plane_url, a.workspace, os.environ["PLANE_API_KEY"], a.project, a.rate))
        if a.command == "projects":
            rows = src.my_projects()
            if a.format == "json":
                return print(json.dumps(rows, indent=2))
            return print("\n".join([f"# Your {'GitLab' if a.target == 'gitlab' else 'Plane'} projects", "",
                                    md_table(["#", "Project", "Name"],
                                             [[n, f"[{r['path']}]({r['url']})", r["name"]] for n, r in enumerate(rows, 1)])]))
        src.set_days(a.timezone, chosen_today)
        periods = src.periods()
        today = src.today()
        periods = mark_status(periods, today)
        if a.command == "periods":
            src.warn_about_days()
            res = {"project": src.name, "kind": src.kind, "periods": periods}
            return print(json.dumps(res, indent=2) if a.format == "json" else render_periods(res))
        starts = [to_date(p["start"]) for p in select(periods, a)[0]]
        # A day earlier: the instance's offset is only fully known after the issues are read
        since = (start_of_day(min(starts), src.tz) - dt.timedelta(days=1) if starts
                 else end_of_day(today, src.tz))
        issues = src.issues(since)
    except ApiError as e:
        sys.exit(f"API error: {e}")
    res = analyze(src.name, src.kind, periods, issues, a, today, src.tz)
    if src.tz_note:
        res["notes"].append(src.tz_note)
    print(json.dumps(res, indent=2, default=str) if a.format == "json" else render(res))
