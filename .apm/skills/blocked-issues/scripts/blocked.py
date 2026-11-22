#!/usr/bin/env python3
"""Report on the blocked issues of a GitLab or Plane project.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. Read-only: nothing is written to GitLab or Plane.

    # the projects you are a member of (when the user names no project)
    python3 blocked.py projects --target gitlab
    python3 blocked.py projects --target plane

    # open milestones (GitLab) or open cycles (Plane), with blocked counts
    python3 blocked.py periods --target gitlab --project acme/inventory-manager
    python3 blocked.py periods --target plane  --project INV

    # the report, for one milestone/cycle or for the whole project
    python3 blocked.py report --target gitlab --project acme/inventory-manager --period "v0.3 - Purchase Orders"
    python3 blocked.py report --target plane  --project INV --all [--deep]
        [--today YYYY-MM-DD] [--stale-days 30] [--format md|json]

Credentials: GitLab uses GITLAB_URL and GITLAB_TOKEN (read_api scope). Plane
uses PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY.

An issue is blocked when it is open and
  - its workflow label / state is "blocked" (or "on hold"), or
  - it has an open "blocked by" issue link / relation.
An issue is stale, and left out of the report, when it has a "stale" label or
state, or when nothing changed on it for more than --stale-days days.

For each blocked issue the report looks at its parent and children, its linked
issues (blocked by / blocks / relates to), and its comments and description
(block reasons, issues mentioned but not linked, external tickets), then gives
a diagnosis and a next step.
"""
import argparse
import datetime as dt
import html
import json
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
from hackathon_shared.dates import age_reference, day_of, parse_dt, to_date  # noqa: E402
from hackathon_shared.days import timezone_arg  # noqa: E402
from hackathon_shared.gitlab import GitLabClient  # noqa: E402
from hackathon_shared.labels import BLOCKED_VALUES, STALE_VALUES, label_value  # noqa: E402
from hackathon_shared.net import ApiError  # noqa: E402
from hackathon_shared.plane import PlaneClient  # noqa: E402

UA = "blocked-issues-skill/1.0"
BLOCK_WORDS = re.compile(
    r"block|bloque|imped|waiting|awaiting|aguard|depend|pending|pendente|on[ -]?hold|stuck|travad|espera|"
    r"cannot proceed|can't proceed|não consigo|nao consigo", re.I)
EXT_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")
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


# ------------------------------------------------------------------ helpers
def age_days(since, now):
    return None if since is None else max(0, (now - since).total_seconds() / 86400)


def fmt_days(d):
    return "?" if d is None else f"{d:.0f} d"


def strip_html(s):
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", s or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def is_blocked_name(name):
    return bool(name) and label_value(name) in BLOCKED_VALUES


def is_stale_name(name):
    return bool(name) and label_value(name) in STALE_VALUES


def one_line(text, limit=220):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def new_item(**kw):
    item = {"key": None, "title": "", "url": None, "open": True, "status": None, "labels": [],
            "assignees": [], "priority": None, "created": None, "updated": None, "period": None,
            "parent": None, "children": [], "links": [], "comments": [], "description": "",
            "blocked_since": None, "blocked_since_source": None, "detailed": False}
    item.update(kw)
    return item


# ------------------------------------------------------------------ GitLab
GL_ITEM_FIELDS = """
  id iid title state webUrl createdAt updatedAt reference(full: true)
  description
  widgets {
    type
    ... on WorkItemWidgetLabels { labels { nodes { title } } }
    ... on WorkItemWidgetAssignees { assignees { nodes { username } } }
    ... on WorkItemWidgetMilestone { milestone { title } }
    ... on WorkItemWidgetHierarchy {
      parent { id iid title state webUrl reference(full: true) }
      children(first: 100) { nodes { id iid title state webUrl reference(full: true) } }
    }
    ... on WorkItemWidgetLinkedItems {
      linkedItems(first: 100) { nodes { linkType linkCreatedAt
        workItem { id iid title state webUrl reference(full: true) } } }
    }
  }
"""


