#!/usr/bin/env python3
"""Populate a GitLab or Plane project with a standard label set and a Kanban board.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode.

    GITLAB_TOKEN=... python3 populate.py --target gitlab --project acme/shop \
        --labels labels.json [--gitlab-url http://localhost:8929] [--board-name Kanban] [--dry-run]

    PLANE_API_KEY=... python3 populate.py --target plane --project "Shop" \
        --labels labels.json [--plane-url http://localhost:8090] [--workspace my-team] \
        [--create-project] [--identifier SHOP] [--priority-labels] [--dry-run]

labels.json is a list of labels, or {"labels": [...]}, each one
    {"name": "scope::value", "color": "#rrggbb", "description": "..."}
Scoped names (scope::value) are the convention. Labels in the "workflow" scope
define the Kanban columns, in the order they appear in the file.

GitLab
  - labels are created as project labels (existing project or group labels with
    the same name are reused, never modified);
  - a board (default name "Kanban") is reused when one with that name exists,
    otherwise created, and gets one list per workflow label, in file order.
    GitLab keeps its own Open and Closed lists. If GitLab refuses to create the
    board, the script stops with the error; it never falls back to another board.

Plane
  - the Kanban board of a Plane project is grouped by state, so workflow labels
    become states (backlog -> Backlog, ready -> Todo, in-progress -> In Progress,
    review -> In Review, blocked -> Blocked, done -> Done; others keep their name
    in the "started" group). Existing states are reused, never deleted;
  - priority labels map to Plane's built-in priority field, so nothing is created
    for them unless --priority-labels is given;
  - other scoped labels become a parent label (the scope) with child labels (the
    values); plain labels stay plain.

Re-running is safe: whatever already exists is reused, so nothing is duplicated.
--dry-run reads the target and prints the plan without writing anything.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "populate-project-skill"
WORKFLOW_SCOPES = {"workflow", "status", "state", "stage"}
PRIORITY_SCOPES = {"priority", "prio"}
# normalized workflow value -> (Plane state name, Plane state group). Same mapping as gitlab-to-plane.
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
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def slug(value):
    return re.sub(r"[\s_]+", "-", str(value).strip().lower())


def split_scope(name):
    if "::" in name:
        scope, value = name.rsplit("::", 1)
        return scope.strip(), value.strip()
    return None, name.strip()


def load_labels(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("labels") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        sys.exit(f"{path}: expected a non-empty list of labels or {{\"labels\": [...]}}")
    labels, seen = [], set()
    for i, l in enumerate(raw):
        if isinstance(l, str):
            l = {"name": l}
        name = (l.get("name") or "").strip()
        if not name:
            sys.exit(f"{path}: label #{i + 1} has no name")
        if name.lower() in seen:
            sys.exit(f"{path}: duplicate label {name!r}")
        seen.add(name.lower())
        color = (l.get("color") or "#6B7280").strip()
        if not HEX.match(color):
            sys.exit(f"{path}: label {name!r} has invalid color {color!r} (use #rrggbb)")
        scope, value = split_scope(name)
        labels.append({"name": name, "color": color, "description": (l.get("description") or "").strip(),
                       "scope": scope, "value": value})
    return labels


def is_workflow(l):
    return (l["scope"] or "").lower() in WORKFLOW_SCOPES


def is_priority(l):
    return (l["scope"] or "").lower() in PRIORITY_SCOPES


class ApiError(Exception):
    def __init__(self, status, body, method, url):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA, **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode() or "null"
            return resp.status, json.loads(raw), resp.headers
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"), method, url) from None
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {url}: {e.reason}")


class Plan:
    """Collects what was (or would be) done, prints it and builds the report."""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.created, self.reused, self.notes = [], [], []

    def create(self, kind, name):
        self.created.append({"kind": kind, "name": name})
        print(f"  + {'would create' if self.dry_run else 'created'} {kind}: {name}", flush=True)

    def reuse(self, kind, name, why="already exists"):
        self.reused.append({"kind": kind, "name": name})
        print(f"  = {kind}: {name} ({why})", flush=True)

    def note(self, msg):
        self.notes.append(msg)
        print(f"  ! {msg}", flush=True)


# ------------------------------------------------------------------ GitLab
class GitLab:
    def __init__(self, url, token):
        self.base = url.rstrip("/") + "/api/v4"
        self.headers = {"PRIVATE-TOKEN": token}

    def req(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return http(method, url, self.headers, body)[1]

    def pages(self, path, params=None):
        params, items, page = dict(params or {}), [], 1
        while True:
            params.update(per_page=100, page=page)
            url = self.base + path + "?" + urllib.parse.urlencode(params)
            _, data, headers = http("GET", url, self.headers)
            items += data or []
            nxt = headers.get("X-Next-Page")
            if not nxt:
                return items
            page = int(nxt)


def populate_gitlab(args, labels, plan):
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        sys.exit("GITLAB_TOKEN is not set (GitLab: User settings -> Access tokens, scope 'api')")
    if not args.gitlab_url:
        sys.exit("--gitlab-url (or GITLAB_URL) is required")
    gl = GitLab(args.gitlab_url, token)
    ref = args.project if args.project.isdigit() else urllib.parse.quote(args.project, safe="")
    try:
        proj = gl.req("GET", f"/projects/{ref}")
    except ApiError as e:
        if e.status in (401, 403):
            sys.exit(f"GitLab refused the token (HTTP {e.status}); it needs the 'api' scope and access to {args.project}")
        if e.status == 404:
            sys.exit(f"GitLab project {args.project!r} not found (use group/project or the numeric id)")
        raise
    pid = proj["id"]
    print(f"GitLab project: {proj['path_with_namespace']} (id {pid}) {proj.get('web_url', '')}")

    print("Labels:")
    existing = {l["name"].lower(): l for l in gl.pages(f"/projects/{pid}/labels", {"include_ancestor_groups": "true"})}
    ids = {}
    for l in labels:
        cur = existing.get(l["name"].lower())
        if cur:
            where = "group label" if cur.get("is_project_label") is False else "already exists"
            if cur.get("color", "").lower() != l["color"].lower():
                where += f", color kept as {cur.get('color')}"
            plan.reuse("label", l["name"], where)
            ids[l["name"]] = cur["id"]
            continue
        plan.create("label", l["name"])
        if not args.dry_run:
            body = {"name": l["name"], "color": l["color"]}
            if l["description"]:
                body["description"] = l["description"]
            ids[l["name"]] = gl.req("POST", f"/projects/{pid}/labels", body)["id"]

    workflow = [l for l in labels if is_workflow(l)]
    print(f"Kanban board ({args.board_name}):")
    if not workflow:
        plan.note("no workflow labels in the set, so no board columns were created")
        return {"target": "gitlab", "project": proj["path_with_namespace"], "url": proj.get("web_url")}
    boards = gl.req("GET", f"/projects/{pid}/boards") or []
    board = next((b for b in boards if (b.get("name") or "").lower() == args.board_name.lower()), None)
    if board:
        plan.reuse("board", board["name"])
    else:
        if not args.dry_run:
            try:
                board = gl.req("POST", f"/projects/{pid}/boards", {"name": args.board_name})
            except ApiError as e:
                sys.exit(f"GitLab did not allow creating the board {args.board_name!r} "
                         f"(HTTP {e.status}: {e.body[:300]}). The labels were created; no board lists were added.")
        plan.create("board", args.board_name)
    in_board = {((x.get("label") or {}).get("name") or "").lower() for x in (board or {}).get("lists") or []}
    for l in workflow:
        if l["name"].lower() in in_board:
            plan.reuse("board list", l["name"])
            continue
        plan.create("board list", l["name"])
        if not args.dry_run:
            gl.req("POST", f"/projects/{pid}/boards/{board['id']}/lists", {"label_id": ids[l["name"]]})
    board_url = proj.get("web_url", "") + "/-/boards" + (f"/{board['id']}" if board and board.get("id") else "")
    print(f"Board URL: {board_url}")
    return {"target": "gitlab", "project": proj["path_with_namespace"], "url": proj.get("web_url"),
            "board_url": board_url}


# ------------------------------------------------------------------ Plane
class Plane:
    def __init__(self, base_url, api_key, workspace, rate_per_min=60, dry_run=False):
        self.base = base_url.rstrip("/") + f"/api/v1/workspaces/{workspace}"
        self.headers = {"X-API-Key": api_key}
        self.dry_run = dry_run
        self.min_interval = 60.0 / max(1, rate_per_min - 2)
        self.last = 0.0

    def req(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(8):
            wait = self.min_interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                return http(method, url, self.headers, body)[1]
            except ApiError as e:
                if e.status == 429:
                    time.sleep(10)
                    continue
                if e.status >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise ApiError(429, "rate limited too many times", method, url)

    def list(self, path):
        params, items = {"per_page": 100}, []
        while True:
            data = self.req("GET", path, params=params)
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
        return self.req(method, path, body)


def default_identifier(name):
    words = re.findall(r"[A-Za-z0-9]+", name)
    ident = "".join(w[0] for w in words) if len(words) > 1 else (words[0] if words else "PRJ")
    return ident.upper()[:5] or "PRJ"


def plane_state_for(value):
    v = slug(value)
    if v in STATE_SYNONYMS:
        return STATE_SYNONYMS[v]
    return (value.replace("-", " ").strip().title(), "started")


def populate_plane(args, labels, plan):
    key = os.environ.get("PLANE_API_KEY")
    if not key:
        sys.exit("PLANE_API_KEY is not set (Plane: Workspace settings -> API tokens)")
    if not args.plane_url or not args.workspace:
        sys.exit("--plane-url and --workspace (or PLANE_URL / PLANE_WORKSPACE) are required")
    p = Plane(args.plane_url, key, args.workspace, args.rate, args.dry_run)
    try:
        projects = p.list("/projects/")
    except ApiError as e:
        if e.status in (401, 403):
            sys.exit(f"Plane refused the API key (HTTP {e.status})")
        if e.status == 404:
            sys.exit(f"Plane workspace {args.workspace!r} not found")
        raise
    want = args.project.strip().lower()
    matches = [x for x in projects if x.get("identifier", "").lower() == want or x.get("name", "").lower() == want]
    if len(matches) > 1:
        sys.exit("several Plane projects match {!r}: {}".format(
            args.project, ", ".join(f"{x['name']} ({x['identifier']})" for x in matches)))
    if matches:
        proj = matches[0]
        print(f"Plane project: {proj['name']} ({proj['identifier']})")
    elif args.create_project:
        ident = (args.identifier or default_identifier(args.project)).upper()
        print(f"Plane project: {args.project} ({ident})")
        plan.create("project", f"{args.project} ({ident})")
        proj = p.write("POST", "/projects/", {"name": args.project, "identifier": ident})
    else:
        names = ", ".join(f"{x['name']} ({x['identifier']})" for x in projects) or "none"
        print(f"Plane project {args.project!r} not found in workspace {args.workspace!r}. Existing: {names}",
              file=sys.stderr)
        sys.exit(3)
    pid = proj["id"]
    new_project = str(pid).startswith("<new")

    # -- workflow -> states (the Kanban columns)
    workflow = [l for l in labels if is_workflow(l)]
    print("Kanban columns (states):")
    if not workflow:
        plan.note("no workflow labels in the set, so no states were created")
    if new_project:
        # a new project comes with these default states
        states = [{"name": n, "group": g, "sequence": i * 1000}
                  for i, (n, g) in enumerate([("Backlog", "backlog"), ("Todo", "unstarted"),
                                              ("In Progress", "started"), ("Done", "completed"),
                                              ("Cancelled", "cancelled")])]
    else:
        states = p.list(f"/projects/{pid}/states/")
    by_slug = {slug(s["name"]): s for s in states}
    used = set()
    for l in workflow:
        name, group = plane_state_for(l["value"])
        cur = by_slug.get(slug(l["value"])) or by_slug.get(slug(name))
        if cur:
            plan.reuse("state", cur["name"], f"for {l['name']}")
            used.add(slug(cur["name"]))
            continue
        same_group = [s.get("sequence") or 0 for s in states if s.get("group") == group]
        seq = (max(same_group) if same_group else max([s.get("sequence") or 0 for s in states] or [0])) + 1000
        plan.create("state", f"{name} [{group}] for {l['name']}")
        body = {"name": name, "group": group, "color": GROUP_COLORS[group], "sequence": seq}
        if l["description"]:
            body["description"] = l["description"]
        s = p.write("POST", f"/projects/{pid}/states/", body)
        s.setdefault("group", group)
        s.setdefault("sequence", seq)
        states.append(s)
        by_slug[slug(name)] = s
        used.add(slug(name))
    extra = [s["name"] for s in states if slug(s["name"]) not in used]
    if workflow and extra:
        plan.note("states not in the workflow were kept (delete them in Plane if unwanted): " + ", ".join(extra))

    # -- priority
    rest = [l for l in labels if not is_workflow(l)]
    if not args.priority_labels and any(is_priority(l) for l in rest):
        plan.note("priority labels map to Plane's built-in Priority field (Urgent/High/Medium/Low); "
                  "no labels created for them")
        rest = [l for l in rest if not is_priority(l)]

    # -- labels: scope -> parent label, value -> child label
    print("Labels:")
    existing = {} if new_project else {l["name"].lower(): l for l in p.list(f"/projects/{pid}/labels/")}
    scopes = {}
    for l in rest:
        if l["scope"]:
            scopes.setdefault(l["scope"], []).append(l)
    value_count = {}
    for items in scopes.values():
        for l in items:
            value_count[l["value"].lower()] = value_count.get(l["value"].lower(), 0) + 1
    taken = {l["value"].lower() for l in rest if not l["scope"]} | {s.lower() for s in scopes}

    def ensure(name, color, description="", parent=None):
        cur = existing.get(name.lower())
        if cur:
            plan.reuse("label", name)
            return cur["id"]
        plan.create("label", name + (f" (in {parent[1]})" if parent else ""))
        body = {"name": name, "color": color}
        if description:
            body["description"] = description
        if parent:
            body["parent"] = parent[0]
        made = p.write("POST", f"/projects/{pid}/labels/", body)
        existing[name.lower()] = made
        return made["id"]

    for l in rest:
        if not l["scope"]:
            ensure(l["value"], l["color"], l["description"])
    for scope, items in scopes.items():
        parent_id = ensure(scope, "#6B7280")
        for l in items:
            v = l["value"]
            child = l["name"] if value_count[v.lower()] > 1 or v.lower() in taken else v
            ensure(child, l["color"], l["description"], parent=(parent_id, scope))

    url = f"{args.plane_url.rstrip('/')}/{args.workspace}/projects/{pid}/issues"
    print(f"Board URL: {url} (choose the Board layout, grouped by state)")
    return {"target": "plane", "project": f"{proj.get('name')} ({proj.get('identifier')})", "url": url}


def gitlab_url_from_mcp():
    """Derive the GitLab base URL from a gitlab MCP entry in .mcp.json, if any."""
    try:
        with open(".mcp.json", encoding="utf-8") as f:
            url = json.load(f)["mcpServers"]["gitlab"]["url"]
        return re.sub(r"/api/v4/mcp/?$", "", url)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, choices=["gitlab", "plane"])
    ap.add_argument("--project", required=True,
                    help="GitLab: group/project or numeric id. Plane: project name or identifier")
    ap.add_argument("--labels", required=True, help="JSON file with the label set")
    ap.add_argument("--gitlab-url", default=os.environ.get("GITLAB_URL") or gitlab_url_from_mcp(),
                    help="GitLab base URL (or GITLAB_URL; default: derived from .mcp.json)")
    ap.add_argument("--board-name", default="Kanban", help="GitLab board name (default Kanban)")
    ap.add_argument("--plane-url", default=os.environ.get("PLANE_URL"), help="Plane base URL (or PLANE_URL)")
    ap.add_argument("--workspace", default=os.environ.get("PLANE_WORKSPACE"), help="Plane workspace slug (or PLANE_WORKSPACE)")
    ap.add_argument("--create-project", action="store_true", help="Plane: create the project when it does not exist")
    ap.add_argument("--identifier", help="Plane: identifier for a created project (default: derived from the name)")
    ap.add_argument("--priority-labels", action="store_true",
                    help="Plane: also create priority labels instead of relying on the built-in Priority field")
    ap.add_argument("--rate", type=int, default=60, help="Plane API calls per minute (default 60)")
    ap.add_argument("--dry-run", action="store_true", help="read the target, print the plan, write nothing")
    ap.add_argument("--report", help="write a JSON summary to this file")
    args = ap.parse_args(argv)

    labels = load_labels(args.labels)
    plan = Plan(args.dry_run)
    if args.dry_run:
        print("DRY RUN: nothing will be written")
    try:
        info = (populate_gitlab if args.target == "gitlab" else populate_plane)(args, labels, plan)
    except ApiError as e:
        sys.exit(f"API error: {e}")
    summary = {**info, "dry_run": args.dry_run, "created": plan.created, "reused": plan.reused, "notes": plan.notes}
    print(f"Summary: {len(plan.created)} {'to create' if args.dry_run else 'created'}, {len(plan.reused)} reused")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
