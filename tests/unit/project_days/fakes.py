"""An offline GitLab and Plane with one sprint, whose times are written in a chosen timezone."""
import datetime as dt

START, END = dt.date(2026, 3, 2), dt.date(2026, 3, 15)
UTC = dt.timezone.utc


def offset(text):
    """'-03:00' as a timezone."""
    hours, minutes = int(text[1:3]), int(text[4:6])
    delta = dt.timedelta(hours=hours, minutes=minutes)
    return dt.timezone(-delta if text[0] == "-" else delta)


def moment(local_time, zone):
    """'03-15T22:00' of 2026, on the clock of `zone`."""
    return dt.datetime.fromisoformat(f"2026-{local_time}:00").replace(tzinfo=zone)


class World:
    """`project` is the timezone of the Plane project or of the GitLab instance, `key_owner` the one of the
    API key's owner: Plane writes every time of its answers in it."""

    def __init__(self, project="-03:00", key_owner="+00:00", zone_name=None):
        self.project, self.key_owner = offset(project), offset(key_owner)
        self.zone_name = zone_name or project
        self.closed = self.joined = None
        self.updated = moment("03-10T12:00", self.project)
        self.comment = None
        self.blocked = self.members = False
        self.instance_time = moment("03-10T12:00", self.project)

    def plane_time(self, when):
        return when.astimezone(self.key_owner).isoformat().replace("+00:00", "Z") if when else None

    def gitlab_time(self, when):
        return when.astimezone(self.project).isoformat() if when else None

    def http(self, method, url, headers, body=None):
        if url.endswith("/api/graphql"):
            return self.graphql(body["query"]), {}
        if "/api/v4/" in url:
            return self.gitlab_rest(url.split("?")[0]), {}
        return self.plane(url.split("?")[0]), {}

    def graphql(self, query):
        project = {"name": "Demo", "webUrl": "https://gl/demo", "lastActivityAt": self.gitlab_time(self.instance_time)}
        if "milestones(" in query:
            state = "active"
            project["milestones"] = {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"id": "gid://gitlab/Milestone/1", "title": "Sprint", "state": state,
                 "startDate": START.isoformat(), "dueDate": END.isoformat()}]}
        else:
            project["workItems"] = {"pageInfo": {"hasNextPage": False}, "nodes": [self.work_item()]}
        return {"data": {"project": project}}

    def work_item(self):
        labels = [{"title": "points::3"}] + ([{"title": "workflow::blocked"}] if self.blocked else [])
        return {"id": "gid://gitlab/WorkItem/1", "iid": "1", "reference": "demo/app#1", "title": "Task",
                "webUrl": "https://gl/demo/1", "state": "CLOSED" if self.closed else "OPEN",
                "createdAt": self.gitlab_time(moment("02-20T12:00", self.project)),
                "updatedAt": self.gitlab_time(self.updated), "closedAt": self.gitlab_time(self.closed),
                "widgets": [{"type": "LABELS", "labels": {"nodes": labels}},
                            {"type": "MILESTONE", "milestone": {"id": "gid://gitlab/Milestone/1", "title": "Sprint"}}]}

    def gitlab_rest(self, path):
        if path.endswith("/resource_milestone_events") and self.joined:
            return [{"action": "add", "created_at": self.gitlab_time(self.joined),
                     "milestone": {"id": 1, "title": "Sprint"}}]
        if path.endswith("/notes") and self.comment:
            return [{"system": False, "author": {"username": "ana"}, "body": "blocked by the vendor",
                     "created_at": self.gitlab_time(self.comment)}]
        if path.endswith("/projects/demo%2Fapp"):
            return {"id": 100}
        return []

    def plane(self, path):
        parts = path.rstrip("/").split("/")
        if parts[-1] == "projects":
            return [{"id": "p", "identifier": "DEMO", "name": "Demo", "timezone": self.zone_name}]
        if parts[-1] == "members" and self.members:
            return [{"id": "u", "display_name": "ana", "role": 15}]
        if parts[-1] == "cycles":
            return [self.cycle()]
        if parts[-1] in ("cycle-issues", "issues"):
            return [self.plane_issue()]
        if parts[-2] == "issues":
            return self.plane_issue()
        if parts[-1] == "activities" and self.joined:
            return [{"field": "cycles", "verb": "created", "created_at": self.plane_time(self.joined),
                     "old_identifier": None, "old_value": "", "new_identifier": "c", "new_value": "Sprint"}]
        if parts[-1] == "comments" and self.comment:
            return [{"id": "k", "actor": "u", "comment_html": "<p>blocked by the vendor</p>",
                     "created_at": self.plane_time(self.comment)}]
        return []

    def cycle(self):
        # As Plane's convert_to_utc stores them: 00:00:01 and 23:59:00 in the project's timezone
        start = dt.datetime.combine(START, dt.time(0, 0, 1), tzinfo=self.project)
        end = dt.datetime.combine(END, dt.time(23, 59), tzinfo=self.project)
        return {"id": "c", "name": "Sprint", "start_date": self.plane_time(start), "end_date": self.plane_time(end)}

    def plane_issue(self):
        state = {"group": "started", "name": "Blocked" if self.blocked else "Doing"}
        if self.closed:
            state = {"group": "completed", "name": "Done"}
        return {"id": "i", "sequence_id": 1, "name": "Task", "project": "p", "state": state, "labels": [],
                "assignees": [], "estimate_point": {"value": "3"},
                "created_at": self.plane_time(moment("02-20T12:00", self.project)),
                "updated_at": self.plane_time(self.updated), "completed_at": self.plane_time(self.closed)}
