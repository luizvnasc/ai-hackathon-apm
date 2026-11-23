"""Scope stability: how much a GitLab milestone or Plane cycle changed while it was running.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Read-only: nothing is written to GitLab or Plane.

    stability projects --target gitlab|plane
    stability periods  --target ... --project P
    stability report   --target ... --project P [--last 6] [--period NAME ...]
                                  [--unit auto|points|count] [--grace-days 0] [--timezone TZ]
                                  [--today YYYY-MM-DD] [--format md|json]

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api). Plane uses
PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY. Unset variables are read from a
.env file in the current directory; the environment wins.

Definitions (delivery plays no part):
  * Initial scope: what was in the period at the end of its start day (plus
    --grace-days), sized as it was then. Earlier changes are planning.
  * Changed: added + removed + cancelled + |re-estimated|, from the day after
    the cutoff to the last day. Every movement counts: in and out is two.
  * Stability = max(0, 1 - changed / initial). >= 90% stable, >= 75% moderate,
    below unstable. No percentage when the initial scope is empty.
  * Rollover: an open issue moved to a later period on the last day or after.
    Listed, never counted.
  * Cancelled counts once; removing that issue afterwards adds nothing.
  * Re-estimated: the difference between two values. A first estimate is not a
    change. When counting issues it is listed but not summed.
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
from .dates import UTC, day_of, parse_dt
from .days import timezone_arg
from .gitlab import GitLabClient, with_full_path
from .labels import has_cancelled_label, label_points, number
from .net import ApiError
from .plane import PlaneClient, estimate
from .report import md_table
from .stats import slope

UA = "scope-stability-skill/1.0"
STABLE, MODERATE = 0.9, 0.75
PAIR_SECONDS = 5  # a replacement is logged as two events at (almost) the same instant
MAX_LISTED = 50  # changes per period in markdown; the JSON has them all
UNKNOWN = "?"  # a milestone/cycle that no longer exists


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA, **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "null"), resp.headers
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"), method, url) from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(0, f"cannot reach the server: {getattr(e, 'reason', e)}", method, url) from None
    except ValueError:
        raise ApiError(0, "the response is not JSON", method, url) from None


def fetch(method, url, headers, body=None):
    """http() with bounded retries. Every request of this script is a read, GraphQL included."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return http(method, url, headers, body)
        except ApiError as e:
            if e.status == 429 and attempt < 6:
                time.sleep(10)
            elif e.status >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
            else:
                raise


# ------------------------------------------------------------------ values
def new_issue(**kw):
    """`initial` is the period (id) the issue was created in and `moves` its history [(when, period id or None)];
    `points_initial` and `estimates` do the same for its size."""
    it = {"key": None, "title": "", "url": None, "created": None, "closed": None, "period": None, "initial": None,
          "moves": [], "points": None, "points_initial": None, "estimates": [], "cancelled": None,
          "approx_cancel": False, "missing_history": False, "inconsistent": False}
    it.update(kw)
    return it


def check_final(it):
    final = it["moves"][-1][1] if it["moves"] else it["initial"]
    it["inconsistent"] = final != it["period"] and final != UNKNOWN


def replay(events):
    """(initial, changes) of a single-valued field from (when, "add"|"remove", value), oldest first.
    An add and a remove of two different values within PAIR_SECONDS are one replacement."""
    steps, i = [], 0
    while i < len(events):
        when, action, value = events[i]
        if i + 1 < len(events):
            when2, action2, value2 = events[i + 1]
            if action2 != action and value2 != value and (when2 - when).total_seconds() <= PAIR_SECONDS:
                added, removed = (value, value2) if action == "add" else (value2, value)
                steps.append((when, removed, added))
                i += 2
                continue
        steps.append((when, None, value) if action == "add" else (when, value, None))
        i += 1
    if not steps:
        return None, []
    initial = current = steps[0][1]
    changes = []
    for when, old, new in steps:
        if new is None and old != current:
            continue  # removing a value it no longer has
        if new != current:
            changes.append((when, new))
            current = new
    return initial, changes


