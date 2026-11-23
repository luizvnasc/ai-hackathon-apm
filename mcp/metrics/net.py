"""API errors, and the guards that keep a token on the configured server. Importing it installs the redirect guard."""
import io
import sys
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, status, body, method, url, shown=400):
        """`shown` is how much of the answer goes into the message."""
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:shown]}")
        self.status, self.body = status, body


def workspace_segment(slug):
    """The workspace slug as one path segment, so it cannot change the API route."""
    if slug in (".", ".."):
        sys.exit(f"invalid Plane workspace {slug!r}")
    return urllib.parse.quote(slug, safe="")


def safe_redirect(source, target):
    """Whether a redirect may carry the token: same origin, or http -> https on the same host (default ports)."""
    def origin(url):
        u = urllib.parse.urlsplit(url)
        return u.scheme, u.hostname, u.port or {"http": 80, "https": 443}.get(u.scheme)
    try:
        old, new = origin(source), origin(target)
    except ValueError:
        return False
    return old == new or (old[1] == new[1] and old[0] == "http" and old[2] == 80 and new[0] == "https" and new[2] == 443)


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """urllib resends every header, tokens included, to wherever a redirect points: refuse other origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_redirect(req.full_url, newurl):
            why = b"redirect to another origin refused, so the token is not sent there: check the configured URL"
            raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, io.BytesIO(why))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urllib.request.install_opener(urllib.request.build_opener(SameOriginRedirects))
