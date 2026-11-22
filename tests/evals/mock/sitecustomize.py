"""Offline GitLab/Plane for the skill evals (stdlib only).

Python imports ``sitecustomize`` at startup when this directory is on PYTHONPATH.
When ``EVAL_FIXTURES`` points to a fixtures directory, ``urllib.request.urlopen``
is replaced by a function that answers from the JSON files in that directory, so
the skills' scripts run unchanged but never touch a network. Without
``EVAL_FIXTURES`` this module does nothing.

Environment:
  EVAL_FIXTURES      directory with a ``routes.json`` manifest and the JSON bodies it names
  EVAL_DATASET_DATE  optional YYYY-MM-DD: the "today" the fixtures were written for. Every
                     date in a fixture body is shifted by (today - EVAL_DATASET_DATE) days, so
                     the dataset always ends "now": the script's today matches the real date the
                     agent sees, and idle days, current periods etc. never drift.
  EVAL_TODAY         optional YYYY-MM-DD: ``datetime.date.today()`` returns it (unit tests
                     use it with EVAL_DATASET_DATE set to the same date, so nothing shifts)

Weekly buckets (Monday to Sunday) depend on the weekday of "today", so a shift that is not
a multiple of 7 days can move work between weeks; assert exact weekly numbers only in unit
tests, where EVAL_TODAY freezes the date.

``routes.json`` is a list, first match wins. Hosts are ignored; only the path and
query matter:

  {"method": "GET",                      # default GET
   "path": "/api/v1/workspaces/eval/projects/",   # exact; or "path_regex": "^/api/v4/projects/.*"
   "query": {"per_page": "100"},         # optional: these pairs must be present (subset match)
   "graphql": {"contains": ["milestones"],        # optional, for POST /api/graphql: every keyword
               "variables": {"after": null}},     # must be in the query; variables is a subset match
   "file": "plane_projects.json",        # body, relative to the fixtures dir
   "expand": {"estimate_point": "points.json"},   # optional: when ?expand= names the field, its id is
                                         # replaced by the object with that id in this file, as Plane does
   "status": 200,                        # default 200
   "headers": {"X-Next-Page": ""}}       # optional response headers

Rules:
  * Unmatched requests get HTTP 404 with a JSON body, and the miss is logged to
    stderr as ``[eval-mock] MISS ...`` so fixtures are easy to complete.
  * Writes (any method other than GET, except a GraphQL POST whose query is not a
    mutation) always get HTTP 403 and are logged as ``[eval-mock] WRITE REFUSED``.
    A write can never succeed here, whatever the manifest says.
"""
import datetime
import email.message
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

LOG_PREFIX = "[eval-mock]"


def _log(message):
    print(f"{LOG_PREFIX} {message}", file=sys.stderr)


class FakeResponse:
    """The slice of ``http.client.HTTPResponse`` the scripts use."""

    def __init__(self, body, status=200, headers=None, url=""):
        self._fp = io.BytesIO(body)
        self.status = self.code = status
        self.url = url
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)

    def read(self, *args):
        return self._fp.read(*args)

    def getcode(self):
        return self.status

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def close(self):
        self._fp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _error(url, status, payload):
    body = json.dumps(payload).encode()
    return urllib.error.HTTPError(url, status, str(payload.get("error", status)), email.message.Message(),
                                  io.BytesIO(body))


def _body_json(data):
    try:
        return json.loads(data.decode()) if data else None
    except (ValueError, AttributeError):
        return None


def _is_write(method, path, body):
    if method == "GET":
        return False
    if method == "POST" and path.rstrip("/") == "/api/graphql":
        query = (body or {}).get("query") if isinstance(body, dict) else None
        return not isinstance(query, str) or query.lstrip().startswith("mutation")
    return True


