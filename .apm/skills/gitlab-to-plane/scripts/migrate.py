#!/usr/bin/env python3
"""Migrate a GitLab project's work items into a Plane project.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. The agent exports the GitLab project with the GitLab MCP server
into one JSON file, then runs:

    PLANE_API_KEY=... python3 migrate.py --data export.json \
        --plane-url http://localhost:8090 --workspace my-team \
        [--project-name "Inventory Manager"] [--identifier INVM] \
        [--user-map bruno=bruno@acme.io,ana=ana@acme.io] \
        [--no-comments] [--dry-run] [--report report.json]

export.json is either a list of work items or
    {"project": {"path", "name", "description", "url"}, "work_items": [...]}
Each work item may be the raw object returned by get_work_item (createdAt,
closedAt, widgets incl. NOTES) or the compact shape
    {"iid", "title", "description", "state", "created", "closed", "url",
     "labels": [{"title", "color"}] | ["name"], "assignees": ["username"],
     "milestone": {"title", "start_date", "due_date"}, "start_date", "due_date",
     "notes": [{"id", "body", "author", "created", "system"}]}

Mapping (GitLab -> Plane):
  project                          -> project (found by identifier, else created)
  milestone                        -> cycle (same dates), items added to it
  workflow::/status:: label, state -> state (Backlog/Todo/In Progress/In Review/Done/...)
  priority:: label                 -> priority (urgent/high/medium/low)
  other scoped label  scope::value -> label "value" grouped under parent label "scope"
  plain label                      -> label
  assignee                         -> workspace member (by --user-map, email or display name)
  description (Markdown)           -> description (same content, as HTML)
  issue URL                        -> link on the work item ("GitLab group/project#iid")
  non-system note                  -> comment
  created date                     -> created_at (kept)

Re-running is safe: every object carries external_source="gitlab" and an
external_id, and work items are upserted, so nothing is duplicated.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCE = "gitlab"
STATE_SCOPES = {"workflow", "status", "state", "stage"}
PRIORITY_SCOPES = {"priority", "prio"}
PRIORITY_MAP = {
    "urgent": "urgent", "critical": "urgent", "blocker": "urgent", "p0": "urgent", "highest": "urgent",
    "high": "high", "p1": "high", "major": "high",
    "medium": "medium", "normal": "medium", "p2": "medium",
    "low": "low", "minor": "low", "lowest": "low", "trivial": "low", "p3": "low", "p4": "low",
}
# normalized workflow value -> (Plane state name, Plane state group)
STATE_SYNONYMS = {
    "backlog": ("Backlog", "backlog"), "icebox": ("Backlog", "backlog"), "triage": ("Backlog", "backlog"),
    "todo": ("Todo", "unstarted"), "to-do": ("Todo", "unstarted"), "ready": ("Todo", "unstarted"),
    "open": ("Todo", "unstarted"), "selected": ("Todo", "unstarted"),
    "in-progress": ("In Progress", "started"), "doing": ("In Progress", "started"),
    "wip": ("In Progress", "started"), "started": ("In Progress", "started"), "development": ("In Progress", "started"),
    "review": ("In Review", "started"), "in-review": ("In Review", "started"), "code-review": ("In Review", "started"),
    "qa": ("In Review", "started"), "testing": ("In Review", "started"),
    "blocked": ("Blocked", "started"), "on-hold": ("Blocked", "started"),
    "done": ("Done", "completed"), "closed": ("Done", "completed"), "resolved": ("Done", "completed"),
    "canceled": ("Cancelled", "cancelled"), "cancelled": ("Cancelled", "cancelled"),
    "wontfix": ("Cancelled", "cancelled"), "won't-fix": ("Cancelled", "cancelled"), "duplicate": ("Cancelled", "cancelled"),
}
GROUP_COLORS = {"backlog": "#A3A3A3", "unstarted": "#3A3A3A", "started": "#F59E0B",
                "completed": "#16A34A", "cancelled": "#EF4444"}


def slug(value):
    return re.sub(r"[\s_]+", "-", str(value).strip().lower())


def split_scope(label):
    if "::" not in label:
        return None, label
    scope, value = label.rsplit("::", 1)
    return scope.strip(), value.strip()


# ------------------------------------------------------------------ markdown
def md_inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def md_to_html(md):
    """Small Markdown subset: headings, lists, task lists, quotes, code fences, inline marks."""
    if not md:
        return ""
    out, para, list_tag, in_code, code = [], [], None, False, []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(md_inline(l) for l in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in md.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code, in_code = [], False
            else:
                flush_para(); close_list(); in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        s = line.strip()
        if not s:
            flush_para(); close_list(); continue
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            flush_para(); close_list()
            level = min(len(m.group(1)), 3)
            out.append(f"<h{level}>{md_inline(m.group(2))}</h{level}>")
            continue
        m = re.match(r"[-*+]\s+\[( |x|X)\]\s+(.*)", s)
        if m:
            flush_para()
            if list_tag != "ul":
                close_list(); out.append("<ul>"); list_tag = "ul"
            mark = "☑" if m.group(1).lower() == "x" else "☐"
            out.append(f"<li>{mark} {md_inline(m.group(2))}</li>")
            continue
        m = re.match(r"([-*+]|\d+[.)])\s+(.*)", s)
        if m:
            flush_para()
            tag = "ol" if m.group(1)[0].isdigit() else "ul"
            if list_tag != tag:
                close_list(); out.append(f"<{tag}>"); list_tag = tag
            out.append(f"<li>{md_inline(m.group(2))}</li>")
            continue
        if s.startswith(">"):
            flush_para(); close_list()
            out.append(f"<blockquote><p>{md_inline(s.lstrip('> '))}</p></blockquote>")
            continue
        close_list()
        para.append(s)
    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush_para(); close_list()
    return "".join(out)


# ------------------------------------------------------------------ input
def norm_item(raw, project_path):
    labels, assignees, milestone, notes = raw.get("labels"), raw.get("assignees"), raw.get("milestone"), raw.get("notes")
    start_date, due_date = raw.get("start_date"), raw.get("due_date")
    for w in raw.get("widgets") or []:
        t = w.get("type")
        if t == "LABELS" and labels is None:
            labels = (w.get("labels") or {}).get("nodes", [])
        elif t == "ASSIGNEES" and assignees is None:
            assignees = (w.get("assignees") or {}).get("nodes", [])
        elif t == "MILESTONE" and milestone is None:
            milestone = w.get("milestone")
        elif t == "NOTES" and notes is None:
            notes = (w.get("notes") or {}).get("nodes", [])
        elif t == "START_AND_DUE_DATE":
            start_date = start_date or w.get("startDate")
            due_date = due_date or w.get("dueDate")
    lab = []
    for l in labels or []:
        if isinstance(l, dict):
            lab.append({"title": l.get("title") or l.get("name"), "color": l.get("color")})
        else:
            lab.append({"title": l, "color": None})
    if isinstance(milestone, str):
        milestone = {"title": milestone}
    if milestone:
        milestone = {"title": milestone.get("title"),
                     "start_date": milestone.get("start_date") or milestone.get("startDate"),
                     "due_date": milestone.get("due_date") or milestone.get("dueDate")}
    nts = []
    for n in notes or []:
        author = n.get("author")
        if isinstance(author, dict):
            author = author.get("username") or author.get("name")
        nts.append({"id": str(n.get("id")), "body": n.get("body") or "", "author": author,
                    "created": n.get("created") or n.get("createdAt"), "system": bool(n.get("system"))})
    iid = str(raw.get("iid"))
    ref = raw.get("reference") or f"{project_path}#{iid}"
    return {
        "iid": iid,
        "ref": ref,
        "title": raw.get("title") or f"#{iid}",
        "description": raw.get("description") or "",
        "closed": str(raw.get("state") or "").lower() == "closed" or bool(raw.get("closed") or raw.get("closedAt")),
        "created": raw.get("created") or raw.get("createdAt"),
        "closed_at": raw.get("closed") or raw.get("closedAt"),
        "url": raw.get("url") or raw.get("webUrl"),
        "labels": [l for l in lab if l["title"]],
        "assignees": [a.get("username") if isinstance(a, dict) else a for a in (assignees or []) if a],
        "milestone": milestone if milestone and milestone.get("title") else None,
        "start_date": (start_date or "")[:10] or None,
        "due_date": (due_date or "")[:10] or None,
        "notes": [n for n in nts if not n["system"] and n["body"].strip()],
    }


# ------------------------------------------------------------------ Plane client
class PlaneError(Exception):
    def __init__(self, status, body, method, path):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


class Plane:
    def __init__(self, base_url, api_key, workspace, rate_per_min=60, dry_run=False):
        self.base = base_url.rstrip("/") + f"/api/v1/workspaces/{workspace}"
        self.key, self.dry_run = api_key, dry_run
        self.min_interval = 60.0 / max(1, rate_per_min - 2)
        self.last = 0.0
        self.calls = 0

    def _request(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(8):
            wait = self.min_interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method, headers={
                "X-API-Key": self.key, "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "gitlab-to-plane-skill"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode() or "null"
                    return resp.status, json.loads(raw)
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code == 429:
                    reset = e.headers.get("X-RateLimit-Reset") or e.headers.get("Retry-After")
                    try:
                        delay = float(reset) - time.time() if reset and float(reset) > 1e9 else float(reset or 10)
                    except ValueError:
                        delay = 10
                    time.sleep(min(max(delay, 1), 65))
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
                err = PlaneError(e.code, text, method, path)
                err.payload = payload
                raise err
        raise PlaneError(429, "rate limited too many times", method, path)

    def get(self, path, params=None):
        status, data = self._request("GET", path, params=params)
        return data

    def list(self, path, params=None):
        """GET a list endpoint, following cursor pagination when present."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items = []
        while True:
            data = self.get(path, params)
            if isinstance(data, list):
                return items + data
            items += data.get("results") or []
            if data.get("next_page_results") and data.get("next_cursor"):
                params["cursor"] = data["next_cursor"]
            else:
                return items

    def write(self, method, path, body):
        if self.dry_run:
            return {"id": f"<new:{path}>", **body}
        return self._request(method, path, body=body)[1]


