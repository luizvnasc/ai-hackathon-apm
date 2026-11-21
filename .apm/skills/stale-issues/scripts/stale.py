#!/usr/bin/env python3
"""Stale issues of a GitLab milestone or Plane cycle (or of the whole project).

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Read-only: nothing is written to GitLab or Plane.

    python3 stale.py projects --target gitlab|plane
    python3 stale.py periods  --target ... --project P [--stale-days 14]
    python3 stale.py report   --target ... --project P (--period NAME | --all)
                              [--stale-days 14] [--today YYYY-MM-DD] [--format md|json]

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api). Plane uses
PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY. Unset variables are read from a
.env file in the current directory; the environment wins.

An open issue is stale when it has a "stale" label or state, or when nothing
changed on it (its last update: edits, labels, comments, state) for more than
--stale-days days. For work not started yet, the clock starts at the beginning
of the milestone/cycle: waiting in the backlog before it began does not count. Each stale issue gets a kind:
  * stalled      work started (in progress / review / testing) and then stopped;
  * blocked      marked blocked or on hold (see the blocked-issues skill);
  * not started  still in backlog / to do / ready;
  * marked stale it carries a stale label or state.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "stale-issues-skill/1.0"
UTC = dt.timezone.utc
STALE_VALUES = {"stale"}
BLOCKED_VALUES = {"blocked", "on-hold", "on hold", "onhold", "impeded", "bloqueado", "bloqueada"}
STARTED_VALUES = {"in-progress", "in progress", "doing", "wip", "review", "in review", "in-review", "code review",
                  "testing", "qa", "em andamento", "em revisão", "em revisao"}
KIND_ORDER = ["stalled", "blocked", "not started", "marked stale"]


class ApiError(Exception):
    def __init__(self, status, body, method, url):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:400]}")
        self.status = status


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


def parse_dt(value):
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    if len(s) == 10:
        s += "T00:00:00+00:00"
    d = dt.datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def to_date(value):
    d = parse_dt(value)
    return d.date() if d else None


def label_value(name):
    return (name or "").rsplit("::", 1)[-1].strip().lower()


def scoped(labels, scope):
    return [label_value(l) for l in labels if "::" in l and l.rsplit("::", 1)[0].strip().lower() == scope]


def num(values):
    for v in values:
        if re.fullmatch(r"\d+(\.\d+)?", v):
            return float(v)
    return None


def new_issue(**kw):
    it = {"key": None, "title": "", "url": None, "created": None, "updated": None, "status": None,
          "started": False, "blocked": False, "stale_mark": False, "assignees": [], "priority": None,
          "points": None, "period": None}
    it.update(kw)
    return it


# ------------------------------------------------------------------ GitLab
class GitLab:
    kind = "milestone"

    def __init__(self, url, token, project):
        self.url, self.headers, self.project = url.rstrip("/"), {"PRIVATE-TOKEN": token}, project

    def gql(self, query, variables=None):
        data, _ = http("POST", self.url + "/api/graphql", self.headers, {"query": query, "variables": variables or {}})
        if data.get("errors"):
            raise ApiError(200, json.dumps(data["errors"]), "POST", "/api/graphql")
        return data["data"]

    def my_projects(self):
        out, page = [], 1
        while True:
            q = urllib.parse.urlencode({"membership": "true", "archived": "false", "order_by": "last_activity_at",
                                        "sort": "desc", "per_page": 100, "page": page})
            data, headers = http("GET", f"{self.url}/api/v4/projects?{q}", self.headers)
            out += [{"path": p["path_with_namespace"], "name": p["name"], "url": p["web_url"]}
                    for p in data if not p.get("marked_for_deletion_at") and not p.get("marked_for_deletion_on")]
            if not headers.get("X-Next-Page"):
                return out
            page = int(headers["X-Next-Page"])

    def periods(self, today):
        q = """query($p: ID!) { project(fullPath: $p) { name milestones(state: active, includeAncestors: true, first: 100) {
                 nodes { title startDate dueDate } } } }"""
        proj = self.gql(q, {"p": self.project})["project"]
        if not proj:
            sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
        self.name = proj["name"]
        return [{"name": m["title"], "start": m["startDate"], "end": m["dueDate"]} for m in proj["milestones"]["nodes"]]

    def issues(self, period):
        q = """query($p: ID!, $after: String, $ms: [String!]) { project(fullPath: $p) { name
                 workItems(state: opened, milestoneTitle: $ms, types: [ISSUE, TASK, INCIDENT], first: 100, after: $after) {
                   pageInfo { hasNextPage endCursor }
                   nodes { iid title webUrl createdAt updatedAt widgets { type
                     ... on WorkItemWidgetLabels { labels { nodes { title } } }
                     ... on WorkItemWidgetAssignees { assignees { nodes { username } } }
                     ... on WorkItemWidgetMilestone { milestone { title } } } } } } }"""
        out, after = [], None
        while True:
            proj = self.gql(q, {"p": self.project, "after": after, "ms": [period] if period else None})["project"]
            if not proj:
                sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
            self.name = proj["name"]
            page = proj["workItems"]
            for n in page["nodes"]:
                w = {x.get("type"): x for x in n.get("widgets") or []}
                labels = [l["title"] for l in ((w.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
                flow = scoped(labels, "workflow") + scoped(labels, "status") + scoped(labels, "state")
                out.append(new_issue(
                    key=f"#{n['iid']}", title=n["title"], url=n["webUrl"], created=n["createdAt"], updated=n["updatedAt"],
                    status=flow[0] if flow else "open", started=bool(set(flow) & STARTED_VALUES),
                    blocked=bool(set(flow) & BLOCKED_VALUES) or any(label_value(l) in BLOCKED_VALUES for l in labels),
                    stale_mark=any(label_value(l) in STALE_VALUES for l in labels),
                    priority=(scoped(labels, "priority") or [None])[0],
                    points=num(scoped(labels, "points") + scoped(labels, "sp") + scoped(labels, "estimate")),
                    assignees=[a["username"] for a in ((w.get("ASSIGNEES") or {}).get("assignees") or {}).get("nodes", [])],
                    period=((w.get("MILESTONE") or {}).get("milestone") or {}).get("title")))
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]


# ------------------------------------------------------------------ Plane
class Plane:
    kind = "cycle"

    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        self.url, self.ws = url.rstrip("/"), workspace
        self.base = f"{self.url}/api/v1/workspaces/{workspace}"
        self.headers = {"X-API-Key": api_key}
        self.min_interval, self.last = 60.0 / max(1, rate_per_min - 2), 0.0
        self.all_projects = self.list("/projects/")
        if project is None:
            return
        want = project.strip().lower()
        match = [p for p in self.all_projects if want in (p["identifier"].lower(), p["name"].lower()) or p["id"] == project]
        if not match:
            names = ", ".join(f"{p['name']} ({p['identifier']})" for p in self.all_projects)
            sys.exit(f"Plane project {project!r} not found in workspace {workspace!r}. Projects: {names}")
        p = match[0]
        self.pid, self.ident, self.name = p["id"], p["identifier"], f"{p['name']} ({p['identifier']})"

    def req(self, path, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(8):
            wait = self.min_interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                return http("GET", url, self.headers)[0]
            except ApiError as e:
                if e.status == 429:
                    time.sleep(10)
                    continue
                if e.status >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise ApiError(429, "rate limited too many times", "GET", url)

    def list(self, path, params=None):
        params, out = {"per_page": 100, **(params or {})}, []
        while True:
            data = self.req(path, params)
            if isinstance(data, list):
                return out + data
            out += data.get("results") or []
            if data.get("next_page_results") and data.get("next_cursor"):
                params["cursor"] = data["next_cursor"]
            else:
                return out

    def my_projects(self):
        return [{"path": p["identifier"], "name": p["name"], "url": f"{self.url}/{self.ws}/projects/{p['id']}/issues"}
                for p in self.all_projects if p.get("is_member", True) and not p.get("archived_at")]

    def periods(self, today):
        self._cycles = []
        for c in self.list(f"/projects/{self.pid}/cycles/"):
            end = to_date(c.get("end_date"))
            if c.get("archived_at") or (c.get("status") or "").lower() == "completed" or (end and end < today):
                continue
            self._cycles.append(c)
        return [{"name": c["name"], "id": c["id"], "start": (c.get("start_date") or "")[:10] or None,
                 "end": (c.get("end_date") or "")[:10] or None} for c in self._cycles]

    def issues(self, period):
        label_names = {l["id"]: l["name"] for l in self.list(f"/projects/{self.pid}/labels/")}
        params = {"expand": "state,labels,assignees"}
        if period:
            cycles = self.list(f"/projects/{self.pid}/cycles/")
            match = [c for c in cycles if c["id"] == period or c["name"].strip().lower() == period.strip().lower()]
            if not match:
                sys.exit(f"cycle {period!r} not found. Cycles: " + ", ".join(c["name"] for c in cycles))
            rows = self.list(f"/projects/{self.pid}/cycles/{match[0]['id']}/cycle-issues/", params)
        else:
            rows = self.list(f"/projects/{self.pid}/issues/", params)
        out = []
        for r in rows:
            state = r.get("state") if isinstance(r.get("state"), dict) else {}
            group = (state.get("group") or "").lower()
            if group in ("completed", "cancelled") or r.get("archived_at") or r.get("is_draft"):
                continue
            labels = []
            for l in r.get("labels") or []:
                if isinstance(l, dict):
                    parent = label_names.get(l.get("parent"))
                    labels.append(f"{parent}::{l['name']}" if parent else l["name"])
            sname = (state.get("name") or "").strip()
            out.append(new_issue(
                key=f"{self.ident}-{r['sequence_id']}", title=r["name"],
                url=f"{self.url}/{self.ws}/projects/{self.pid}/issues/{r['id']}",
                created=r.get("created_at"), updated=r.get("updated_at"), status=sname or group or "open",
                started=group == "started" and sname.lower() not in BLOCKED_VALUES,
                blocked=sname.lower() in BLOCKED_VALUES or any(label_value(l) in BLOCKED_VALUES for l in labels),
                stale_mark=sname.lower() in STALE_VALUES or any(label_value(l) in STALE_VALUES for l in labels),
                priority=None if r.get("priority") in (None, "none") else r.get("priority"),
                points=num(scoped(labels, "points")), period=period,
                assignees=[a.get("display_name") or a.get("email") for a in r.get("assignees") or []
                           if isinstance(a, dict)]))
        return out


# ------------------------------------------------------------------ analysis
def idle_days(it, now, period_start=None):
    """Days since the last update. Work not started yet is only idle inside the period: its clock starts
    at the period start, since sitting in the backlog before the period began is not staleness."""
    d = parse_dt(it["updated"]) or parse_dt(it["created"])
    if d is not None and period_start is not None and kind_of(it) == "not started":
        d = max(d, period_start)
    return None if d is None else max(0.0, (now - d).total_seconds() / 86400)


def kind_of(it):
    if it["blocked"]:
        return "blocked"
    if it["started"]:
        return "stalled"
    if it["stale_mark"]:
        return "marked stale"
    return "not started"


def next_step(it, kind, period_left, where):
    who = ", ".join("@" + a for a in it["assignees"]) if it["assignees"] else None
    if kind == "stalled":
        return (f"ask {who} for status: finish, split, or hand it over" if who
                else "work started but nobody is assigned: find the owner or move it back to to do")
    if kind == "blocked":
        return "find what blocks it (blocked-issues skill) and escalate or re-plan"
    if kind == "marked stale":
        return f"confirm it is still wanted: close it or move it out of {where}"
    if period_left is not None and period_left <= 7:
        return f"{where} ends soon and it was never started: move it out or drop it"
    return (f"confirm with {who} that it is still planned, or move it out of {where}" if who
            else f"unassigned and untouched: assign it or move it out of {where}")


def analyze(issues, period, stale_days, today, kind="milestone"):
    now = dt.datetime.combine(today, dt.time(23, 59), tzinfo=UTC)
    period_left = None
    if period and period.get("end"):
        period_left = (to_date(period["end"]) - today).days
    start = parse_dt(period["start"]) if period and period.get("start") else None
    where = f"the {kind}" if period else "the plan"
    stale, fresh = [], []
    for it in issues:
        idle = idle_days(it, now, start)
        it["idle_days"] = idle
        if it["stale_mark"] or (stale_days and idle is not None and idle > stale_days):
            k = kind_of(it)
            it["kind"], it["next_step"] = k, next_step(it, k, period_left, where)
            stale.append(it)
        else:
            fresh.append(it)
    stale.sort(key=lambda i: (KIND_ORDER.index(i["kind"]), -(i["idle_days"] or 0)))
    pointed = all(i["points"] is not None for i in issues) and issues
    res = {"open": len(issues), "stale_count": len(stale), "stale": stale,
           "unit": "points" if pointed else "issues",
           "open_size": sum(i["points"] for i in issues) if pointed else len(issues),
           "stale_size": sum(i["points"] for i in stale) if pointed else len(stale),
           "by_kind": {k: sum(1 for i in stale if i["kind"] == k) for k in KIND_ORDER},
           "unassigned": sum(1 for i in stale if not i["assignees"]),
           "aging": sorted([i for i in fresh if stale_days and i["idle_days"] is not None
                            and i["idle_days"] > stale_days / 2], key=lambda i: -i["idle_days"]),
           "period_left": period_left}
    per = {}
    for i in stale:
        for a in i["assignees"] or ["(unassigned)"]:
            per[a] = per.get(a, 0) + 1
    res["per_person"] = sorted(per.items(), key=lambda kv: -kv[1])
    return res


# ------------------------------------------------------------------ output
def f0(x):
    return "—" if x is None else f"{x:.0f}"


def md_table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                     + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def render(res):
    p = res.get("period")
    scope = f"{res['kind']} **{p['name']}** ({p['start'] or '?'} → {p['end'] or '?'})" if p else "**whole project**"
    out = [f"# Stale issues — {res['project']}", "",
           f"Scope: {scope} · today {res['today']} · stale = no activity for more than {res['stale_days']:g} days "
           "or a stale label/state", ""]
    if p and res["period_left"] is not None:
        out.append(f"The {res['kind']} ends in **{res['period_left']} day(s)**." if res["period_left"] >= 0
                   else f"The {res['kind']} ended {-res['period_left']} day(s) ago.")
    share = f" ({100 * res['stale_size'] / res['open_size']:.0f}% of the open {res['unit']})" if res["open_size"] else ""
    out.append(f"**{res['stale_count']} of {res['open']} open issue(s) are stale**"
               + (f": {f0(res['stale_size'])} {res['unit']}{share}" if res["unit"] == "points" else share) + ".")
    if res["stale_count"]:
        out.append("By kind: " + ", ".join(f"{k} {n}" for k, n in res["by_kind"].items() if n)
                   + f" · unassigned {res['unassigned']}.")
        out += ["", md_table(["Issue", "Title", "Kind", "Status", "Idle", "Assignees", "Priority", "Points", "Next step"],
                             [[f"[{i['key']}]({i['url']})", i["title"], i["kind"], i["status"],
                               f"{f0(i['idle_days'])} d", ", ".join("@" + a for a in i["assignees"]) or "—",
                               i["priority"] or "—", f0(i["points"]), i["next_step"]] for i in res["stale"]])]
        out += ["", "## Stale issues per person", "",
                md_table(["Person", "Stale issues"], [[("@" + m) if not m.startswith("(") else m, n]
                                                      for m, n in res["per_person"]])]
    if res["aging"]:
        out += ["", f"## Getting stale (idle for more than {res['stale_days'] / 2:g} days)", ""]
        out += [f"- [{i['key']}]({i['url']}) {i['title']} — {i['status']}, idle {f0(i['idle_days'])} d"
                for i in res["aging"]]
    out += ["", "- Activity is the issue's last update; in Plane, comments may not count as an update."]
    return "\n".join(out)


def render_periods(res):
    rows = [[n, p["name"], p["start"] or "—", p["end"] or "—", p["open"], p["stale"]]
            for n, p in enumerate(res["periods"], 1)]
    rows.append(["—", "whole project", "", "", res["whole"]["open"], res["whole"]["stale"]])
    return "\n".join([f"# Open {res['kind']}s — {res['project']}", "",
                      md_table(["#", res["kind"].capitalize(), "Start", "End", "Open issues",
                                f"Stale (> {res['stale_days']:g} d)"], rows)])


# ------------------------------------------------------------------ config
def require_config(gitlab=False, plane=False, gitlab_url=None, plane_url=None, workspace=None, scope="read_api"):
    """Stop with setup instructions (Linux/macOS and Windows) when a required variable is missing."""
    missing = []
    if gitlab:
        if not gitlab_url:
            missing.append(("GITLAB_URL", "https://gitlab.example.com"))
        if not os.environ.get("GITLAB_TOKEN"):
            missing.append(("GITLAB_TOKEN", f"<personal access token, scope {scope}: GitLab > User settings > Access tokens>"))
    if plane:
        if not plane_url:
            missing.append(("PLANE_URL", "https://plane.example.com"))
        if not workspace:
            missing.append(("PLANE_WORKSPACE", "<workspace slug, as in the Plane URL /<slug>/projects>"))
        if not os.environ.get("PLANE_API_KEY"):
            missing.append(("PLANE_API_KEY", "<API key: Plane > Workspace settings > API tokens>"))
    if not missing:
        return
    key, example = missing[0]
    lines = [f"NOT CONFIGURED: missing {', '.join(k for k, _ in missing)}.", "",
             "Set them as environment variables, or add them to a .env file in the project root",
             "(one KEY=value per line; keep .env out of git):", ""]
    lines += [f"  {k}={v}" for k, v in missing]
    lines += ["", "How to set an environment variable:",
              f'  Linux/macOS (bash/zsh):  export {key}="{example}"',
              f'  Windows PowerShell:      $env:{key} = "{example}"',
              f'                           persistent: setx {key} "{example}"  (then open a new terminal)',
              f"  Windows cmd:             set {key}={example}",
              "", "Restart the agent after changing environment variables (.env is read on every run), then try again."]
    print("\n".join(lines), file=sys.stderr)
    sys.exit(2)


def load_dotenv(path=".env"):
    """Fill unset GITLAB_*/PLANE_* variables from the project's .env; the environment always wins."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?((?:GITLAB|PLANE)_[A-Z0-9_]+)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value and not os.environ.get(key):
            os.environ[key] = value