class GitLab(GitLabClient):
    empty_page_is_ok = True

    def __init__(self, url, token, project):
        if not url:
            sys.exit("GitLab URL missing: set GITLAB_URL or pass --gitlab-url")
        if not token:
            sys.exit("GITLAB_TOKEN is not set (a personal access token with the read_api scope)")
        super().__init__(url, token, project)
        self.items = {}          # key -> item
        self.by_gid = {}         # global id -> key
        self.project_ids = {}    # full path -> numeric id

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def graphql(self, query, variables=None):
        # Until the copies agree: this skill's error names the whole URL, the others only the path
        data, _ = self.send("POST", self.url + "/api/graphql", {"query": query, "variables": variables or {}})
        if data.get("errors"):
            raise ApiError(200, json.dumps(data["errors"]), "POST", self.url + "/api/graphql")
        return data["data"]

    def my_projects(self):
        # Until the copies agree: unlike the other skills, only marked_for_deletion_on hides a project here
        return [{"path": p["path_with_namespace"], "name": p["name"], "url": p["web_url"],
                 "open_issues": p.get("open_issues_count"),
                 "last_activity": (p.get("last_activity_at") or "")[:10] or None} for p in self.member_projects()
                if not p.get("marked_for_deletion_on")]

    def project_id(self, path):
        if path not in self.project_ids:
            self.project_ids[path] = self.rest("/projects/" + urllib.parse.quote(path, safe=""))["id"]
        return self.project_ids[path]

    # --- keys
    def key_of(self, reference):
        path, _, iid = reference.rpartition("#")
        return f"#{iid}" if path == self.project else reference

    def split_key(self, key):
        path, _, iid = key.rpartition("#")
        return (path or self.project), int(iid)

    # --- conversion
    def to_item(self, n):
        w = {x.get("type"): x for x in n.get("widgets") or []}
        labels = [l["title"] for l in ((w.get("LABELS") or {}).get("labels") or {}).get("nodes", [])]
        status = next((l for l in labels if is_blocked_name(l)), None) or next(
            (l for l in labels if l.split("::")[0].lower() in ("workflow", "status", "state")), None)
        hier = w.get("HIERARCHY") or {}
        parent = hier.get("parent")
        links = []
        for ln in ((w.get("LINKED_ITEMS") or {}).get("linkedItems") or {}).get("nodes", []):
            other = ln["workItem"]
            self.remember_stub(other)
            links.append({"type": ln["linkType"], "key": self.key_of(other["reference"]),
                          "created": ln.get("linkCreatedAt"), "resolved": False})
        if parent:
            self.remember_stub(parent)
        children = []
        for c in ((hier.get("children") or {}).get("nodes")) or []:
            self.remember_stub(c)
            children.append(self.key_of(c["reference"]))
        item = new_item(
            key=self.key_of(n["reference"]), gid=n["id"], title=n["title"], url=n["webUrl"],
            open=n["state"] == "OPEN", status=status or ("closed" if n["state"] != "OPEN" else "open"),
            labels=labels, priority=next((label_value(l) for l in labels if l.lower().startswith("priority::")), None),
            assignees=[a["username"] for a in ((w.get("ASSIGNEES") or {}).get("assignees") or {}).get("nodes", [])],
            created=n.get("createdAt"), updated=n.get("updatedAt"),
            period=((w.get("MILESTONE") or {}).get("milestone") or {}).get("title"),
            parent=self.key_of(parent["reference"]) if parent else None, children=children,
            links=links, description=n.get("description") or "", full=True)
        return item

    def remember_stub(self, n):
        key = self.key_of(n["reference"])
        if key not in self.items:
            self.items[key] = new_item(key=key, gid=n["id"], title=n["title"], url=n["webUrl"],
                                       open=n["state"] == "OPEN", full=False)
            self.by_gid[n["id"]] = key

    def store(self, item):
        old = self.items.get(item["key"])
        if old and old.get("full"):
            return old
        self.items[item["key"]] = item
        self.by_gid[item["gid"]] = item["key"]
        return item

    def full(self, key):
        """Return the item with labels/assignees, fetching it if only a stub is known."""
        it = self.items.get(key)
        if it and it.get("full"):
            return it
        try:
            if it and it.get("gid"):
                n = self.graphql(f"query($id: WorkItemID!) {{ workItem(id: $id) {{ {GL_ITEM_FIELDS} }} }}",
                             {"id": it["gid"]})["workItem"]
            else:
                path, iid = self.split_key(key)
                n = (self.graphql(f"query($p: ID!, $iid: String!) {{ project(fullPath: $p) {{ workItems(iid: $iid) "
                              f"{{ nodes {{ {GL_ITEM_FIELDS} }} }} }} }}", {"p": path, "iid": str(iid)})
                     ["project"] or {}).get("workItems", {}).get("nodes")
                n = (n or [None])[0]
        except ApiError:
            n = None
        if not n:
            return it
        return self.store(self.to_item(n))

    # --- scope
    def periods(self):
        q = """query($p: ID!) { project(fullPath: $p) { name webUrl lastActivityAt
                 milestones(state: active, includeAncestors: true, first: 100) {
                   nodes { title startDate dueDate webPath } } } }"""
        proj = self.graphql(q, {"p": self.project})["project"]
        if not proj:
            sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
        self.learn_times(proj.get("lastActivityAt"))
        items = self.load(None)
        out = []
        for m in proj["milestones"]["nodes"]:
            mine = [i for i in items if i["period"] == m["title"]]
            out.append({"name": m["title"], "start": m["startDate"], "end": m["dueDate"],
                        "open_issues": len(mine), "blocked_label": sum(is_blocked_name(i["status"]) for i in mine)})
        return {"project": proj["name"], "url": proj["webUrl"], "kind": "milestone", "periods": out,
                "whole_project": {"open_issues": len(items),
                                  "blocked_label": sum(is_blocked_name(i["status"]) for i in items)}}

    def load(self, milestone):
        q = f"""query($p: ID!, $after: String, $ms: [String!]) {{ project(fullPath: $p) {{
                  workItems(state: opened, milestoneTitle: $ms, types: [ISSUE, TASK, INCIDENT], first: 50, after: $after) {{
                    pageInfo {{ hasNextPage endCursor }} nodes {{ {GL_ITEM_FIELDS} }} }} }} }}"""
        out, after = [], None
        while True:
            proj = self.graphql(q, {"p": self.project, "after": after, "ms": [milestone] if milestone else None})["project"]
            if not proj:
                sys.exit(f"GitLab project {self.project!r} not found (or the token cannot read it)")
            page = proj["workItems"]
            for n in page["nodes"]:
                self.learn_times(n.get("createdAt"), n.get("updatedAt"), n.get("closedAt"))
                item = self.to_item(n)
                self.items[item["key"]] = item
                self.by_gid[item["gid"]] = item["key"]
                out.append(item)
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]

    def resolve_links(self, item):
        """GitLab Free reports "blocks" on both ends of a blocking link: read the direction from the link itself."""
        if not any(l["type"] in ("blocks", "is_blocked_by") for l in item["links"]):
            return
        path, iid = self.split_key(item["key"])
        pid = self.project_id(path)
        try:
            rest_links = self.rest(f"/projects/{pid}/issues/{iid}/links")
        except ApiError:
            return
        direction = {}
        for rl in rest_links:
            if rl.get("link_type") not in ("blocks", "is_blocked_by"):
                continue
            other_key = self.key_of(rl["references"]["full"])
            try:
                detail = self.rest(f"/projects/{pid}/issues/{iid}/links/{rl['issue_link_id']}")
                src_issue = detail.get("source_issue") or {}
                source_is_me = (src_issue.get("iid") == iid and src_issue.get("project_id") == pid) \
                    if src_issue else None
            except ApiError:
                source_is_me = None
            if source_is_me is not None:
                direction[other_key] = "blocks" if source_is_me else "blocked_by"
        for l in item["links"]:
            if l["type"] in ("blocks", "is_blocked_by"):
                l["type"] = direction.get(l["key"], "blocked_by" if l["type"] == "is_blocked_by" else "blocks")
                l["resolved"] = l["key"] in direction

    def detail(self, item):
        if item["detailed"]:
            return
        item["detailed"] = True
        path, iid = self.split_key(item["key"])
        pid = self.project_id(path)
        notes = self.rest_pages(f"/projects/{pid}/issues/{iid}/notes", {"sort": "asc", "order_by": "created_at"})
        item["comments"] = [{"author": n["author"]["username"], "created": n["created_at"], "body": n["body"]}
                            for n in notes if not n.get("system")]
        try:
            events = self.rest_pages(f"/projects/{pid}/issues/{iid}/resource_label_events")
        except ApiError:
            events = []
        adds = [e["created_at"] for e in events
                if e.get("action") == "add" and is_blocked_name((e.get("label") or {}).get("name"))]
        if adds:
            item["blocked_since"], item["blocked_since_source"] = adds[-1], "label added"

    def ref_pattern(self):
        return re.compile(r"(?:(?<![\w/.-])([\w.-]+(?:/[\w.-]+)+)|(?<![\w&]))#(\d+)\b|"
                          + re.escape(self.url) + r"/([\w./-]+?)/-/(?:issues|work_items)/(\d+)")

    def mentions(self, text):
        out = []
        for m in self.ref_pattern().finditer(text or ""):
            if m.group(2):
                path = m.group(1) or self.project
                out.append(f"#{m.group(2)}" if path == self.project else f"{path}#{m.group(2)}")
            else:
                path = m.group(3)
                out.append(f"#{m.group(4)}" if path == self.project else f"{path}#{m.group(4)}")
        return out

    def own_ticket_prefix(self):
        return None


