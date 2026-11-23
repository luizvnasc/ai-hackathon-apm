"""GitLab client: GraphQL, REST with pages, and the user's projects."""
import json
import sys
import urllib.parse

from .days import InstanceZone, ProjectDays
from .net import ApiError

PAGE_SIZE = 100
MEMBER_PROJECTS = {"membership": "true", "archived": "false", "order_by": "last_activity_at", "sort": "desc"}


def with_full_path(source):
    """The GitLab source, with a project given without its group turned into group/project."""
    source.use_full_path()
    return source


class GitLabClient(ProjectDays):
    """Base of each skill's GitLab class. The skill provides send(method, url, body), which makes one request."""

    kind = "milestone"
    empty_page_is_ok = False  # True: an empty answer to a list is an empty page, not an error

    def __init__(self, url, token, project=None):
        self.url, self.project = url.rstrip("/"), project
        self.headers = {"PRIVATE-TOKEN": token}
        self.tz = InstanceZone()

    def send(self, method, url, body=None):
        """Returns (data, response headers)."""
        raise NotImplementedError

    def graphql(self, query, variables=None):
        payload = {"query": query, "variables": variables or {}}
        data, _ = self.send("POST", self.url + "/api/graphql", payload)
        if data.get("errors"):  # GraphQL reports errors with HTTP 200
            raise ApiError(200, json.dumps(data["errors"]), "POST", "/api/graphql")
        return data["data"]

    def rest_url(self, path, params=None):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return f"{self.url}/api/v4{path}{query}"

    def rest(self, path, params=None, *, method="GET", body=None):
        data, _ = self.send(method, self.rest_url(path, params), body)
        return data

    def rest_pages(self, path, params=None):
        """The rows of every page."""
        query, rows, page = dict(params or {}), [], 1
        while True:
            query.update(per_page=PAGE_SIZE, page=page)
            data, headers = self.send("GET", self.rest_url(path, query))
            if not data and self.empty_page_is_ok:
                data = []
            rows += data
            if not headers.get("X-Next-Page"):
                return rows
            page = int(headers["X-Next-Page"])

    def member_projects(self):
        """The projects the token's user is a member of, directly or through a group."""
        return self.rest_pages("/projects", MEMBER_PROJECTS)

    def use_full_path(self):
        """A project given without its group becomes group/project, found among the user's projects."""
        if not self.project:
            return
        given_in_full = "/" in self.project or self.project.isdigit()
        if given_in_full:
            return
        self.project = self.full_path(self.project)

    def full_path(self, name):
        wanted = name.strip().lower()
        paths = []
        for row in self.member_projects():
            full_path = row["path_with_namespace"]
            project_path = full_path.rsplit("/", 1)[-1]
            if wanted in (project_path.lower(), row["name"].lower()):
                paths.append(full_path)
        if len(paths) == 1:
            return paths[0]
        if not paths:
            sys.exit(f"GitLab project {name!r} is not among your projects. ASK THE USER: which group holds it? "
                     "Then call again with project set to group/project.")
        choices = "\n".join(f"- {path}" for path in paths)
        sys.exit(f"GitLab project {name!r} is in more than one group:\n{choices}\n"
                 "ASK THE USER: which one? Show this list, then call again with project set to that group/project.")

    def my_projects(self):
        projects = []
        for row in self.member_projects():
            if row.get("marked_for_deletion_at") or row.get("marked_for_deletion_on"):
                continue
            projects.append({"path": row["path_with_namespace"], "name": row["name"], "url": row["web_url"]})
        return projects
