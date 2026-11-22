#!/usr/bin/env python3
"""Area-based assignment helper for GitLab and Plane.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Every command is read-only except `assign`.

    python3 assign.py projects        --target gitlab|plane
    python3 assign.py periods         --target ... --project P          # open milestones/cycles, current one marked
    python3 assign.py scores          --target ... --project P          # area score of every developer
    python3 assign.py suggest-member  --target ... --project P --issue KEY
    python3 assign.py suggest-issue   --target ... --project P --member NAME [--period NAME]
    python3 assign.py assign          --target ... --project P --issue KEY --member NAME [--replace] [--yes]
        (assign writes only with --yes, after the user confirmed; without it, or with --dry-run, it prints the plan)
        common options: [--roles developer] [--half-life 180] [--today YYYY-MM-DD] [--format md|json]
                        [--no-comments] (Plane: skip reading comments, much faster)

Issue keys: GitLab "#45", "45" or a URL; Plane "INV-45" or "45".
Members: GitLab username; Plane display name or email.

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api; api for
`assign`). Plane uses PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY.

Score model (the same for both tools):
  * Developers: GitLab members with the Developer role, Plane members with the
    Member role (Plane has no Developer role). --roles widens it.
  * Every issue of the project counts, open or closed. A developer earns points
    on an issue for each way they took part in it:
        assignee of a closed (done) issue   3.0
        assignee of an open issue           1.5
        author                              1.0
        commenter                           1.0
    Cancelled issues count half. Points fade with age: an issue last active
    --half-life days ago (default 180) counts half.
  * The points go to the issue's areas (`area::x` labels / the `area` label
    group, split evenly when there are several) and to its type (`type::x`).
  * Area score 0-100: the developer's points in the area divided by the top
    developer's points there (100 = the team's reference for the area).
  * Fit for an issue = 80% mean score in its areas + 20% score in its type.
    An issue without area, or whose areas nobody has history in, uses its
    type only.
  * Availability 0-100: 100 minus the share of the busiest developer's load,
    where load = open issues assigned in the issue's own milestone/cycle
    (suggest-member) or in the chosen/current one (suggest-issue) (points
    when every issue has points: GitLab points:: label, Plane Estimate field).
  * suggest-member ranks by 75% fit + 25% availability.
  * suggest-issue ranks the open, unassigned, unblocked issues of the period by
    fit + a priority bonus (urgent 20, high 10, medium 5).
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# APM installs each skill by itself: hackathon_shared/ is generated from the package's shared/.
sys.dont_write_bytecode = True  # never write into an installed skill
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python3 -I leaves it out
from hackathon_shared.config import load_dotenv, require_config  # noqa: E402
from hackathon_shared.dates import age_reference, parse_dt  # noqa: E402
from hackathon_shared.days import timezone_arg  # noqa: E402
from hackathon_shared.gitlab import GitLabClient  # noqa: E402
from hackathon_shared.labels import has_cancelled_label, scoped  # noqa: E402
from hackathon_shared.net import ApiError  # noqa: E402
from hackathon_shared.plane import PlaneClient  # noqa: E402
from hackathon_shared.report import md_table  # noqa: E402

UA = "assign-issues-skill/1.0"
ROLE_LEVELS = {
    "gitlab": {"guest": 10, "planner": 15, "reporter": 20, "developer": 30, "maintainer": 40, "owner": 50},
    "plane": {"guest": 5, "member": 15, "developer": 15, "admin": 20, "maintainer": 20, "owner": 20},
}
WEIGHTS = {"assignee_done": 3.0, "assignee_open": 1.5, "author": 1.0, "commenter": 1.0}
PRIORITY_BONUS = {"urgent": 20, "critical": 20, "high": 10, "medium": 5, "low": 0}
BLOCKED = {"blocked", "on-hold", "on hold", "onhold"}
def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA, **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode(errors="replace")
            try:
                return json.loads(raw or "null"), resp.headers
            except ValueError:
                sys.exit(f"the answer from {url} is not JSON: check the URL (it must point to the API server)")
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"), method, url) from None
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {url}: {e.reason}")


def estimate(r):
    """Plane's story points: `estimate_point` expanded to {value: "5", ...}; None when unset or not numeric
    (category or time estimates). Never from labels. The /estimates/ routes are not used: Plane CE does not
    register them (makeplane/plane#9933), and expand works with or without that bug."""
    ep = r.get("estimate_point")
    value = str(ep.get("value") or "").strip() if isinstance(ep, dict) else ""
    return float(value) if re.fullmatch(r"\d+(\.\d+)?", value) else None


def new_issue(**kw):
    it = {"key": None, "title": "", "url": None, "open": True, "cancelled": False, "status": None,
          "labels": [], "areas": [], "types": [], "priority": None, "points": None, "assignees": [],
          "author": None, "commenters": [], "period": None, "updated": None, "closed": None}
    it.update(kw)
    return it


# ------------------------------------------------------------------ GitLab
GL_FIELDS = """
  iid title state webUrl createdAt updatedAt closedAt
  author { username }
  widgets {
    type
    ... on WorkItemWidgetLabels { labels { nodes { title } } }
    ... on WorkItemWidgetAssignees { assignees { nodes { username } } }
    ... on WorkItemWidgetMilestone { milestone { title } }
    %s
  }
"""
GL_NOTES = "... on WorkItemWidgetNotes { notes(first: 100) { nodes { system author { username } } } }"


class GitLab(GitLabClient):
    empty_page_is_ok = True

    def __init__(self, url, token, project):
        if not url:
            sys.exit("GitLab URL missing: set GITLAB_URL or pass --gitlab-url")
        if not token:
            sys.exit("GITLAB_TOKEN is not set (a personal access token: read_api, or api to assign)")
        super().__init__(url, token, project)

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def pid(self):
        return urllib.parse.quote(self.project, safe="")

    def my_projects(self):
        # Until the copies agree: unlike the other skills, only marked_for_deletion_on hides a project here
        return [{"path": p["path_with_namespace"], "name": p["name"], "url": p["web_url"],
                 "open_issues": p.get("open_issues_count")} for p in self.member_projects()
                if not p.get("marked_for_deletion_on")]

    def members(self):
        rows = self.rest_pages(f"/projects/{self.pid()}/members/all")
        return [{"id": m["id"], "name": m["username"], "display": m.get("name"), "level": m["access_level"]}
                for m in rows if m.get("state") == "active" and not m.get("bot")]

    def periods(self):
        q = """query($p: ID!) { project(fullPath: $p) { lastActivityAt milestones(state: active, includeAncestors: true, first: 100) {
                 nodes { title startDate dueDate } } } }"""
        proj = self.graphql(q, {"p": self.project})["project"]
        if not proj:
            sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
        self.learn_times(proj.get("lastActivityAt"))
        return [{"name": m["title"], "start": m["startDate"], "end": m["dueDate"]} for m in proj["milestones"]["nodes"]]

    def issues(self, comments=True):
        q = f"""query($p: ID!, $after: String) {{ project(fullPath: $p) {{
                  workItems(types: [ISSUE, TASK, INCIDENT], first: 50, after: $after) {{
                    pageInfo {{ hasNextPage endCursor }} nodes {{ {GL_FIELDS % (GL_NOTES if comments else "")} }} }} }} }}"""
        out, after = [], None
        while True:
            proj = self.graphql(q, {"p": self.project, "after": after})["project"]
            if not proj:
                sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
            for n in proj["workItems"]["nodes"]:
                self.learn_times(n.get("createdAt"), n.get("updatedAt"), n.get("closedAt"))
                out.append(self.to_issue(n))
            info = proj["workItems"]["pageInfo"]
            if not info["hasNextPage"]:
                return out
            after = info["endCursor"]

    def to_issue(self, n):
        w = {x.get("type"): x for x in n.get("widgets") or []}
        labels = [l["title"] for l in ((w.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
        workflow = (scoped(labels, "workflow") or scoped(labels, "status") or [None])[0]
        notes = ((w.get("NOTES") or {}).get("notes") or {}).get("nodes", [])
        pts = scoped(labels, "points") or scoped(labels, "sp") or scoped(labels, "estimate")
        points = float(pts[0]) if pts and re.fullmatch(r"\d+(\.\d+)?", pts[0]) else None
        is_open = n["state"] == "OPEN"
        return new_issue(
            key=f"#{n['iid']}", iid=int(n["iid"]), title=n["title"], url=n["webUrl"], open=is_open,
            cancelled=not is_open and has_cancelled_label(labels),
            status=workflow or ("open" if is_open else "closed"), labels=labels,
            areas=scoped(labels, "area"), types=scoped(labels, "type"),
            priority=(scoped(labels, "priority") or [None])[0], points=points,
            assignees=[a["username"] for a in ((w.get("ASSIGNEES") or {}).get("assignees") or {}).get("nodes", [])],
            author=(n.get("author") or {}).get("username"),
            commenters=sorted({x["author"]["username"] for x in notes if not x.get("system") and x.get("author")}),
            period=((w.get("MILESTONE") or {}).get("milestone") or {}).get("title"),
            updated=n.get("updatedAt"), closed=n.get("closedAt"))

    def blocked(self, it):
        return (it["status"] or "").lower() in BLOCKED

    def assign(self, it, member, members, replace):
        ids = []
        if not replace:  # the issue's own assignee ids, so people who left the project are kept
            current = self.rest(f"/projects/{self.pid()}/issues/{it['iid']}") or {}
            ids = [a["id"] for a in current.get("assignees") or []]
        if member["id"] not in ids:
            ids.append(member["id"])
        self.rest(f"/projects/{self.pid()}/issues/{it['iid']}", method="PUT", body={"assignee_ids": ids})

    def normalize_key(self, key):
        m = re.search(r"(?:#|/(?:issues|work_items)/)?(\d+)\s*$", key.strip())
        return f"#{m.group(1)}" if m else key


# ------------------------------------------------------------------ Plane
class Plane(PlaneClient):
    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        if not (url and workspace and api_key):
            sys.exit("Plane needs PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY (or --plane-url/--workspace)")
        super().__init__(url, workspace, api_key, rate_per_min)
        self.project = project
        row = self.select_project(project)
        if row:
            self.proj, self.project = row, self.name

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def my_projects(self):
        return [{"path": p["identifier"], "name": p["name"], "url": self.project_url(p["id"]), "open_issues": None}
                for p in self.joined_projects()]

    def members(self):
        """Project members, with the role they hold in the workspace (the API key cannot read project roles)."""
        roles = {m["id"]: m.get("role") for m in self.list("/members/")}
        rows = self.list(f"/projects/{self.pid}/members/")
        self.member_names = {m["id"]: m.get("display_name") or m.get("email") for m in rows}
        return [{"id": m["id"], "name": m.get("display_name") or m.get("email"), "display":
                 " ".join(x for x in (m.get("first_name"), m.get("last_name")) if x) or None,
                 "email": m.get("email"), "level": roles.get(m["id"]) or 0} for m in rows]

    def periods(self):
        out = []
        for c in self.list(f"/projects/{self.pid}/cycles/"):
            if c.get("archived_at") or (c.get("status") or "").lower() == "completed":
                continue
            out.append({"name": c["name"], "id": c["id"], "start": self.local_day(c.get("start_date")),
                        "end": self.local_day(c.get("end_date"))})
        return out

    def issues(self, comments=True):
        if not hasattr(self, "member_names"):
            self.members()
        names = self.member_names
        cycle_of = {}
        for c in self.list(f"/projects/{self.pid}/cycles/"):
            for r in self.list(f"/projects/{self.pid}/cycles/{c['id']}/cycle-issues/"):
                cycle_of[r["id"]] = c["name"]
        out = []
        for r in self.list(f"/projects/{self.pid}/issues/", {"expand": "state,labels,assignees,estimate_point"}):
            if r.get("is_draft"):
                continue
            state = r.get("state") if isinstance(r.get("state"), dict) else {}
            group = state.get("group")
            labels_raw = [l for l in r.get("labels") or [] if isinstance(l, dict)]
            parents = {l["id"]: l["name"] for l in labels_raw}
            labels = []
            for l in labels_raw:
                scope = self.label_parent(l, parents)
                labels.append(f"{scope}::{l['name']}" if scope else l["name"])
            commenters = []
            if comments:
                for c in self.list(f"/projects/{self.pid}/issues/{r['id']}/comments/"):
                    who = names.get(c.get("actor") or c.get("created_by"))
                    if who and who not in commenters:
                        commenters.append(who)
            out.append(new_issue(
                key=f"{self.ident}-{r['sequence_id']}", id=r["id"], title=r["name"],
                url=f"{self.url}/{self.ws}/projects/{self.pid}/issues/{r['id']}",
                open=group not in ("completed", "cancelled"), cancelled=group == "cancelled",
                status=state.get("name"), labels=labels, areas=scoped(labels, "area"), types=scoped(labels, "type"),
                priority=None if r.get("priority") in (None, "none") else r["priority"],
                points=estimate(r),
                assignees=[a.get("display_name") or a.get("email") for a in r.get("assignees") or [] if isinstance(a, dict)],
                assignee_ids=[a["id"] for a in r.get("assignees") or [] if isinstance(a, dict)],
                author=names.get(r.get("created_by")), commenters=commenters,
                period=cycle_of.get(r["id"]), updated=r.get("updated_at"), closed=r.get("completed_at")))
        return out

    def label_parent(self, label, known):
        pid = label.get("parent")
        if not pid:
            return None
        if not hasattr(self, "_label_names"):
            self._label_names = {l["id"]: l["name"] for l in self.list(f"/projects/{self.pid}/labels/")}
        return known.get(pid) or self._label_names.get(pid)

    def blocked(self, it):
        return (it["status"] or "").lower() in BLOCKED or bool(set(l.lower() for l in it["labels"]) & BLOCKED)

    def assign(self, it, member, members, replace):
        ids = [] if replace else list(it.get("assignee_ids") or [])
        if member["id"] not in ids:
            ids.append(member["id"])
        self.request(f"/projects/{self.pid}/issues/{it['id']}/", method="PATCH", body={"assignees": ids})

    def normalize_key(self, key):
        m = re.search(r"(\d+)\s*/?\s*$", key.strip())
        return f"{self.ident}-{m.group(1)}" if m else key


# ------------------------------------------------------------------ scoring
def developers(src, target, roles):
    levels = [ROLE_LEVELS[target][r] for r in roles]
    allowed = set(levels)
    return [m for m in src.members() if m["level"] in allowed]


def current_period(periods, today):
    live = [p for p in periods if p["start"] and p["end"]
            and dt.date.fromisoformat(p["start"][:10]) <= today <= dt.date.fromisoformat(p["end"][:10])]
    return live[0]["name"] if len(live) == 1 else None


def build_scores(issues, devs, today_dt, half_life):
    names = {d["name"] for d in devs}
    area_pts = {d["name"]: {} for d in devs}
    type_pts = {d["name"]: {} for d in devs}
    evidence = {d["name"]: {} for d in devs}
    for it in issues:
        when = parse_dt(it["closed"] or it["updated"])
        age = max(0.0, (today_dt - when).total_seconds() / 86400) if when else 0.0
        decay = 0.5 ** (age / half_life) if half_life else 1.0
        if it["cancelled"]:
            decay *= 0.5
        roles = {}
        for a in it["assignees"]:
            roles[a] = max(roles.get(a, 0), WEIGHTS["assignee_open" if it["open"] else "assignee_done"])
        if it["author"]:
            roles[it["author"]] = roles.get(it["author"], 0) + WEIGHTS["author"]
        for c in it["commenters"]:
            roles[c] = roles.get(c, 0) + WEIGHTS["commenter"]
        for who, w in roles.items():
            if who not in names:
                continue
            pts = w * decay
            for area in it["areas"]:
                area_pts[who][area] = area_pts[who].get(area, 0) + pts / len(it["areas"])
                ev = evidence[who].setdefault(area, {"done": 0, "open": 0, "other": 0})
                if who in it["assignees"]:
                    ev["open" if it["open"] else "done"] += 1
                else:
                    ev["other"] += 1
            for t in it["types"]:
                type_pts[who][t] = type_pts[who].get(t, 0) + pts / len(it["types"])

    def normalize(pts):
        keys = {k for d in pts.values() for k in d}
        top = {k: max(d.get(k, 0) for d in pts.values()) for k in keys}
        return {who: {k: (100 * d.get(k, 0) / top[k] if top[k] else 0) for k in sorted(keys)} for who, d in pts.items()}

    return {"area": normalize(area_pts), "type": normalize(type_pts), "evidence": evidence,
            "areas": sorted({a for it in issues for a in it["areas"]}),
            "types": sorted({t for it in issues for t in it["types"]})}


def fit(scores, who, it):
    # areas/types nobody in the team has history in are left out instead of counting as 0
    a = [scores["area"][who][x] for x in it["areas"] if x in scores["area"][who]]
    t = [scores["type"][who][x] for x in it["types"] if x in scores["type"][who]]
    area = sum(a) / len(a) if a else None
    typ = sum(t) / len(t) if t else None
    if area is None and typ is None:
        return 0.0, area, typ
    if area is None:
        return typ, area, typ
    if typ is None:
        return area, area, typ
    return 0.8 * area + 0.2 * typ, area, typ


def loads(issues, devs, period):
    scope = [i for i in issues if i["open"] and (period is None or i["period"] == period)]
    use_points = bool(scope) and all(i["points"] is not None for i in scope)
    out = {}
    for d in devs:
        mine = [i for i in scope if d["name"] in i["assignees"]]
        out[d["name"]] = {"issues": len(mine), "points": sum(i["points"] or 0 for i in mine),
                          "load": sum(i["points"] or 0 for i in mine) if use_points else len(mine),
                          "keys": [i["key"] for i in mine]}
    top = max((v["load"] for v in out.values()), default=0)
    for v in out.values():
        v["availability"] = 100.0 if not top else 100 * (1 - v["load"] / top)
    return out, ("points" if use_points else "issues")


def find_member(devs, all_members, name):
    want = name.strip().lstrip("@").lower()
    for pool in (devs, all_members):
        for m in pool:
            if want in {str(m["name"]).lower(), str(m.get("display") or "").lower(), str(m.get("email") or "").lower()}:
                return m
        hits = [m for m in pool if want in str(m["name"]).lower() or want in str(m.get("display") or "").lower()]
        if len(hits) == 1:
            return hits[0]
    return None


# ------------------------------------------------------------------ output
def n0(x):
    return "—" if x is None else f"{x:.0f}"


def render_scores(res):
    areas = res["scores"]["areas"]
    rows = [[f"@{d}"] + [n0(res["scores"]["area"][d].get(a, 0)) for a in areas] for d in res["developers"]]
    out = [f"# Area scores — {res['project']}", "",
           f"{len(res['developers'])} developer(s) · {res['issue_count']} issues read · half-life {res['half_life']:g} days", "",
           md_table(["Developer"] + areas, rows), ""]
    best = []
    for a in areas:
        top = max(res["developers"], key=lambda d: res["scores"]["area"][d].get(a, 0), default=None)
        if top and res["scores"]["area"][top].get(a, 0) > 0:
            ev = res["scores"]["evidence"][top].get(a, {})
            best.append(f"- **{a}:** @{top} ({ev.get('done', 0)} done, {ev.get('open', 0)} open as assignee, "
                        f"{ev.get('other', 0)} as author/commenter)")
    out += ["Reference per area:"] + best
    if res["scores"]["types"]:
        out += ["", "Type scores:", "",
                md_table(["Developer"] + res["scores"]["types"],
                         [[f"@{d}"] + [n0(res["scores"]["type"][d].get(t, 0)) for t in res["scores"]["types"]]
                          for d in res["developers"]])]
    return "\n".join(out)


def render_suggest_member(res):
    it = res["issue"]
    out = [f"# Best developer for {it['key']} — {res['project']}", "",
           f"[{it['key']}]({it['url']}) **{it['title']}** · areas: {', '.join(it['areas']) or 'none'} · "
           f"type: {', '.join(it['types']) or 'none'} · priority: {it['priority'] or '—'} · status: {it['status']} · "
           f"assigned to: {', '.join('@' + a for a in it['assignees']) or 'nobody'}", "",
           f"Load = open issues assigned in **{res['load_scope']}** (in {res['load_unit']}).", ""]
    if not it["open"]:
        out += ["> This issue is closed.", ""]
    rows = [[n, f"@{r['member']}", n0(r["final"]), n0(r["fit"]), n0(r["area"]), n0(r["type"]),
             f"{r['load']['load']:g} ({r['load']['issues']} issue{'s' if r['load']['issues'] != 1 else ''})", n0(r["availability"])]
            for n, r in enumerate(res["ranking"], 1)]
    out.append(md_table(["#", "Developer", "Score", "Fit", "Area", "Type", "Load", "Availability"], rows))
    if res["ranking"]:
        b = res["ranking"][0]
        out += ["", f"**Recommendation: @{b['member']}** (score {n0(b['final'])})."]
        ev = b["evidence"]
        if ev:
            out.append("Evidence: " + "; ".join(f"{a}: {e['done']} done, {e['open']} open as assignee, "
                                                f"{e['other']} as author/commenter" for a, e in ev.items()))
        if len(res["ranking"]) > 1 and res["ranking"][1]["final"] >= b["final"] - 5:
            out.append(f"Close call with @{res['ranking'][1]['member']} (score {n0(res['ranking'][1]['final'])}).")
        if b["fit"] == 0:
            out.append("Nobody has history in this issue's area or type: the ranking only reflects availability.")
    for note in res["notes"]:
        out.append(f"- {note}")
    return "\n".join(out)


def render_suggest_issue(res):
    m = res["member"]
    out = [f"# Best issues for @{m} — {res['project']}", "",
           f"Scope: **{res['period'] or 'whole project'}** · candidates: open, unassigned, not blocked "
           f"({len(res['ranking'])} found)", "",
           f"@{m}'s load there: {res['load']['issues']} open issue(s) assigned"
           + (f" ({', '.join(res['load']['keys'])})" if res['load']['keys'] else "") + ".", "",
           "Area scores of @" + m + ": " + (", ".join(f"{a} {n0(s)}" for a, s in
                                                     sorted(res["member_scores"].items(), key=lambda x: -x[1]) if s > 0)
                                           or "no history yet"), ""]
    rows = [[n, f"[{r['key']}]({r['url']})", r["title"][:60], ", ".join(r["areas"]) or "—", r["priority"] or "—",
             n0(r["rank"]), n0(r["fit"]), f"@{r['best_other']} ({n0(r['best_other_fit'])})" if r["best_other"] else "—"]
            for n, r in enumerate(res["ranking"][: res["limit"]], 1)]
    out.append(md_table(["#", "Issue", "Title", "Area", "Priority", "Rank", "Fit", "Best other fit"], rows))
    if res["ranking"]:
        b = res["ranking"][0]
        out += ["", f"**Recommendation: {b['key']}** {b['title']} (fit {n0(b['fit'])}, priority {b['priority'] or '—'})."]
        stretch = [r for r in res["ranking"][: res["limit"]] if r["best_other"] and r["best_other_fit"] - r["fit"] >= 40]
        if stretch:
            out.append("Others fit much better: " + ", ".join(f"{r['key']} (@{r['best_other']})" for r in stretch)
                       + ". Consider them for learning, not for speed.")
    for note in res["notes"]:
        out.append(f"- {note}")
    return "\n".join(out)


def render_periods(res):
    rows = [[n, p["name"] + (" **(current)**" if p["name"] == res["current"] else ""), p["start"] or "—", p["end"] or "—"]
            for n, p in enumerate(res["periods"], 1)]
    out = [f"# Open {res['kind']}s — {res['project']}", "", md_table(["#", res["kind"].capitalize(), "Start", "End"], rows)]
    if not res["current"]:
        out += ["", f"No single {res['kind']} contains today ({res['today']})."]
    return "\n".join(out)


def render_projects(target, rows):
    return "\n".join([f"# Your {'GitLab' if target == 'gitlab' else 'Plane'} projects", "",
                      md_table(["#", "Project", "Name", "Open issues"],
                               [[n, f"[{r['path']}]({r['url']})", r["name"], "—" if r["open_issues"] is None else r["open_issues"]]
                                for n, r in enumerate(rows, 1)])])


# ------------------------------------------------------------------ main


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "scores", "suggest-member", "suggest-issue", "assign"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project")
    ap.add_argument("--issue", help="issue key for suggest-member / assign")
    ap.add_argument("--member", help="developer for suggest-issue / assign")
    ap.add_argument("--period", help="milestone/cycle for suggest-issue and the load (default: the current one)")
    ap.add_argument("--all", action="store_true", help="suggest-issue / load: use the whole project")
    ap.add_argument("--roles", default="developer", help="comma list of roles counted as developers (default developer)")
    ap.add_argument("--half-life", type=float, default=180, help="days after which an issue counts half (0: no decay)")
    ap.add_argument("--include-assigned", action="store_true", help="suggest-issue: also rank issues assigned to others")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--replace", action="store_true", help="assign: replace the current assignees")
    ap.add_argument("--dry-run", action="store_true", help="assign: print the change, write nothing")
    ap.add_argument("--yes", action="store_true", help="assign: write the change (only after the user confirmed it)")
    ap.add_argument("--no-comments", action="store_true", help="do not read comments (faster, mostly for Plane)")
    ap.add_argument("--timezone", type=timezone_arg, help="IANA name or +HH:MM: where a day begins and ends")
    ap.add_argument("--today")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=int, default=60)
    a = ap.parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace, scope="read_api (api to assign)")

    chosen_today = dt.date.fromisoformat(a.today) if a.today else None
    roles = [r.strip().lower() for r in a.roles.split(",") if r.strip()]
    bad = [r for r in roles if r not in ROLE_LEVELS[a.target]]
    if bad:
        sys.exit(f"unknown role(s) {bad}; use: {', '.join(ROLE_LEVELS[a.target])}")
    if a.command != "projects" and not a.project:
        sys.exit(f"{a.command} needs --project (run `projects` to list yours)")
    emit = (lambda obj, md: print(json.dumps(obj, indent=2, default=str) if a.format == "json" else md(obj)))
    try:
        src = (GitLab(a.gitlab_url, os.environ.get("GITLAB_TOKEN"), (a.project or "").strip("/") or None)
               if a.target == "gitlab" else
               Plane(a.plane_url, a.workspace, os.environ.get("PLANE_API_KEY"), a.project, a.rate))
        if a.command == "projects":
            rows = src.my_projects()
            return emit(rows, lambda r: render_projects(a.target, r))

        src.set_days(a.timezone, chosen_today)
        periods = src.periods()
        today = src.today()
        src.warn_about_days()
        current = current_period(periods, today)
        if a.command == "periods":
            return emit({"project": src.project, "kind": src.kind, "today": today.isoformat(), "timezone": str(src.tz),
                         "periods": periods, "current": current}, render_periods)

        if a.period and a.period not in [p["name"] for p in periods]:
            sys.exit(f"{src.kind} {a.period!r} is not open. Open: " + ", ".join(p["name"] for p in periods))
        scope = None if a.all else (a.period or current)

        all_members = src.members()
        devs = developers(src, a.target, roles)
        if not devs:
            sys.exit(f"no project member has the role(s) {roles}; members: "
                     + ", ".join(f"{m['name']} ({m['level']})" for m in all_members))
        issues = src.issues(comments=not a.no_comments)
        scores = build_scores(issues, devs, age_reference(today, src.tz), a.half_life)
        dev_names = [d["name"] for d in devs]
        notes = []
        if scope is None and not a.all:
            notes.append(f"No single {src.kind} contains today: load and candidates use the whole project.")

        if a.command == "scores":
            return emit({"project": src.project, "developers": dev_names, "issue_count": len(issues),
                         "half_life": a.half_life, "scores": scores}, render_scores)

        by_key = {i["key"]: i for i in issues}
        if a.command in ("suggest-member", "assign"):
            if not a.issue:
                sys.exit(f"{a.command} needs --issue")
            it = by_key.get(src.normalize_key(a.issue))
            if not it:
                sys.exit(f"issue {a.issue!r} not found in {src.project}")

        if a.command == "assign":
            if not a.member:
                sys.exit("assign needs --member")
            m = find_member(devs, all_members, a.member)
            if not m:
                sys.exit(f"{a.member!r} is not a member of {src.project}. Developers: {', '.join(dev_names)}")
            before = it["assignees"]
            after = [m["name"]] if a.replace else before + ([m["name"]] if m["name"] not in before else [])
            change = (f"{it['key']} ({it['title']}): assignees {', '.join('@' + x for x in before) or 'none'}"
                      f" -> {', '.join('@' + x for x in after)}")
            if before == after:
                print(f"nothing to do: {change}")
                return
            if a.dry_run:
                print(f"DRY RUN, nothing written: {change}")
                return
            if not a.yes:
                print(f"PLAN, nothing written: {change}\nRe-run with --yes once the user has confirmed this change.")
                return
            src.assign(it, m, all_members, a.replace)
            print(f"assigned: {change}")
            return

        load, unit = loads(issues, devs, scope)

        if a.command == "suggest-member":
            # capacity is measured where the issue will be worked on: its own milestone/cycle
            if not a.all and not a.period and it["period"] and it["period"] in [p["name"] for p in periods]:
                scope = it["period"]
                load, unit = loads(issues, devs, scope)
            ranking = []
            for d in dev_names:
                f, ar, ty = fit(scores, d, it)
                ranking.append({"member": d, "fit": f, "area": ar, "type": ty, "load": load[d],
                                "availability": load[d]["availability"], "final": 0.75 * f + 0.25 * load[d]["availability"],
                                "evidence": {x: scores["evidence"][d][x] for x in it["areas"] if x in scores["evidence"][d]}})
            ranking.sort(key=lambda r: (-r["final"], -r["fit"]))
            new = [x for x in it["areas"] if x not in scores["area"][dev_names[0]]]
            if new:
                notes.append(f"Nobody has history in area {', '.join(new)}: the fit uses the type instead.")
            if not it["areas"]:
                notes.append("The issue has no area label: the fit uses its type only. Add an area:: label for a better match.")
            if it["assignees"]:
                notes.append(f"Already assigned to {', '.join('@' + x for x in it['assignees'])}.")
            if src.blocked(it):
                notes.append("The issue is blocked: whoever takes it cannot start until the block is solved.")
            return emit({"project": src.project, "issue": it, "load_scope": scope or "whole project", "load_unit": unit,
                         "ranking": ranking, "notes": notes}, render_suggest_member)

        if a.command == "suggest-issue":
            if not a.member:
                sys.exit("suggest-issue needs --member")
            m = find_member(devs, all_members, a.member)
            if not m:
                sys.exit(f"{a.member!r} is not a member of {src.project}. Developers: {', '.join(dev_names)}")
            if m["name"] not in dev_names:
                notes.append(f"@{m['name']} does not have the {'/'.join(roles)} role; scored anyway.")
                devs.append(m)
                dev_names.append(m["name"])
                scores = build_scores(issues, devs, age_reference(today, src.tz), a.half_life)
                load, unit = loads(issues, devs, scope)
            who = m["name"]
            cands = [i for i in issues if i["open"] and not src.blocked(i)
                     and (scope is None or i["period"] == scope)
                     and (a.include_assigned or not i["assignees"]) and who not in i["assignees"]]
            ranking = []
            for i in cands:
                f, _, _ = fit(scores, who, i)
                others = [(fit(scores, d, i)[0], d) for d in dev_names if d != who]
                bo = max(others) if others else (0, None)
                ranking.append({"key": i["key"], "title": i["title"], "url": i["url"], "areas": i["areas"],
                                "priority": i["priority"], "assignees": i["assignees"], "fit": f,
                                "rank": f + PRIORITY_BONUS.get((i["priority"] or "").lower(), 0),
                                "best_other": bo[1], "best_other_fit": bo[0]})
            ranking.sort(key=lambda r: (-r["rank"], -r["fit"]))
            new = sorted({x for r in ranking for x in r["areas"] if x not in scores["area"][who]})
            if new:
                notes.append(f"Nobody has history in area {', '.join(new)}: those issues are ranked by type only.")
            if not ranking:
                notes.append("No open, unassigned, unblocked issue in this scope. Try --include-assigned or --all.")
            return emit({"project": src.project, "member": who, "period": scope, "load": load[who],
                         "member_scores": scores["area"][who], "ranking": ranking, "limit": a.limit,
                         "notes": notes}, render_suggest_issue)
    except ApiError as e:
        sys.exit(f"API error: {e}")


if __name__ == "__main__":
    main()