# ------------------------------------------------------------------ Plane
class Plane(PlaneClient):
    def __init__(self, url, workspace, api_key, project, rate_per_min=60):
        if not (url and workspace and api_key):
            sys.exit("Plane needs PLANE_URL, PLANE_WORKSPACE and PLANE_API_KEY (or --plane-url/--workspace)")
        super().__init__(url, workspace, api_key, rate_per_min)
        self.items, self.by_id = {}, {}
        row = self.select_project(project)
        self.idents = {p["id"]: p["identifier"] for p in self.all_projects}
        if not row:
            return
        self.proj, self.project = row, self.name
        try:
            self.members = {m["id"]: m.get("display_name") or m.get("email") for m in self.list("/members/")}
        except ApiError:
            self.members = {}

    def send(self, method, url, body=None):
        return http(method, url, self.headers, body)

    def my_projects(self):
        return [{"path": p["identifier"], "name": p["name"], "url": self.project_url(p["id"]),
                 "open_issues": None, "last_activity": (p.get("updated_at") or "")[:10] or None}
                for p in self.joined_projects()]

    def web_url(self, project_id, issue_id):
        return f"{self.url}/{self.ws}/projects/{project_id}/issues/{issue_id}"

    def to_item(self, r):
        state = r.get("state") if isinstance(r.get("state"), dict) else {}
        labels = [l["name"] for l in r.get("labels") or [] if isinstance(l, dict)]
        group = state.get("group")
        pid = r.get("project") or self.pid
        item = new_item(
            key=f"{self.idents.get(pid, self.ident)}-{r['sequence_id']}", id=r["id"], project_id=pid,
            title=r["name"], url=self.web_url(pid, r["id"]),
            open=group not in ("completed", "cancelled") and not r.get("archived_at"),
            status=state.get("name"), status_group=group, labels=labels,
            priority=None if r.get("priority") in (None, "none") else r.get("priority"),
            assignees=[a.get("display_name") or a.get("email") for a in r.get("assignees") or [] if isinstance(a, dict)],
            created=r.get("created_at"), updated=r.get("updated_at"), parent_id=r.get("parent"),
            description=strip_html(r.get("description_html")), full=True)
        self.items[item["key"]] = item
        self.by_id[item["id"]] = item["key"]
        return item

    def fetch(self, project_id, issue_id):
        if issue_id in self.by_id:
            return self.items[self.by_id[issue_id]]
        try:
            r = self.request(f"/projects/{project_id}/issues/{issue_id}/", {"expand": "state,labels,assignees"})
        except ApiError:
            return None
        return self.to_item(r)

    def full(self, key):
        if key in self.items:
            return self.items[key]
        try:
            r = self.request(f"/work-items/{key}/", {"expand": "state,labels,assignees"})
        except ApiError:
            return None
        return self.to_item(r)

    def open_cycles(self, today):
        out = []
        for c in self.list(f"/projects/{self.pid}/cycles/"):
            end = to_date(self.local_day(c.get("end_date")))
            status = (c.get("status") or "").lower()
            if c.get("archived_at") or status == "completed" or (end and end < today):
                continue
            out.append(c)
        return sorted(out, key=lambda c: c.get("start_date") or "")

    def periods(self, today):
        out = []
        for c in self.open_cycles(today):
            items = [self.to_item(r) for r in self.list(f"/projects/{self.pid}/cycles/{c['id']}/cycle-issues/",
                                                       {"expand": "state,labels,assignees"})]
            items = [i for i in items if i["open"]]
            out.append({"name": c["name"], "id": c["id"], "start": self.local_day(c.get("start_date")),
                        "end": self.local_day(c.get("end_date")), "open_issues": len(items),
                        "blocked_label": sum(self.marked_blocked(i) for i in items)})
        allitems = [i for i in self.load(None) if i["open"]]
        return {"project": self.project, "url": f"{self.url}/{self.ws}/projects/{self.pid}/issues", "kind": "cycle",
                "periods": out, "whole_project": {"open_issues": len(allitems),
                                                  "blocked_label": sum(self.marked_blocked(i) for i in allitems)}}

    @staticmethod
    def marked_blocked(item):
        return is_blocked_name(item["status"]) or any(is_blocked_name(l) for l in item["labels"])

    def load(self, cycle):
        if cycle:
            want = cycle.strip().lower()
            cycles = self.list(f"/projects/{self.pid}/cycles/")
            match = [c for c in cycles if c["id"] == cycle or c["name"].strip().lower() == want]
            if not match:
                sys.exit(f"cycle {cycle!r} not found. Cycles: " + ", ".join(c["name"] for c in cycles))
            rows = self.list(f"/projects/{self.pid}/cycles/{match[0]['id']}/cycle-issues/",
                             {"expand": "state,labels,assignees"})
            name = match[0]["name"]
        else:
            rows = self.list(f"/projects/{self.pid}/issues/", {"expand": "state,labels,assignees"})
            name = None
        items = [self.to_item(r) for r in rows if not r.get("is_draft")]
        for it in items:
            it["period"] = name
        # parents and children (children are only known inside the loaded scope)
        for it in list(self.items.values()):
            if it.get("parent_id"):
                parent = self.items.get(self.by_id.get(it["parent_id"])) or self.fetch(self.pid, it["parent_id"])
                if parent:
                    it["parent"] = parent["key"]
                    if it["key"] not in parent["children"]:
                        parent["children"].append(it["key"])
        return items

    def load_relations(self, item):
        if item.get("relations_loaded"):
            return
        item["relations_loaded"] = True
        try:
            rel = self.request(f"/projects/{item['project_id']}/work-items/{item['id']}/relations/")
        except ApiError:
            try:
                rel = self.request(f"/projects/{item['project_id']}/issues/{item['id']}/relations/")
            except ApiError:
                print(f"warning: cannot read the relations of {item['key']}", file=sys.stderr)
                return
        kinds = {"blocked_by": "blocked_by", "blocking": "blocks", "relates_to": "relates_to", "duplicate": "duplicate"}
        for kind, entries in (rel or {}).items():
            for e in entries or []:
                other = self.fetch(e.get("project_id") or item["project_id"], e.get("issue_id") or e.get("id"))
                if other:
                    item["links"].append({"type": kinds.get(kind, kind), "key": other["key"], "created": None,
                                          "resolved": True})

    def detail(self, item):
        if item["detailed"]:
            return
        item["detailed"] = True
        self.load_relations(item)
        comments = self.list(f"/projects/{item['project_id']}/issues/{item['id']}/comments/")
        item["comments"] = sorted(
            ({"author": (c.get("actor_detail") or {}).get("display_name")
                        or self.members.get(c.get("actor") or c.get("created_by")) or c.get("actor"),
              "created": c["created_at"], "body": strip_html(c.get("comment_html"))} for c in comments),
            key=lambda c: c["created"])
        try:
            acts = self.list(f"/projects/{item['project_id']}/issues/{item['id']}/activities/")
        except ApiError:
            acts = []
        hits = sorted(a["created_at"] for a in acts
                      if a.get("field") == "state" and is_blocked_name(a.get("new_value")))
        if hits:
            item["blocked_since"], item["blocked_since_source"] = hits[-1], "state changed"

    def mentions(self, text):
        return [f"{self.ident}-{m.group(1)}" for m in re.finditer(rf"\b{re.escape(self.ident)}-(\d+)\b", text or "")]

    def own_ticket_prefix(self):
        return self.ident