def _match(route, method, path, query, body):
    if route.get("method", "GET").upper() != method:
        return False
    if "path" in route and route["path"] != path:
        return False
    if "path_regex" in route and not re.search(route["path_regex"], path):
        return False
    for key, value in (route.get("query") or {}).items():
        if query.get(key) != [str(value)]:
            return False
    gql = route.get("graphql")
    if gql:
        text = (body or {}).get("query", "") if isinstance(body, dict) else ""
        if not all(word in text for word in gql.get("contains", [])):
            return False
        variables = (body or {}).get("variables") or {}
        if any(variables.get(k) != v for k, v in (gql.get("variables") or {}).items()):
            return False
    return True


_DATE = re.compile(rb"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])")


def shift_dates(payload, days):
    """Move every YYYY-MM-DD in payload (bytes) by ``days``; times and zones are kept."""
    if not days:
        return payload

    def move(m):
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return m.group(0)
        return (d + datetime.timedelta(days=days)).isoformat().encode()

    return _DATE.sub(move, payload)


def _expand(payload, route, query, fixtures_dir):
    """Plane's ?expand=: swap the ids of the requested fields for their objects."""
    asked = ",".join(query.get("expand", [])).split(",")
    fields = {k: v for k, v in (route.get("expand") or {}).items() if k in asked}
    if not fields:
        return payload
    body = json.loads(payload)
    for field, name in fields.items():
        with open(os.path.join(fixtures_dir, name), encoding="utf-8") as fh:
            objects = {o["id"]: o for o in json.load(fh)["results"]}
        for row in body["results"]:
            if row.get(field):
                row[field] = objects[row[field]]
    return json.dumps(body).encode()


def make_urlopen(fixtures_dir, shift_days=0):
    """Build the replacement for ``urllib.request.urlopen`` that answers from fixtures_dir."""
    with open(os.path.join(fixtures_dir, "routes.json"), encoding="utf-8") as fh:
        routes = json.load(fh)

    def urlopen(request, data=None, timeout=None, **_kwargs):
        if isinstance(request, str):
            request = urllib.request.Request(request, data=data)
        method = request.get_method().upper()
        parts = urllib.parse.urlsplit(request.full_url)
        path, query = parts.path, urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        pairs = sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        shown = path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")
        body = _body_json(request.data)
        if _is_write(method, path, body):
            _log(f"WRITE REFUSED {method} {shown}")
            raise _error(request.full_url, 403, {"error": "eval mock: writes are never allowed"})
        for route in routes:
            if _match(route, method, path, query, body):
                with open(os.path.join(fixtures_dir, route["file"]), "rb") as fh:
                    payload = shift_dates(_expand(fh.read(), route, query, fixtures_dir), shift_days)
                status = route.get("status", 200)
                if status >= 400:
                    raise urllib.error.HTTPError(request.full_url, status, "mock", email.message.Message(),
                                                 io.BytesIO(payload))
                return FakeResponse(payload, status, route.get("headers"), request.full_url)
        if isinstance(body, dict) and "query" in body:
            shown += " graphql " + " ".join(str(body["query"]).split())[:80]
        _log(f"MISS {method} {shown}")
        raise _error(request.full_url, 404, {"error": f"eval mock: no fixture for {method} {shown}"})

    return urlopen


def _freeze_today(iso):
    real = datetime.date

    class FrozenDate(real):
        @classmethod
        def today(cls):
            return cls.fromisoformat(iso)

    datetime.date = FrozenDate


def install():
    fixtures = os.environ.get("EVAL_FIXTURES")
    if not fixtures:
        return
    if os.environ.get("EVAL_TODAY"):
        _freeze_today(os.environ["EVAL_TODAY"])
    shift = 0
    if os.environ.get("EVAL_DATASET_DATE"):
        anchor = datetime.date.fromisoformat(os.environ["EVAL_DATASET_DATE"])
        shift = (datetime.date.today() - anchor).days
    urllib.request.urlopen = make_urlopen(fixtures, shift)


install()