# ------------------------------------------------------------------ main
def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "report"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--period", help="milestone title (GitLab) or cycle name/id (Plane)")
    scope.add_argument("--all", action="store_true", help="the whole project")
    ap.add_argument("--stale-days", type=float, default=14, help="idle days after which an issue is stale (default 14)")
    ap.add_argument("--today")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=int, default=60)
    a = ap.parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace)

    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
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
        periods = sorted(src.periods(today), key=lambda p: p["start"] or "9999")
        if a.command == "periods":
            everything = src.issues(None)
            for p in periods:
                mine = [i for i in everything if i["period"] == p["name"]] if a.target == "gitlab" else src.issues(p["name"])
                r = analyze(mine, p, a.stale_days, today, src.kind)
                p.update(open=r["open"], stale=r["stale_count"])
            w = analyze(everything, None, a.stale_days, today)
            res = {"project": src.name, "kind": src.kind, "stale_days": a.stale_days, "periods": periods,
                   "whole": {"open": w["open"], "stale": w["stale_count"]}}
            return print(json.dumps(res, indent=2, default=str) if a.format == "json" else render_periods(res))
        if not (a.period or a.all):
            sys.exit("report needs --period <milestone/cycle> or --all (run `periods` to list them)")
        period = None
        if a.period:
            period = next((p for p in periods if p["name"].strip().lower() == a.period.strip().lower()), None) \
                or {"name": a.period, "start": None, "end": None}
        issues = src.issues(period["name"] if period else None)
    except ApiError as e:
        sys.exit(f"API error: {e}")
    res = analyze(issues, period, a.stale_days, today, src.kind)
    res.update(project=src.name, kind=src.kind, period=period, today=today.isoformat(), stale_days=a.stale_days)
    print(json.dumps(res, indent=2, default=str) if a.format == "json" else render(res))


if __name__ == "__main__":
    main()