# ------------------------------------------------------------------ analysis
def find_blocked(src, items, deep, today_dt, stale_days, target):
    marked, link_only, stale = [], [], []
    for it in items:
        if not it["open"]:
            continue
        flagged = is_blocked_name(it["status"]) or any(is_blocked_name(l) for l in it["labels"])
        if not flagged:
            if target == "gitlab":
                has_block_link = any(l["type"] in ("blocks", "is_blocked_by") for l in it["links"])
                if not has_block_link:
                    continue
                src.resolve_links(it)
            elif deep:
                src.load_relations(it)
            else:
                continue
            open_blockers = [l for l in it["links"] if l["type"] == "blocked_by" and (src.full(l["key"]) or {}).get("open")]
            if not open_blockers:
                continue
        is_stale = is_stale_name(it["status"]) or any(is_stale_name(l) for l in it["labels"])
        idle = age_days(parse_dt(it["updated"]), today_dt)
        if is_stale or (stale_days and idle is not None and idle > stale_days):
            stale.append({"key": it["key"], "title": it["title"], "url": it["url"],
                          "why": "stale label/state" if is_stale else f"no activity for {idle:.0f} days"})
            continue
        (marked if flagged else link_only).append(it)
    return marked, link_only, stale


def summarize_other(src, key, today_dt):
    o = src.full(key)
    if not o:
        return {"key": key, "title": "(not readable)", "open": None}
    return {"key": key, "title": o["title"], "url": o["url"], "open": o["open"], "status": o["status"],
            "assignees": o["assignees"], "idle_days": age_days(parse_dt(o["updated"]), today_dt),
            "blocked": o["open"] and (is_blocked_name(o["status"]) or any(is_blocked_name(l) for l in o["labels"])),
            "stale": any(is_stale_name(l) for l in o["labels"]) or is_stale_name(o["status"])}


