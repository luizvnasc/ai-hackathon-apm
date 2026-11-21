"""An in-memory Plane API used in place of Plane._request (no network).

It answers the paths migrate.py calls with the shapes of Plane v1.4.3 /api/v1:
paginated lists ({"results", "next_page_results", "next_cursor"}), the project
estimate as a single object (404 when absent), estimate points created from a
list, and a 409 with the existing id when a work item's external_id is known.
"""
import copy
import json
import pathlib
import re

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
DEFAULT_STATES = [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"),
                  ("Done", "completed"), ("Cancelled", "cancelled")]


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page(rows):
    rows = list(rows)
    return {"results": rows, "next_page_results": False, "next_cursor": None, "total_results": len(rows)}


class FakePlane:
    def __init__(self, mig, members=None):
        self.mig = mig
        self.members = fixture("plane_members.json") if members is None else members
        self.projects, self.states, self.labels, self.estimates, self.points = [], {}, {}, {}, {}
        self.project_members, self.cycles, self.items, self.links, self.comments = {}, {}, {}, {}, {}
        self.cycle_issues, self.calls, self.failures, self.seq = {}, [], [], 0

    # ---------------------------------------------------------------- helpers for tests
    def new_id(self, kind):
        self.seq += 1
        return f"{kind}-{self.seq}"

    def add_project(self, name="Inventory Manager", identifier="INVM", **extra):
        pid = self.new_id("proj")
        proj = {"id": pid, "name": name, "identifier": identifier, "cycle_view": False, "estimate": None, **extra}
        self.projects.append(proj)
        self.states[pid] = [{"id": self.new_id("state"), "name": n, "group": g} for n, g in DEFAULT_STATES]
        for key in (self.labels, self.cycles, self.items):
            key[pid] = []
        self.project_members[pid] = set()
        return proj

    def fail(self, method, pattern, status, payload=None):
        """Make the next call matching method + path regex fail once with `status`."""
        self.failures.append((method, re.compile(pattern), status, payload or {"error": "fake failure"}))

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]

    def counts(self):
        return {"projects": len(self.projects), "states": sum(map(len, self.states.values())),
                "labels": sum(map(len, self.labels.values())), "estimates": len(self.estimates),
                "points": sum(map(len, self.points.values())), "cycles": sum(map(len, self.cycles.values())),
                "items": sum(map(len, self.items.values())), "links": sum(map(len, self.links.values())),
                "comments": sum(map(len, self.comments.values())),
                "project_members": sum(map(len, self.project_members.values()))}

    def error(self, status, payload, method, path):
        err = self.mig.PlaneError(status, json.dumps(payload), method, path)
        err.payload = payload
        return err

    # ---------------------------------------------------------------- the router
    def __call__(self, method, path, body=None, params=None):
        self.calls.append((method, path, copy.deepcopy(body)))  # the caller may mutate body afterwards
        for f in list(self.failures):
            if f[0] == method and f[1].search(path):
                self.failures.remove(f)
                raise self.error(f[2], f[3], method, path)
        for pattern, handler in self.ROUTES:
            m = re.fullmatch(pattern, path)
            if m:
                result = getattr(self, handler)(method, body, *m.groups())
                return 200 if method == "GET" else 201, result
        raise self.error(404, {"error": "no route"}, method, path)

    ROUTES = [
        (r"/projects/", "r_projects"),
        (r"/projects/([^/]+)/", "r_project"),
        (r"/projects/([^/]+)/states/", "r_states"),
        (r"/projects/([^/]+)/labels/", "r_labels"),
        (r"/projects/([^/]+)/estimates/", "r_estimates"),
        (r"/projects/([^/]+)/estimates/([^/]+)/estimate-points/", "r_points"),
        (r"/members/", "r_members"),
        (r"/projects/([^/]+)/project-members/", "r_project_members"),
        (r"/projects/([^/]+)/cycles/", "r_cycles"),
        (r"/projects/([^/]+)/cycles/([^/]+)/cycle-issues/", "r_cycle_issues"),
        (r"/projects/([^/]+)/work-items/", "r_items"),
        (r"/projects/([^/]+)/work-items/([^/]+)/", "r_item"),
        (r"/projects/([^/]+)/work-items/([^/]+)/links/", "r_links"),
        (r"/projects/([^/]+)/work-items/([^/]+)/comments/", "r_comments"),
    ]

    def r_projects(self, method, body):
        if method == "GET":
            return page(dict(p) for p in self.projects)
        if any(p["identifier"] == body["identifier"] for p in self.projects):
            raise self.error(409, {"error": "identifier taken"}, method, "/projects/")
        proj = self.add_project(**body)
        return dict(proj)

    def r_project(self, method, body, pid):
        proj = next(p for p in self.projects if p["id"] == pid)
        proj.update(body or {})
        return dict(proj)

    def r_states(self, method, body, pid):
        if method == "GET":
            return page(self.states[pid])
        state = {"id": self.new_id("state"), **body}
        self.states[pid].append(state)
        return state

    def r_labels(self, method, body, pid):
        if method == "GET":
            return page(self.labels[pid])
        if any(l["name"].lower() == body["name"].lower() for l in self.labels[pid]):
            raise self.error(409, {"error": "label exists"}, method, "labels")
        label = {"id": self.new_id("label"), **body}
        self.labels[pid].append(label)
        return label

    def r_estimates(self, method, body, pid):
        if method == "GET":
            if pid not in self.estimates:
                raise self.error(404, {"error": "Estimate not found"}, method, "estimates")
            return dict(self.estimates[pid])
        est = {"id": self.new_id("est"), **body}
        self.estimates[pid] = est
        self.points[est["id"]] = []
        return dict(est)

    def r_points(self, method, body, pid, eid):
        if method == "GET":
            return list(self.points[eid])  # Plane returns a plain list here
        created = [{"id": self.new_id("point"), **b} for b in body]
        self.points[eid] += created
        return created

    def r_members(self, method, body):
        return list(self.members)

    def r_project_members(self, method, body, pid):
        if method == "GET":
            return [m for m in self.members if m["id"] in self.project_members[pid]]
        self.project_members[pid].add(body["member"])
        return {"id": self.new_id("pm"), **body}

    def r_cycles(self, method, body, pid):
        if method == "GET":
            return page(self.cycles[pid])
        cycle = {"id": self.new_id("cycle"), **body}
        self.cycles[pid].append(cycle)
        return cycle

    def r_cycle_issues(self, method, body, pid, cid):
        self.cycle_issues.setdefault(cid, set()).update(body["issues"])
        return {"message": "ok"}

    def r_items(self, method, body, pid):
        if method == "GET":
            return page(self.items[pid])
        old = next((i for i in self.items[pid] if i.get("external_id") == body.get("external_id")
                    and i.get("external_source") == body.get("external_source")), None)
        if old:
            raise self.error(409, {"error": "Issue with the same external id and external source already exists",
                                   "id": old["id"]}, method, "work-items")
        item = {"id": self.new_id("item"), "sequence_id": len(self.items[pid]) + 1, **body}
        self.items[pid].append(item)
        return dict(item)

    def r_item(self, method, body, pid, wid):
        item = next(i for i in self.items[pid] if i["id"] == wid)
        item.update(body or {})
        return dict(item)

    def r_links(self, method, body, pid, wid):
        rows = self.links.setdefault(wid, [])
        if method == "GET":
            return page(rows)
        link = {"id": self.new_id("link"), **body}
        rows.append(link)
        return link

    def r_comments(self, method, body, pid, wid):
        rows = self.comments.setdefault(wid, [])
        if method == "GET":
            return page(rows)
        comment = {"id": self.new_id("comment"), **body}
        rows.append(comment)
        return comment