# ------------------------------------------------------------------ migration
class Migration:
    def __init__(self, plane, args, project_info, items):
        self.p, self.args, self.info, self.items = plane, args, project_info, items
        self.log = []
        self.report = {"project": None, "created": {}, "reused": {}, "items": {}, "unmapped_assignees": {},
                       "warnings": []}

    def say(self, msg):
        print(msg, flush=True)

    def count(self, kind, created):
        bucket = self.report["created" if created else "reused"]
        bucket[kind] = bucket.get(kind, 0) + 1

    # -- project
    def project(self):
        ident = self.args.identifier
        name = self.args.project_name or self.info.get("name") or self.info.get("path", "GitLab project")
        needs_cycles = any(i["milestone"] for i in self.items)
        for proj in self.p.list("/projects/"):
            if proj.get("identifier", "").upper() == ident.upper():
                self.say(f"Project: reusing {proj['name']} ({proj['identifier']})")
                self.count("project", False)
                break
        else:
            desc = self.info.get("description") or ""
            if self.info.get("url"):
                desc = (desc + "\n\n" if desc else "") + f"Migrated from GitLab: {self.info['url']}"
            self.say(f"Project: creating {name} ({ident})")
            self.count("project", True)
            proj = self.p.write("POST", "/projects/", {"name": name, "identifier": ident, "description": desc})
        # New Plane projects start with cycles disabled; milestones need them.
        if needs_cycles and not proj.get("cycle_view"):
            self.p.write("PATCH", f"/projects/{proj['id']}/", {"cycle_view": True})
            proj["cycle_view"] = True
        return proj

    # -- states
    def states(self, pid):
        existing = {} if str(pid).startswith("<new") else {slug(s["name"]): s for s in self.p.list(f"/projects/{pid}/states/")}
        wanted = {}
        for it in self.items:
            wanted[it["iid"]] = self.target_state(it)
        resolved = {}
        for name, group in sorted(set(wanted.values())):
            s = existing.get(slug(name))
            if not s and str(pid).startswith("<new") and name in ("Backlog", "Todo", "In Progress", "Done", "Cancelled"):
                s = {"id": f"<default:{name}>", "name": name}
            if not s:
                body = {"name": name, "group": group, "color": GROUP_COLORS[group],
                        "external_source": SOURCE, "external_id": f"state:{slug(name)}"}
                try:
                    s = self.p.write("POST", f"/projects/{pid}/states/", body)
                    self.count("state", True)
                except PlaneError as e:
                    if e.status != 409:
                        raise
                    s = next(x for x in self.p.list(f"/projects/{pid}/states/") if slug(x["name"]) == slug(name))
            else:
                self.count("state", False)
            resolved[(name, group)] = s["id"]
        self.say("States: " + ", ".join(n for n, _ in sorted(resolved)))
        return {iid: resolved[w] for iid, w in wanted.items()}

    def target_state(self, it):
        values = [split_scope(l["title"])[1] for l in it["labels"]
                  if (split_scope(l["title"])[0] or "").lower() in STATE_SCOPES]
        value = slug(values[0]) if values else None
        if it["closed"]:
            if value and STATE_SYNONYMS.get(value, ("", ""))[1] == "cancelled":
                return STATE_SYNONYMS[value]
            return ("Done", "completed")
        if not value:
            return ("Backlog", "backlog")
        if value in STATE_SYNONYMS and STATE_SYNONYMS[value][1] not in ("completed", "cancelled"):
            return STATE_SYNONYMS[value]
        if value in STATE_SYNONYMS:  # open item labelled done/cancelled: keep it open, in progress
            return ("In Progress", "started")
        return (values[0].replace("-", " ").title(), "started")

    # -- labels
    def labels(self, pid):
        plain, scoped, colors = set(), {}, {}
        for it in self.items:
            for l in it["labels"]:
                scope, value = split_scope(l["title"])
                if scope and scope.lower() in STATE_SCOPES | PRIORITY_SCOPES:
                    continue
                colors[l["title"]] = l.get("color")
                if scope:
                    scoped.setdefault(scope, set()).add(value)
                else:
                    plain.add(value)
        # child label names must be unique per project: fall back to "scope::value" on clashes
        value_count = {}
        for scope, values in scoped.items():
            for v in values:
                value_count[v.lower()] = value_count.get(v.lower(), 0) + 1
        taken = {n.lower() for n in plain} | {s.lower() for s in scoped}
        existing = {} if str(pid).startswith("<new") else {l["name"].lower(): l for l in self.p.list(f"/projects/{pid}/labels/")}
        ids = {}

        def ensure(name, color, parent=None, ext=None):
            l = existing.get(name.lower())
            if l:
                self.count("label", False)
                return l["id"]
            body = {"name": name, "color": color or "#6B7280"}
            if parent:
                body["parent"] = parent
            if ext:
                body.update({"external_source": SOURCE, "external_id": ext})
            try:
                l = self.p.write("POST", f"/projects/{pid}/labels/", body)
            except PlaneError as e:
                if e.status not in (400, 409):
                    raise
                body.pop("external_source", None); body.pop("external_id", None)
                l = self.p.write("POST", f"/projects/{pid}/labels/", body)
            existing[name.lower()] = l
            self.count("label", True)
            return l["id"]

        for name in sorted(plain):
            ids[name] = ensure(name, colors.get(name), ext=f"label:{name}")
        for scope in sorted(scoped):
            parent = ensure(scope, None, ext=f"label-scope:{scope}")
            for v in sorted(scoped[scope]):
                full = f"{scope}::{v}"
                child = full if value_count[v.lower()] > 1 or v.lower() in taken else v
                ids[full] = ensure(child, colors.get(full), parent=parent, ext=f"label:{full}")
        self.say(f"Labels: {len(ids)} mapped ({len(plain)} plain, {sum(len(v) for v in scoped.values())} scoped in {len(scoped)} groups)")
        return ids

    # -- members
    def members(self, pid):
        usernames = sorted({a for it in self.items for a in it["assignees"]})
        if not usernames:
            return {}
        explicit = {}
        for pair in (self.args.user_map or "").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                explicit[k.strip().lstrip("@").lower()] = v.strip().lower()
        ws = self.p.list("/members/")
        by_email = {(m.get("email") or "").lower(): m for m in ws}
        by_name = {(m.get("display_name") or "").lower(): m for m in ws}
        by_local = {(m.get("email") or "").split("@")[0].lower(): m for m in ws}
        project_members = set() if str(pid).startswith("<new") else {
            m.get("id") or m.get("member") for m in self.p.list(f"/projects/{pid}/project-members/")}
        mapped = {}
        for u in usernames:
            key = u.lower()
            m = by_email.get(explicit.get(key, "")) or by_name.get(key) or by_local.get(key)
            if not m:
                self.report["unmapped_assignees"][u] = "no matching Plane workspace member"
                continue
            if m["id"] not in project_members:
                try:
                    self.p.write("POST", f"/projects/{pid}/project-members/", {"member": m["id"], "role": 15})
                    project_members.add(m["id"])
                except PlaneError as e:
                    self.report["warnings"].append(f"could not add {m.get('email')} to the project: {e}")
                    continue
            mapped[u] = m["id"]
        self.say(f"Assignees: {len(mapped)}/{len(usernames)} mapped to Plane members"
                 + (f"; not found: {', '.join(sorted(self.report['unmapped_assignees']))}" if self.report["unmapped_assignees"] else ""))
        return mapped

    # -- cycles
    def cycles(self, pid):
        ms = {}
        for it in self.items:
            if it["milestone"]:
                ms[it["milestone"]["title"]] = it["milestone"]
        if not ms:
            return {}
        existing = [] if str(pid).startswith("<new") else self.p.list(f"/projects/{pid}/cycles/")
        ids = {}
        for title, m in sorted(ms.items(), key=lambda kv: kv[1].get("start_date") or ""):
            ext = f"milestone:{title}"
            c = next((c for c in existing if c.get("external_id") == ext or c.get("name") == title), None)
            if c:
                self.count("cycle", False)
            else:
                body = {"name": title, "external_source": SOURCE, "external_id": ext}
                if m.get("start_date") and m.get("due_date"):
                    body.update({"start_date": m["start_date"][:10], "end_date": m["due_date"][:10]})
                c = self.p.write("POST", f"/projects/{pid}/cycles/", body)
                self.count("cycle", True)
            ids[title] = c["id"]
        self.say(f"Cycles: {', '.join(ids)}")
        return ids

    # -- work items
    def link(self, pid, issue_id, it):
        """Attach a link back to the GitLab issue (skipped when it already exists)."""
        if not it["url"] or self.p.dry_run or str(issue_id).startswith("<new"):
            if it["url"]:
                self.count("link", True)
            return
        base = f"/projects/{pid}/work-items/{issue_id}/links/"
        if any(l.get("url") == it["url"] for l in self.p.list(base)):
            self.count("link", False)
            return
        self.p.write("POST", base, {"url": it["url"], "title": f"GitLab {it['ref']}"})
        self.count("link", True)

    def work_items(self, pid, states, labels, members, cycles):
        cycle_items = {}
        total = len(self.items)
        for n, it in enumerate(sorted(self.items, key=lambda i: int(i["iid"]) if i["iid"].isdigit() else 0), 1):
            prio = "none"
            for l in it["labels"]:
                scope, value = split_scope(l["title"])
                if scope and scope.lower() in PRIORITY_SCOPES:
                    prio = PRIORITY_MAP.get(value.lower(), "none")
            body = {
                "name": it["title"][:255],
                "description_html": md_to_html(it["description"]),
                "state": states[it["iid"]],
                "priority": prio,
                "labels": [labels[l["title"]] for l in it["labels"] if l["title"] in labels],
                "assignees": [members[a] for a in it["assignees"] if a in members],
                "external_source": SOURCE,
                "external_id": it["ref"],
            }
            if it["created"]:
                body["created_at"] = it["created"]
            if it["start_date"]:
                body["start_date"] = it["start_date"]
            if it["due_date"]:
                body["target_date"] = it["due_date"]
            if self.p.dry_run or str(pid).startswith("<new"):
                issue = {"id": f"<new:{it['ref']}>"}
                self.count("work_item", True)
            else:
                # Upsert: POST answers 409 with the existing id when the external_id is known.
                try:
                    issue = self.p.write("POST", f"/projects/{pid}/work-items/", body)
                    self.count("work_item", True)
                except PlaneError as e:
                    existing_id = (getattr(e, "payload", None) or {}).get("id")
                    if e.status != 409 or not existing_id:
                        raise
                    body.pop("created_at", None)
                    issue = self.p.write("PATCH", f"/projects/{pid}/work-items/{existing_id}/", body)
                    self.count("work_item", False)
            self.report["items"][it["ref"]] = {"plane_id": issue.get("id"), "sequence_id": issue.get("sequence_id")}
            if it["milestone"] and it["milestone"]["title"] in cycles:
                cycle_items.setdefault(cycles[it["milestone"]["title"]], []).append(issue["id"])
            self.link(pid, issue["id"], it)
            if it["notes"] and not self.args.no_comments:
                self.comments(pid, issue["id"], it)
            print(f"  [{n}/{total}] {it['ref']} -> {issue.get('sequence_id') or issue['id']}", flush=True)
        for cid, issue_ids in cycle_items.items():
            if not self.p.dry_run and not str(cid).startswith("<new"):
                self.p.write("POST", f"/projects/{pid}/cycles/{cid}/cycle-issues/", {"issues": issue_ids})
        self.say(f"Cycle membership: {sum(len(v) for v in cycle_items.values())} items in {len(cycle_items)} cycles")

    def comments(self, pid, issue_id, it):
        if self.p.dry_run or str(issue_id).startswith("<new"):
            self.count("comment", True)
            return
        existing = {c.get("external_id") for c in self.p.list(f"/projects/{pid}/work-items/{issue_id}/comments/")}
        for note in it["notes"]:
            if note["id"] in existing:
                self.count("comment", False)
                continue
            head = f"<p><em>@{html.escape(note['author'] or '?')} on GitLab, {html.escape((note['created'] or '')[:16].replace('T', ' '))}</em></p>"
            self.p.write("POST", f"/projects/{pid}/work-items/{issue_id}/comments/", {
                "comment_html": head + md_to_html(note["body"]),
                "external_source": SOURCE, "external_id": note["id"]})
            self.count("comment", True)

    def run(self):
        proj = self.project()
        pid = proj["id"]
        self.report["project"] = {"id": pid, "identifier": self.args.identifier, "name": proj.get("name")}
        states = self.states(pid)
        labels = self.labels(pid)
        members = self.members(pid)
        cycles = self.cycles(pid)
        self.work_items(pid, states, labels, members, cycles)
        return self.report