def with_day(comment, zone):
    """The comment with the project's day it was written on."""
    day = day_of(parse_dt(comment["created"]), zone).isoformat()
    return {**comment, "day": day}


def analyze(src, it, today_dt, stale_days, marked):
    if hasattr(src, "resolve_links"):
        src.resolve_links(it)
    src.detail(it)
    by_type = {}
    for l in it["links"]:
        by_type.setdefault(l["type"], []).append(summarize_other(src, l["key"], today_dt))
    blockers = by_type.get("blocked_by", [])
    open_blockers = [b for b in blockers if b["open"]]
    parent = summarize_other(src, it["parent"], today_dt) if it["parent"] else None
    children = [summarize_other(src, k, today_dt) for k in it["children"]]
    blocked_children = [c for c in children if c.get("blocked")]

    texts = [("description", None, it["description"])] + [("comment", c, c["body"]) for c in it["comments"]]
    block_comments = [c for c in it["comments"] if BLOCK_WORDS.search(c["body"] or "")]
    if not block_comments and BLOCK_WORDS.search(it["description"] or ""):
        reason_in_description = next((ln for ln in it["description"].splitlines() if BLOCK_WORDS.search(ln)), None)
    else:
        reason_in_description = None
    linked = {l["key"] for l in it["links"]} | ({it["parent"]} if it["parent"] else set()) | set(it["children"]) | {it["key"]}
    mentioned, external = [], []
    own = src.own_ticket_prefix()
    for _, _, text in texts:
        for k in src.mentions(text):
            if k not in linked and k not in [m["key"] for m in mentioned]:
                mentioned.append(summarize_other(src, k, today_dt))
        for m in EXT_TICKET.finditer(text or ""):
            t = m.group(0)
            if m.group(1) != own and t not in external:
                external.append(t)

    # when did the block start
    since, source = parse_dt(it["blocked_since"]), it["blocked_since_source"]
    if since is None and block_comments:
        since, source = parse_dt(block_comments[0]["created"]), "first block comment"
    if since is None:
        link_dates = [parse_dt(l["created"]) for l in it["links"] if l["type"] == "blocked_by" and l.get("created")]
        if link_dates:
            since, source = min(link_dates), "link created"
    if since is None:
        since, source = parse_dt(it["updated"]), "last update (approx.)"

    causes, steps = [], []
    owner = ", ".join("@" + a for a in it["assignees"]) or None
    if open_blockers:
        causes.append("dependency")
        for b in open_blockers:
            who = ", ".join("@" + a for a in b.get("assignees") or []) or "nobody"
            extra = []
            if b.get("blocked"):
                extra.append("it is blocked too (chain)")
            if b.get("stale") or (stale_days and (b.get("idle_days") or 0) > stale_days):
                extra.append("it looks abandoned")
            if not b.get("assignees"):
                extra.append("it has no assignee")
            steps.append(f"Follow up on {b['key']} ({b.get('status') or 'open'}, assigned to {who}, "
                         f"last update {fmt_days(b.get('idle_days'))} ago)" + (f": {', '.join(extra)}." if extra else "."))
    elif blockers:
        causes.append("dependency resolved")
        steps.append("Every blocking issue is closed: move this issue back to ready / in progress.")
    if parent and parent.get("blocked"):
        causes.append("parent blocked")
        steps.append(f"The parent {parent['key']} is blocked as well: unblocking it comes first.")
    if parent and parent.get("open") is False:
        steps.append(f"The parent {parent['key']} is already closed: check whether this issue is still needed.")
    if blocked_children:
        causes.append("children blocked")
        steps.append("Blocked children: " + ", ".join(c["key"] for c in blocked_children) + ".")
    open_mentions = [m for m in mentioned if m.get("open")]
    if open_mentions:
        causes.append("mentioned issue (not linked)")
        steps.append("Comments mention " + ", ".join(m["key"] for m in open_mentions)
                     + " without an issue link: add a \"blocked by\" link so the dependency is visible.")
    if (block_comments or reason_in_description or external) and not open_blockers and not open_mentions:
        causes.append("external")
        ref = f" ({', '.join(external)})" if external else ""
        steps.append(f"External cause{ref}: name an owner for the follow-up and set a date to check it again.")
    if not causes:
        causes.append("unknown")
        steps.append((f"Nothing says why it is blocked: ask {owner} to write the cause in a comment "
                      "and link the blocking issue, or move it out of Blocked.") if owner else
                     "Nothing says why it is blocked and nobody is assigned: assign an owner first.")
    if not it["assignees"] and causes[0] != "unknown":
        steps.append("Nobody is assigned: assign an owner to drive the unblocking.")
    if not marked:
        steps.append("It has an open blocker but is not marked as blocked: update its workflow label / state.")

    return {
        "key": it["key"], "title": it["title"], "url": it["url"], "status": it["status"],
        "marked_blocked": marked, "assignees": it["assignees"], "priority": it["priority"], "period": it["period"],
        "blocked_since": day_of(since, src.tz).isoformat() if since else None, "blocked_since_source": source,
        "blocked_days": age_days(since, today_dt), "idle_days": age_days(parse_dt(it["updated"]), today_dt),
        "parent": parent, "children": children, "blocked_by": blockers, "blocks": by_type.get("blocks", []),
        "related": by_type.get("relates_to", []) + by_type.get("duplicate", []),
        "block_comments": [with_day(c, src.tz) for c in block_comments[-3:]], "reason_in_description": reason_in_description,
        "last_comment": with_day(it["comments"][-1], src.tz) if it["comments"] else None, "comment_count": len(it["comments"]),
        "mentioned_not_linked": mentioned, "external_refs": external,
        "causes": causes, "next_steps": steps,
    }


