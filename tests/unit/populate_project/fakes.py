"""In-memory GitLab and Plane servers that stand in for populate.py's `http` (no network).

Each fake keeps state, so a second run sees what the first one created, and records every
request in `calls` so tests can check that a dry run never writes.
"""
import argparse
import copy
import json
import pathlib
import urllib.parse

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
GITLAB_URL = "https://gitlab.example.com"
PLANE_URL = "https://plane.example.com"
GITLAB_TOKEN = "glpat-secret-123"
PLANE_KEY = "plane-secret-456"


def fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


def args(**kw):
    base = dict(target="gitlab", project="acme/shop", labels="labels.json", gitlab_url=GITLAB_URL,
                board_name="Kanban", plane_url=PLANE_URL, workspace="demo-ws", create_project=False,
                identifier=None, priority_labels=False, rate=60, dry_run=False, report=None)
    base.update(kw)
    return argparse.Namespace(**base)


class FakeServer:
    def __init__(self, module):
        self.module, self.calls = module, []

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def error(self, status, method, url, body='{"message": "error"}'):
        raise self.module.ApiError(status, body, method, url)

    def __call__(self, method, url, headers, body=None):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((method, parts.path, query, copy.deepcopy(body), dict(headers)))
        return self.handle(method, url, parts.path, query, body)


class FakeGitLab(FakeServer):
    """GitLab REST v4: one project, its (project + group) labels, boards with lists."""

    def __init__(self, module, labels=None, boards=None, page_size=100, refuse_board=False, project=None):
        super().__init__(module)
        self.project = project if project is not None else fixture("gitlab_project.json")
        self.labels = copy.deepcopy(labels if labels is not None else [])
        self.boards = copy.deepcopy(boards if boards is not None else [])
        self.page_size, self.refuse_board, self.next_id = page_size, refuse_board, 1000

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def page(self, items, query):
        size = min(int(query.get("per_page", 20)), self.page_size)
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * size:page * size]
        headers = {"X-Next-Page": str(page + 1) if page * size < len(items) else ""}
        return 200, copy.deepcopy(chunk), headers

    def handle(self, method, url, path, query, body):
        prefix = "/api/v4/projects/"
        assert path.startswith(prefix), path
        rest = path[len(prefix):].split("/")
        ref = urllib.parse.unquote(rest[0])
        if ref not in (str(self.project["id"]), self.project["path_with_namespace"]):
            self.error(404, method, url, '{"message": "404 Project Not Found"}')
        tail = rest[1:]
        if not tail:
            return 200, copy.deepcopy(self.project), {}
        if tail == ["labels"] and method == "GET":
            return self.page(self.labels, query)
        if tail == ["labels"] and method == "POST":
            if any(l["name"] == body["name"] for l in self.labels):
                self.error(409, method, url, '{"message": "Label already exists"}')
            label = dict(body, id=self.new_id(), is_project_label=True)
            self.labels.append(label)
            return 201, copy.deepcopy(label), {}
        if tail == ["boards"] and method == "GET":
            return self.page(self.boards, query)
        if tail == ["boards"] and method == "POST":
            if self.refuse_board:
                self.error(403, method, url, '{"message": "403 Forbidden - multiple boards need a paid tier"}')
            board = {"id": self.new_id(), "name": body["name"], "lists": []}
            self.boards.append(board)
            return 201, copy.deepcopy(board), {}
        if len(tail) == 3 and tail[0] == "boards" and tail[2] == "lists" and method == "POST":
            board = next(b for b in self.boards if str(b["id"]) == tail[1])
            label = next(l for l in self.labels if l["id"] == body["label_id"])
            item = {"id": self.new_id(), "position": len(board["lists"]),
                    "label": {"id": label["id"], "name": label["name"]}}
            board["lists"].append(item)
            return 201, copy.deepcopy(item), {}
        raise AssertionError(f"unexpected GitLab call {method} {path}")


class FakePlane(FakeServer):
    """Plane REST /api/v1: projects, states and labels of one workspace, cursor pagination."""

    def __init__(self, module, projects=None, states=None, labels=None, workspace="demo-ws", page_size=100):
        super().__init__(module)
        self.workspace, self.page_size, self.next_id = workspace, page_size, 0
        self.projects = copy.deepcopy(projects if projects is not None else fixture("plane_projects.json")["results"])
        self.states = {p["id"]: [] for p in self.projects}
        self.labels = {p["id"]: [] for p in self.projects}
        self.states["p-1"] = copy.deepcopy(states if states is not None else fixture("plane_states.json")["results"])
        self.labels["p-1"] = copy.deepcopy(labels if labels is not None else [])

    def new_id(self, kind):
        self.next_id += 1
        return f"{kind}-new-{self.next_id}"

    def page(self, items, query):
        size = min(int(query.get("per_page", 100)), self.page_size)
        start = int(query.get("cursor", "0"))
        chunk = items[start:start + size]
        more = start + size < len(items)
        return 200, {"results": copy.deepcopy(chunk), "next_page_results": more,
                     "next_cursor": str(start + size)}, {}

    def handle(self, method, url, path, query, body):
        prefix = f"/api/v1/workspaces/{self.workspace}/projects/"
        if not path.startswith(prefix):
            self.error(404, method, url, '{"error": "Workspace not found"}')
        rest = [x for x in path[len(prefix):].split("/") if x]
        if not rest and method == "GET":
            return self.page(self.projects, query)
        if not rest and method == "POST":
            proj = dict(body, id=self.new_id("p"))
            self.projects.append(proj)
            # like Plane, a new project comes with the five default states
            defaults = fixture("plane_states.json")["results"]
            self.states[proj["id"]] = [dict(s, id=self.new_id("s")) for s in defaults]
            self.labels[proj["id"]] = []
            return 201, copy.deepcopy(proj), {}
        pid, kind = rest[0], rest[1]
        store = {"states": self.states, "labels": self.labels}[kind][pid]
        if method == "GET":
            return self.page(store, query)
        if method == "POST":
            if any(x["name"].lower() == body["name"].lower() for x in store):
                self.error(400, method, url, '{"error": "name already taken"}')
            item = dict(body, id=self.new_id(kind[0]))
            item.setdefault("parent", None)
            store.append(item)
            return 201, copy.deepcopy(item), {}
        raise AssertionError(f"unexpected Plane call {method} {path}")