def default_identifier(info):
    words = re.findall(r"[A-Za-z0-9]+", (info.get("path") or info.get("name") or "PRJ").split("/")[-1])
    if not words:
        return "PRJ"
    ident = words[0][:3] + "".join(w[0] for w in words[1:])
    return ident.upper()[:12]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="export JSON written by the agent")
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"), help="Plane base URL (or PLANE_URL)")
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"), help="workspace slug (or PLANE_WORKSPACE)")
    ap.add_argument("--project-name", help="Plane project name (default: GitLab project name)")
    ap.add_argument("--identifier", help="Plane project identifier, e.g. INVM (default: derived from the path)")
    ap.add_argument("--user-map", help="gitlab_user=plane_email pairs, comma separated")
    ap.add_argument("--no-comments", action="store_true", help="do not migrate GitLab comments")
    ap.add_argument("--dry-run", action="store_true", help="read Plane, print the plan, write nothing")
    ap.add_argument("--rate", type=int, default=60, help="API calls per minute allowed for the key (default 60)")
    ap.add_argument("--report", help="write the GitLab -> Plane id mapping and summary to this JSON file")
    a = ap.parse_args(argv)

    key = os.environ.get("PLANE_API_KEY")
    if not a.plane_url or not a.workspace:
        sys.exit("--plane-url and --workspace (or PLANE_URL / PLANE_WORKSPACE) are required")
    if not key:
        sys.exit("PLANE_API_KEY is not set (Plane: Workspace settings -> API tokens)")

    with open(a.data, encoding="utf-8") as fh:
        data = json.load(fh)
    info = data.get("project", {}) if isinstance(data, dict) else {}
    raw_items = data.get("work_items", []) if isinstance(data, dict) else data
    path = info.get("path") or (raw_items[0].get("reference", "").split("#")[0] if raw_items else "")
    info.setdefault("path", path)
    items = [norm_item(r, path) for r in raw_items]
    if not items:
        sys.exit("no work items in the export")
    a.identifier = (a.identifier or default_identifier(info)).upper()

    plane = Plane(a.plane_url, key, a.workspace, a.rate, a.dry_run)
    print(f"{'DRY RUN — nothing will be written. ' if a.dry_run else ''}"
          f"GitLab {path}: {len(items)} work items -> Plane {a.plane_url} workspace '{a.workspace}', project {a.identifier}")
    try:
        report = Migration(plane, a, info, items).run()
    except PlaneError as e:
        sys.exit(f"Plane API error: {e}")
    report["api_calls"] = plane.calls
    print("\nSummary")
    print("  created: " + (", ".join(f"{k} {v}" for k, v in report["created"].items()) or "-"))
    print("  reused:  " + (", ".join(f"{k} {v}" for k, v in report["reused"].items()) or "-"))
    if report["unmapped_assignees"]:
        print("  assignees not found in Plane (left unassigned): " + ", ".join(sorted(report["unmapped_assignees"])))
    for w in report["warnings"]:
        print("  warning: " + w)
    print("  Plane sets its own completion date; the GitLab link on each item leads to the original history")
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"  report: {a.report}")


if __name__ == "__main__":
    main()