# ------------------------------------------------------------------ output
def ref_md(o):
    if not o:
        return "—"
    state = "closed" if o.get("open") is False else (o.get("status") or "open")
    title = one_line(o.get("title"), 70)
    who = ", ".join("@" + a for a in o.get("assignees") or [])
    return f"[{o['key']}]({o['url']}) {title} — {state}" + (f", {who}" if who else "") if o.get("url") else f"{o['key']} {title}"


def render_md(res):
    out = [f"# Blocked issues — {res['project']}", "",
           f"Scope: **{res['scope']}** · days in {res['timezone']} · today {res['today']} · stale = no activity for more than "
           f"{res['stale_days']:g} days or a stale label/state", ""]
    rows = res["issues"]
    out.append(f"**{len(rows)} blocked issue(s)**, {len(res['stale_excluded'])} stale left out.")
    out.append("")
    if rows:
        counts = {}
        for r in rows:
            counts[r["causes"][0]] = counts.get(r["causes"][0], 0) + 1
        out.append("Main cause: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
        out.append("")
        out.append("| Issue | Title | Assignee | Priority | Blocked for | Cause | Blocked by |")
        out.append("|---|---|---|---|---|---|---|")
        for r in rows:
            by = ", ".join(b["key"] + ("" if b["open"] else " (closed)") for b in r["blocked_by"]) or "—"
            out.append(f"| [{r['key']}]({r['url']}) | {one_line(r['title'], 60)} | "
                       f"{', '.join('@' + a for a in r['assignees']) or '**unassigned**'} | {r['priority'] or '—'} | "
                       f"{fmt_days(r['blocked_days'])} | {', '.join(r['causes'])} | {by} |")
        out.append("")
    for r in rows:
        out += [f"## [{r['key']}]({r['url']}) {r['title']}", ""]
        out.append(f"- **Status:** {r['status'] or '—'}" + ("" if r["marked_blocked"] else " (not marked blocked)")
                   + f" · **Assignee:** {', '.join('@' + a for a in r['assignees']) or 'unassigned'}"
                   + f" · **Priority:** {r['priority'] or '—'}" + (f" · **Milestone/cycle:** {r['period']}" if r['period'] else ""))
        out.append(f"- **Blocked since:** {r['blocked_since'] or '?'} ({r['blocked_since_source']}), "
                   f"{fmt_days(r['blocked_days'])}; last activity {fmt_days(r['idle_days'])} ago")
        out.append(f"- **Parent:** {ref_md(r['parent']) if r['parent'] else 'none'}"
                   + (" — **blocked**" if r["parent"] and r["parent"].get("blocked") else ""))
        if r["children"]:
            open_c = [c for c in r["children"] if c.get("open")]
            out.append(f"- **Children:** {len(open_c)} open of {len(r['children'])}"
                       + "".join(f"\n  - {ref_md(c)}" + (" — **blocked**" if c.get("blocked") else "") for c in r["children"]))
        for label, key in (("Blocked by", "blocked_by"), ("Blocks", "blocks"), ("Related", "related")):
            if r[key]:
                out.append(f"- **{label}:**" + "".join(
                    f"\n  - {ref_md(o)}" + (f", last update {fmt_days(o.get('idle_days'))} ago" if o.get("open") else "")
                    + (" — **blocked too**" if o.get("blocked") else "") for o in r[key]))
        if not (r["blocked_by"] or r["blocks"] or r["related"]):
            out.append("- **Linked issues:** none")
        if r["block_comments"]:
            out.append("- **Comments about the block:**" + "".join(
                f"\n  - {c['day']} {('@' + c['author']) if c.get('author') else ''}: “{one_line(c['body'])}”"
                for c in r["block_comments"]))
        elif r["reason_in_description"]:
            out.append(f"- **Description says:** “{one_line(r['reason_in_description'])}”")
        else:
            out.append(f"- **Comments:** {r['comment_count']}, none about the block")
        if r["last_comment"] and r["last_comment"] not in r["block_comments"]:
            c = r["last_comment"]
            who = f" @{c['author']}" if c.get("author") else ""
            out.append(f"- **Last comment:** {c['day']}{who}: “{one_line(c['body'], 160)}”")
        if r["mentioned_not_linked"]:
            out.append("- **Mentioned, not linked:**" + "".join(f"\n  - {ref_md(m)}" for m in r["mentioned_not_linked"]))
        if r["external_refs"]:
            out.append(f"- **External references:** {', '.join(r['external_refs'])}")
        out.append(f"- **Diagnosis:** {', '.join(r['causes'])}")
        out.append("- **Next steps:**" + "".join(f"\n  1. {s}" for s in r["next_steps"]))
        out.append("")
    if res["stale_excluded"]:
        out.append("### Stale, left out")
        out += [f"- [{s['key']}]({s['url']}) {s['title']} — {s['why']}" for s in res["stale_excluded"]]
        out.append("")
    return "\n".join(out)


def render_periods_md(p):
    kind = p["kind"]
    out = [f"# Open {kind}s — {p['project']}", "", f"| # | {kind.capitalize()} | Start | End | Open issues | Marked blocked |",
           "|---|---|---|---|---|---|"]
    for n, x in enumerate(p["periods"], 1):
        out.append(f"| {n} | {x['name']} | {x['start'] or '—'} | {x['end'] or '—'} | {x['open_issues']} | {x['blocked_label']} |")
    w = p["whole_project"]
    out.append(f"| {len(p['periods']) + 1} | **whole project** | | | {w['open_issues']} | {w['blocked_label']} |")
    if not p["periods"]:
        out += ["", f"No open {kind}s."]
    return "\n".join(out)


def render_projects_md(target, rows):
    key = "Path" if target == "gitlab" else "Identifier"
    out = [f"# Your {'GitLab' if target == 'gitlab' else 'Plane'} projects", "",
           f"| # | {key} | Name | Open issues | Last activity |", "|---|---|---|---|---|"]
    for n, r in enumerate(rows, 1):
        oi = "—" if r["open_issues"] is None else r["open_issues"]
        out.append(f"| {n} | [{r['path']}]({r['url']}) | {r['name']} | {oi} | {r['last_activity'] or '—'} |")
    if not rows:
        out += ["", "You are not a member of any project."]
    return "\n".join(out)


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["projects", "periods", "report"])
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project", help="GitLab: group/project. Plane: project identifier or name "
                                      "(required by periods and report)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--period", help="milestone title (GitLab) or cycle name/id (Plane)")
    scope.add_argument("--all", action="store_true", help="the whole project")
    ap.add_argument("--deep", action="store_true",
                    help="Plane: also read the relations of every open issue, to find issues blocked only by a relation")
    ap.add_argument("--timezone", type=timezone_arg, help="IANA name or +HH:MM: where a day begins and ends")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--stale-days", type=float, default=30, help="idle days after which an issue is stale (0: off)")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL"))
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"))
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"))
    ap.add_argument("--rate", type=int, default=60, help="Plane API calls per minute (default 60)")
    a = ap.parse_args(argv)
    require_config(gitlab=a.target == "gitlab", plane=a.target == "plane", gitlab_url=a.gitlab_url,
                   plane_url=a.plane_url, workspace=a.workspace, scope="read_api")

    chosen_today = dt.date.fromisoformat(a.today) if a.today else None
    try:
        if a.command != "projects" and not a.project:
            sys.exit(f"{a.command} needs --project (run `projects` to list yours)")
        if a.target == "gitlab":
            src = GitLab(a.gitlab_url, os.environ.get("GITLAB_TOKEN"), (a.project or "").strip("/") or None)
        else:
            src = Plane(a.plane_url, a.workspace, os.environ.get("PLANE_API_KEY"), a.project, a.rate)

        if a.command == "projects":
            rows = src.my_projects()
            print(json.dumps(rows, indent=2) if a.format == "json" else render_projects_md(a.target, rows))
            return

        src.set_days(a.timezone, chosen_today)
        src.warn_about_days()
        if a.command == "periods":
            p = src.periods() if a.target == "gitlab" else src.periods(src.today())
            print(json.dumps(p, indent=2) if a.format == "json" else render_periods_md(p))
            return

        if not (a.period or a.all):
            sys.exit("report needs --period <milestone/cycle> or --all (run `periods` to list them)")
        items = src.load(a.period)
        if a.target == "gitlab" and not items and a.period:
            print(f"warning: no open issues in milestone {a.period!r}", file=sys.stderr)
        today = src.today()
        today_dt = age_reference(today, src.tz)
        marked, link_only, stale = find_blocked(src, items, a.deep, today_dt, a.stale_days, a.target)
        issues = [analyze(src, it, today_dt, a.stale_days, True) for it in marked] + \
                 [analyze(src, it, today_dt, a.stale_days, False) for it in link_only]
        issues.sort(key=lambda r: -(r["blocked_days"] or 0))
    except ApiError as e:
        sys.exit(f"API error: {e}")

    res = {"target": a.target, "project": getattr(src, "project", a.project),
           "scope": a.period or "whole project", "today": today.isoformat(), "timezone": str(src.tz), "stale_days": a.stale_days,
           "issues": issues, "stale_excluded": stale}
    print(json.dumps(res, indent=2, default=str) if a.format == "json" else render_md(res))


if __name__ == "__main__":
    main()
