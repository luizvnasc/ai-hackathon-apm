"""Plane client: rate limit, retries, pages and projects."""
import sys
import time
import urllib.parse

from .days import ProjectDays
from .labels import number
from .net import ApiError, workspace_segment

PAGE_SIZE = 100
RATE_MARGIN = 2  # requests per minute left free, so the limit itself is never reached
MAX_ATTEMPTS = 8
SERVER_ERROR_RETRIES = 3
RATE_LIMIT_WAIT = 10  # seconds


def retry_delay(status, attempt):
    """Seconds to wait before trying again, or None when the error is final."""
    if status == 429:
        return RATE_LIMIT_WAIT
    if status >= 500 and attempt < SERVER_ERROR_RETRIES:
        return 2 ** attempt
    return None


def estimate(issue):
    """The points of an issue read with expand=estimate_point. Never from labels, and never from /estimates/,
    which Plane CE does not route (makeplane/plane#9933)."""
    point = issue.get("estimate_point")
    return number(point.get("value")) if isinstance(point, dict) else None


def full_label_names(labels, names_by_id):
    """A label inside a group becomes `group::label`, as it would be in GitLab."""
    names = []
    for label in labels or []:
        if not isinstance(label, dict):
            continue  # only an id: the request did not expand labels
        group = names_by_id.get(label.get("parent"))
        names.append(f"{group}::{label['name']}" if group else label["name"])
    return names


class PlaneClient(ProjectDays):
    """Base of each skill's Plane class. The skill provides send(method, url, body), which makes one request."""

    kind = "cycle"

    def __init__(self, url, workspace, api_key, rate_per_min=60):
        self.url, self.ws = url.rstrip("/"), workspace
        self.base = f"{self.url}/api/v1/workspaces/{workspace_segment(workspace)}"
        self.headers = {"X-API-Key": api_key}
        self.min_interval = 60.0 / max(1, rate_per_min - RATE_MARGIN)
        self.last_request = 0.0

    def send(self, method, url, body=None):
        """Returns (data, response headers)."""
        raise NotImplementedError

    def api_url(self, path, params=None):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return f"{self.base}{path}{query}"

    def pace(self):
        wait = self.min_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()

    def request(self, path, params=None, *, method="GET", body=None):
        url = self.api_url(path, params)
        for attempt in range(MAX_ATTEMPTS):
            self.pace()
            try:
                data, _ = self.send(method, url, body)
                return data
            except ApiError as error:
                delay = retry_delay(error.status, attempt)
                if delay is None:
                    raise
                time.sleep(delay)
        raise ApiError(429, "rate limited too many times", method, url)

    def list(self, path, params=None):
        """The rows of every page."""
        query, rows = {"per_page": PAGE_SIZE, **(params or {})}, []
        while True:
            data = self.request(path, query)
            if isinstance(data, list):  # some endpoints are not paginated
                return rows + data
            rows += data.get("results") or []
            has_next_page = data.get("next_page_results") and data.get("next_cursor")
            if not has_next_page:
                return rows
            query["cursor"] = data["next_cursor"]

    def select_project(self, wanted):
        """Read the workspace's projects and select `wanted` by identifier, name or id. Returns its row."""
        self.all_projects = self.list("/projects/")
        if wanted is None:
            return None
        row = self.find_project(wanted)
        self.pid, self.ident = row["id"], row["identifier"]
        self.name = f"{row['name']} ({row['identifier']})"
        if row.get("timezone"):
            self.use_timezone(row["timezone"])  # Plane stores a cycle's days as times in this timezone
        return row

    def find_project(self, wanted):
        text = wanted.strip().lower()
        for row in self.all_projects:
            same_name = text in (row["identifier"].lower(), row["name"].lower())
            if same_name or row["id"] == wanted:
                return row
        known = ", ".join(f"{row['name']} ({row['identifier']})" for row in self.all_projects)
        sys.exit(f"Plane project {wanted!r} not found in workspace {self.ws!r}. Projects: {known}")

    def joined_projects(self):
        """The active projects the key's user is a member of."""
        return [row for row in self.all_projects if row.get("is_member", True) and not row.get("archived_at")]

    def project_url(self, project_id):
        return f"{self.url}/{self.ws}/projects/{project_id}/issues"

    def my_projects(self):
        projects = []
        for row in self.joined_projects():
            url = self.project_url(row["id"])
            projects.append({"path": row["identifier"], "name": row["name"], "url": url})
        return projects