# ------------------------------------------------------------------ GitLab
class GitLab(GitLabClient):
    def send(self, method, url, body=None):
        return fetch(method, url, self.headers, body)

    def periods(self):
        q = """query($p: ID!, $after: String) { project(fullPath: $p) { name lastActivityAt
                 milestones(includeAncestors: true, first: 100, after: $after) {
                   pageInfo { hasNextPage endCursor } nodes { id title state startDate dueDate } } } }"""
        self._periods, after = [], None
        while True:
            proj = self.graphql(q, {"p": self.project, "after": after})["project"]
            if not proj:
                sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
            self.name = proj["name"]
            self.learn_times(proj.get("lastActivityAt"))
            page = proj["milestones"]
            for m in page["nodes"]:
                self._periods.append({"id": str(m["id"]).rsplit("/", 1)[-1] if m.get("id") else m["title"],
                                      "name": m["title"], "start": m["startDate"], "end": m["dueDate"],
                                      "closed": m["state"] == "closed"})
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return [dict(p) for p in self._periods]
            after = info["endCursor"]

    def key(self, milestone):
        """The period id of a milestone reference: by id, so a rename keeps the history, else by title."""
        if not milestone:
            return UNKNOWN
        ident = str(milestone.get("id") or "").rsplit("/", 1)[-1]
        if any(p["id"] == ident for p in self._periods):
            return ident
        title = milestone.get("title")
        return next((p["id"] for p in self._periods if p["name"] == title), ident or title or UNKNOWN)

    def moves(self, events):
        rows = [(parse_dt(e["created_at"]), e["action"], self.key(e.get("milestone"))) for e in events]
        return replay(sorted(rows, key=lambda r: r[0]))

    @staticmethod
    def estimates(events):
        rows = []
        for e in events:
            value = label_points([(e.get("label") or {}).get("name") or ""])
            if value is not None:
                rows.append((parse_dt(e["created_at"]), e["action"], value))
        return replay(sorted(rows, key=lambda r: r[0]))

    def issues(self, since):
        q = """query($p: ID!, $after: String) { project(fullPath: $p) {
                 workItems(types: [ISSUE, TASK, INCIDENT], first: 100, after: $after) {
                   pageInfo { hasNextPage endCursor }
                   nodes { iid title state webUrl createdAt updatedAt closedAt widgets { type
                     ... on WorkItemWidgetLabels { labels { nodes { title } } }
                     ... on WorkItemWidgetMilestone { milestone { id title } } } } } } }"""
        nodes, after = [], None
        while True:
            page = self.graphql(q, {"p": self.project, "after": after})["project"]["workItems"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        base = f"/projects/{urllib.parse.quote(self.project, safe='')}/issues"
        out = []
        for n in nodes:
            self.learn_times(n.get("createdAt"), n.get("updatedAt"), n.get("closedAt"))
            w = {x.get("type"): x for x in n.get("widgets") or []}
            labels = [l["title"] for l in ((w.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
            milestone = (w.get("MILESTONE") or {}).get("milestone")
            current = self.key(milestone) if milestone else None
            closed = parse_dt(n.get("closedAt")) if n["state"] != "OPEN" else None
            is_cancelled = has_cancelled_label(labels)
            it = new_issue(key=f"#{n['iid']}", title=n["title"], url=n["webUrl"], created=parse_dt(n["createdAt"]),
                           closed=closed, period=current, initial=current, points=label_points(labels),
                           points_initial=label_points(labels), cancelled=closed if is_cancelled else None)
            # Not touched since the first analyzed period began: it kept its milestone and size all along.
            if parse_dt(n["updatedAt"]) >= since:
                path = f"{base}/{n['iid']}"
                events = self.rest_pages(path + "/resource_milestone_events")
                if events:
                    it["initial"], it["moves"] = self.moves(events)
                    check_final(it)
                else:
                    it["missing_history"] = bool(current)
                first, changes = self.estimates(self.rest_pages(path + "/resource_label_events"))
                if changes or first is not None:
                    it["points_initial"], it["estimates"] = first, changes
            out.append(it)
        return out


# ------------------------------------------------------------------ Plane
class Plane(PlaneClient):
    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        super().__init__(url, workspace, api_key, rate_per_min)
        self.select_project(project)

    def send(self, method, url, body=None):
        return fetch(method, url, self.headers, body)

    def request(self, path, params=None):
        # Until the copies agree: here the retries are in fetch(), which does not pace each attempt
        self.pace()
        return self.send("GET", self.api_url(path, params))[0]

    def periods(self):
        self._periods = [{"id": c["id"], "name": c["name"], "start": self.local_day(c.get("start_date")),
                          "end": self.local_day(c.get("end_date")),
                          "closed": (c.get("status") or "").lower() == "completed"}
                         for c in self.list(f"/projects/{self.pid}/cycles/") if not c.get("archived_at")]
        return [dict(p) for p in self._periods]

    def key(self, ident, name):
        if ident:
            return str(ident)
        if name:
            return next((p["id"] for p in self._periods if p["name"] == name), UNKNOWN)
        return None

    def apply_history(self, it, activities, cancelled_states):
        acts = sorted(activities, key=lambda a: parse_dt(a["created_at"]))
        cycles = [a for a in acts if a.get("field") in ("cycles", "cycle")]
        if cycles:
            it["initial"] = self.key(cycles[0].get("old_identifier"), cycles[0].get("old_value"))
            it["moves"] = [(parse_dt(a["created_at"]), None if a.get("verb") == "deleted"
                            else self.key(a.get("new_identifier"), a.get("new_value"))) for a in cycles]
            check_final(it)
        else:
            it["missing_history"] = bool(it["period"])
        sizes = [a for a in acts if (a.get("field") or "").startswith("estimate_")]
        if sizes:
            it["points_initial"] = number(sizes[0].get("old_value"))
            it["estimates"] = [(parse_dt(a["created_at"]), number(a.get("new_value"))) for a in sizes]
        if it["cancelled"]:
            into = [a for a in acts if a.get("field") == "state" and str(a.get("new_identifier")) in cancelled_states]
            it["approx_cancel"] = not into
            if into:
                it["cancelled"] = parse_dt(into[-1]["created_at"])

    def issues(self, since):
        current = {}
        for c in self._periods:
            for r in self.list(f"/projects/{self.pid}/cycles/{c['id']}/cycle-issues/"):
                current[r.get("id") or r.get("issue")] = c["id"]
        states = {str(s["id"]) for s in self.list(f"/projects/{self.pid}/states/") if s.get("group") == "cancelled"}
        out = []
        for r in self.list(f"/projects/{self.pid}/issues/", {"expand": "state,estimate_point"}):
            if r.get("is_draft"):
                continue
            group = (r.get("state") if isinstance(r.get("state"), dict) else {}).get("group")
            updated = parse_dt(r.get("updated_at"))
            # Plane sets completed_at only for completed items: a cancellation is dated by its state activity.
            closed = (parse_dt(r.get("completed_at")) or updated) if group in ("completed", "cancelled") else None
            period = current.get(r["id"])
            it = new_issue(key=f"{self.ident}-{r['sequence_id']}", title=r["name"],
                           url=f"{self.url}/{self.ws}/projects/{self.pid}/issues/{r['id']}",
                           created=parse_dt(r.get("created_at")), closed=closed, period=period, initial=period,
                           points=estimate(r), points_initial=estimate(r),
                           cancelled=closed if group == "cancelled" else None)
            if updated is None or updated >= since:
                self.apply_history(it, self.list(f"/projects/{self.pid}/issues/{r['id']}/activities/"), states)
            out.append(it)
        return out


# ------------------------------------------------------------------ math
def known_sizes(it):
    return [v for v in [it["points_initial"]] + [v for _, v in it["estimates"]] if v is not None]


def rating(value):
    if value is None:
        return None
    eps = 1e-9  # 1 - 3.4/34 must still be 90%
    return "stable" if value >= STABLE - eps else "moderate" if value >= MODERATE - eps else "unstable"


def walk(it, p, starts, cutoff, stop, today, tz):
    """One issue through one period: (size in the initial scope or None, changes, rollovers)."""
    born = min([t for t in [it["created"]] if t] + [t for t, _ in it["moves"]], default=None)
    timeline = [(born or dt.datetime.min.replace(tzinfo=UTC), 0, None)]
    timeline += [(t, 1, v) for t, v in it["estimates"]]
    timeline += [(t, 2, to) for t, to in it["moves"]]
    if it["cancelled"]:
        timeline.append((it["cancelled"], 3, None))
    timeline.sort(key=lambda e: (e[0], e[1]))  # at the same instant: size first, then move

    sizes = known_sizes(it)
    size = sizes[0] if sizes else 0.0  # the first estimate also sizes the time before it
    start, end = dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"])
    where, alive, initial, changes, rollover = None, False, None, [], []

    def record(kind, day, amount, **extra):
        return {"type": kind, "date": day.isoformat(), "key": it["key"], "title": it["title"], "url": it["url"],
                "size": amount, **extra}

    snapshot = False
    for when, what, value in timeline:
        # a date-less birth sorts first and has no real day
        day = start if when.year == 1 else day_of(when, tz)
        if not snapshot and day > cutoff:
            snapshot = True
            initial = size if alive and where == p["id"] else None
        counts = cutoff < day <= stop
        inside = alive and where == p["id"]
        if what == 0:
            alive, where = True, it["initial"]
            if counts and where == p["id"]:
                changes.append(record("added", day, size))
        elif what == 1:
            if value is None:
                continue
            if inside and counts and value != size:
                changes.append(record("re-estimated", day, abs(value - size), before=size, after=value))
            size = value
        elif what == 2:
            if value == where:
                continue
            if inside:
                later = value in starts and starts[value] > start
                still_open = not it["closed"] or it["closed"] > when
                if cutoff < day <= today and day >= end and later and still_open:
                    rollover.append(record("rollover", day, size))
                elif counts:
                    changes.append(record("removed", day, size))
            elif alive and counts and value == p["id"]:
                changes.append(record("added", day, size))
            where = value
        else:
            if inside and counts:
                changes.append(record("cancelled", day, size))
            alive = False  # cancelled work is out of scope: moving it later changes nothing
    if not snapshot:
        initial = size if alive and where == p["id"] else None
    return initial, changes, rollover


def measure(p, issues, starts, grace, today, tz):
    start, end = dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"])
    cutoff, stop = start + dt.timedelta(days=grace), min(end, today)
    out = {"period": p, "initial": [], "changes": [], "rollover": [], "involved": []}
    for it in issues:
        initial, changes, rollover = walk(it, p, starts, cutoff, stop, today, tz)
        if initial is not None:
            out["initial"].append(initial)
        out["changes"] += changes
        out["rollover"] += rollover
        if initial is not None or changes:
            out["involved"].append(it)
    out["changes"].sort(key=lambda c: c["date"])
    out["rollover"].sort(key=lambda c: c["date"])
    return out


def period_row(m, unit):
    p, points = m["period"], unit == "points"

    def weight(c):
        if points:
            return c["size"]
        return 0 if c["type"] == "re-estimated" else 1

    def total(kind):
        return sum(weight(c) for c in m["changes"] if c["type"] == kind)

    initial = sum(m["initial"]) if points else len(m["initial"])
    added, removed, cancelled, reest = total("added"), total("removed"), total("cancelled"), total("re-estimated")
    changed = added + removed + cancelled + reest
    signed = sum(c["after"] - c["before"] for c in m["changes"] if c["type"] == "re-estimated") if points else 0
    start, end = dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"])
    days, thirds = max(1, (end - start).days + 1), [0, 0, 0]
    for c in m["changes"]:
        thirds[min(2, (dt.date.fromisoformat(c["date"]) - start).days * 3 // days)] += weight(c)
    rate = changed / initial if initial else None
    stability = None if rate is None else max(0.0, 1 - rate)
    return {"id": p["id"], "name": p["name"], "start": p["start"], "end": p["end"], "status": p["status"],
            "initial": initial, "initial_issues": len(m["initial"]), "added": added, "removed": removed,
            "cancelled": cancelled, "reestimated": reest, "changed": changed,
            "final": initial + added - removed - cancelled + signed, "change_rate": rate, "stability": stability,
            "rating": rating(stability), "thirds": thirds, "changes": m["changes"], "rollover": m["rollover"],
            "rolled_over": sum(weight(c) for c in m["rollover"])}


def summarize(rows):
    rows = [r for r in rows if r["status"] == "finished" and r["stability"] is not None]
    values = [r["stability"] for r in rows]
    if not values:
        return None
    trend, median = slope(values), statistics.median(values)
    word = ("not enough periods" if trend is None else "getting more stable" if trend > 0.05 else
            "getting less stable" if trend < -0.05 else "flat")
    return {"n": len(values), "median": median, "mean": statistics.mean(values), "min": min(values),
            "max": max(values), "rating": rating(median), "latest": values[-1], "latest_name": rows[-1]["name"],
            "latest_rating": rating(values[-1]), "trend": trend, "trend_word": word}


def mark_status(periods, today):
    periods = [p for p in periods if p["start"] and p["end"]]
    for p in periods:
        start, end = dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"])
        p["status"] = "finished" if p["closed"] or end < today else "current" if start <= today else "upcoming"
    return sorted(periods, key=lambda p: (p["start"], p["end"]))


def select(periods, a):
    """(periods to analyze, notes): the named ones, or the last finished ones plus the current one."""
    if a.period:
        wanted = {n.strip().lower() for n in a.period}
        chosen = [p for p in periods if p["name"].strip().lower() in wanted]
        unknown = wanted - {p["name"].strip().lower() for p in chosen}
        if unknown:
            names = ", ".join(p["name"] for p in periods) or "none with dates"
            sys.exit(f"not found (or without start and end dates): {', '.join(sorted(unknown))}. Available: {names}")
    else:
        chosen = [p for p in periods if p["status"] == "finished"][-a.last:]
        chosen += [p for p in periods if p["status"] == "current"]
        chosen.sort(key=lambda p: (p["start"], p["end"]))
    notes = [f"{p['name']} has not started: there is no change to measure yet."
             for p in chosen if p["status"] == "upcoming"]
    return [p for p in chosen if p["status"] != "upcoming"], notes


def analyze(name, kind, periods, issues, a, today, tz):
    """Pure calculation over loaded periods and issues (no API calls)."""
    chosen, notes = select(periods, a)
    starts = {p["id"]: dt.date.fromisoformat(p["start"]) for p in periods}
    measured = [measure(p, issues, starts, a.grace_days, today, tz) for p in chosen]
    involved = list({id(i): i for m in measured for i in m["involved"]}.values())
    unpointed = [i for i in involved if not known_sizes(i)]
    unit = "issues" if a.unit == "count" or (a.unit == "auto" and (unpointed or not involved)) else "points"
    if unpointed and a.unit == "auto":
        where = "an Estimate" if kind == "cycle" else "a `points::N` label"
        notes.append(f"{len(unpointed)} issue(s) have no points ({where}), so every issue counts as 1 and "
                     "re-estimates are listed but not summed.")
    elif unpointed and unit == "points":
        notes.append(f"{len(unpointed)} issue(s) have no points and count as 0.")
    rows = [period_row(m, unit) for m in measured]
    empty = [r["name"] for r in rows if not r["initial"]]
    if empty:
        notes.append(f"Empty initial scope, so no percentage: {', '.join(empty)}.")
    summary = summarize(rows)
    if summary and summary["n"] < 3:
        notes.append(f"Only {summary['n']} finished period(s): treat the median and trend as a rough guide.")
    if any(r["status"] == "current" for r in rows):
        notes.append("The current period is still running: its stability is partial and can only stay or fall.")
    missing = sum(1 for i in involved if i["missing_history"])
    if missing:
        notes.append(f"{missing} of {len(involved)} issue(s) have no {kind} history: they count as in their current "
                     f"{kind} since they were created. Removals and moves of these issues cannot be seen, so the "
                     "result is approximate.")
    odd = sum(1 for i in involved if i["inconsistent"])
    if odd:
        notes.append(f"{odd} issue(s) have a history that does not end in their current {kind}: some changes were "
                     "not recorded, so the result is approximate.")
    if any(i["approx_cancel"] for i in involved):
        notes.append("Some cancellations have no state history and are dated by the issue's last update.")
    if rows:
        notes.append("Deleted or archived issues, and cancellations that were later undone, are not seen.")
    return {"project": name, "kind": kind, "unit": unit, "grace_days": a.grace_days, "today": today.isoformat(),
            "timezone": str(tz), "rows": rows, "summary": summary, "notes": notes}


# ------------------------------------------------------------------ output
def f1(x):
    return "—" if x is None else f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def pct(x):
    if x is None:
        return "—"
    shown = round(100 * x + 1e-9)
    if rating(shown / 100) != rating(x):
        shown = int(100 * x)  # 89.6% must not show as 90% next to "moderate"
    return f"{shown}%"


def cell(text):
    return re.sub(r"[\r\n]+", " ", str(text)).replace("|", "\\|")


def render(res):
    unit, kind = res["unit"], res["kind"]
    out = [f"# Scope stability — {cell(res['project'])}", "",
           f"Unit: **{unit}** · initial scope: end of the start day"
           + (f" + {res['grace_days']} day(s)" if res["grace_days"] else "")
           + f" · days in {res['timezone']} · today {res['today']}", ""]
    rows = res["rows"]
    if not rows:
        out.append(f"No started {kind} with start and end dates to analyze.")
        return "\n".join(out + [f"- {n}" for n in res["notes"]])
    out.append(md_table(
        [kind.capitalize(), "Dates", "Status", "Initial", "Added", "Removed", "Cancelled", "Re-estimated", "Changed",
         "Final", "Change", "Stability", "Rating"],
        [[cell(r["name"]), f"{r['start']} → {r['end']}", "current (partial)" if r["status"] == "current" else r["status"],
          f"{f1(r['initial'])} ({r['initial_issues']})", f1(r["added"]), f1(r["removed"]), f1(r["cancelled"]),
          f1(r["reestimated"]), f1(r["changed"]), f1(r["final"]),
          "—" if r["change_rate"] is None else f"{100 * r['change_rate']:.0f}%",
          f"**{pct(r['stability'])}**", r["rating"] or "—"] for r in rows]))
    s = res["summary"]
    if s:
        out += ["", "## History (finished periods)", "",
                md_table(["Periods", "Median", "Mean", "Min", "Max", "Latest", "Trend / period"],
                         [[s["n"], pct(s["median"]), pct(s["mean"]), pct(s["min"]), pct(s["max"]),
                           f"{pct(s['latest'])} ({s['latest_rating']})",
                           "—" if s["trend"] is None else f"{100 * s['trend']:+.0f} pp"]]), "",
                f"Typical: **{s['rating']}** (median) · trend: **{s['trend_word']}**"]
    moved = [r for r in rows if r["changed"]]
    if moved:
        out += ["", f"## When it changed ({unit}, by third of the {kind})", "",
                md_table([kind.capitalize(), "First third", "Middle", "Last third"],
                         [[cell(r["name"])] + [f1(v) for v in r["thirds"]] for r in moved])]
    for r in rows:
        if r["changes"]:
            shown = r["changes"][:MAX_LISTED]
            out += ["", f"## Changes — {cell(r['name'])}", "",
                    md_table(["Date", "Change", "Issue", "Points"],
                             [[c["date"], c["type"], f"[{c['key']}]({c['url']}) {cell(c['title'])}",
                               f"{f1(c['before'])} → {f1(c['after'])}" if c["type"] == "re-estimated" else f1(c["size"])]
                              for c in shown])]
            if len(r["changes"]) > len(shown):
                out += ["", f"… and {len(r['changes']) - len(shown)} more (use --format json)."]
        if r["rollover"]:
            out += ["", f"Rollover of {cell(r['name'])}, not counted: {f1(r['rolled_over'])} {unit} — "
                    + ", ".join(f"[{c['key']}]({c['url']})" for c in r["rollover"][:MAX_LISTED])]
    return "\n".join(out + [""] + [f"- {n}" for n in res["notes"]])


def render_periods(res):
    rows = [[n, cell(p["name"]), p["start"], p["end"], p["status"]] for n, p in enumerate(res["periods"], 1)]
    return "\n".join([f"# {res['kind'].capitalize()}s — {res['project']}", "",
                      md_table(["#", res["kind"].capitalize(), "Start", "End", "Status"], rows)])


# ------------------------------------------------------------------ main
def at_least(low):
    def check(value):
        if not value.isdigit() or int(value) < low:
            raise argparse.ArgumentTypeError(f"must be a whole number >= {low}")
        return int(value)
    return check


def date_arg(value):
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from None


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "report"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project")
    ap.add_argument("--last", type=at_least(1), default=6, help="finished periods to analyze (default 6)")
    ap.add_argument("--period", action="append", help="analyze this milestone/cycle (repeatable)")
    ap.add_argument("--unit", choices=["auto", "points", "count"], default="auto")
    ap.add_argument("--grace-days", type=at_least(0), default=0,
                    help="days after the start day that are still planning (default 0)")
    ap.add_argument("--timezone", type=timezone_arg, help="IANA name or +HH:MM: where a day begins and ends")
    ap.add_argument("--today", type=date_arg)
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=at_least(3), default=60)
    a = ap.parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace)
    if a.command != "projects" and not a.project:
        sys.exit(f"{a.command} needs --project (run `projects` to list yours)")

    try:
        src = (with_full_path(GitLab(a.gitlab_url, os.environ["GITLAB_TOKEN"], (a.project or "").strip("/") or None))
               if a.target == "gitlab" else
               Plane(a.plane_url, a.workspace, os.environ["PLANE_API_KEY"], a.project, a.rate))
        src.set_days(a.timezone, a.today)
        if a.command == "projects":
            rows = src.my_projects()
            if a.format == "json":
                return print(json.dumps(rows, indent=2))
            return print("\n".join([f"# Your {'GitLab' if a.target == 'gitlab' else 'Plane'} projects", "",
                                    md_table(["#", "Project", "Name"],
                                             [[n, f"[{r['path']}]({r['url']})", cell(r["name"])]
                                              for n, r in enumerate(rows, 1)])]))
        periods = src.periods()
        today = src.today()
        periods = mark_status(periods, today)
        if a.command == "periods":
            src.warn_about_days()
            res = {"project": src.name, "kind": src.kind, "periods": periods}
            return print(json.dumps(res, indent=2) if a.format == "json" else render_periods(res))
        chosen = select(periods, a)[0]
        issues = []
        if chosen:
            first = min(dt.date.fromisoformat(p["start"]) for p in chosen) - dt.timedelta(days=1)
            issues = src.issues(dt.datetime.combine(first, dt.time(), tzinfo=UTC))
    except ApiError as e:
        sys.exit(f"API error: {e}")
    res = analyze(src.name, src.kind, periods, issues, a, today, src.tz)
    if src.tz_note:
        res["notes"].append(src.tz_note)
    print(json.dumps(res, indent=2, default=str) if a.format == "json" else render(res))
