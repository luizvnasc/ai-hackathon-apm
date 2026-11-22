"""The agent container's only way out: a filter in front of the Anthropic Messages API (stdlib only).

Runs in its own container and listens on a Unix socket. The credential arrives on stdin and is never
given to the agent's container. Only Haiku requests to the Messages API are forwarded, and none of the
features that make Anthropic's servers fetch or run things (server tools, MCP, URL or file sources).
The process ends when stdin closes, so it cannot outlive run.py.
"""
import http.client
import http.server
import json
import os
import socketserver
import ssl
import sys
import threading

HOST = "api.anthropic.com"
SOCKET = "/run/proxy/api.sock"
PATHS = {"/v1/messages", "/v1/messages?beta=true", "/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true"}
FIELDS = {"model", "messages", "system", "tools", "tool_choice", "max_tokens", "metadata", "stream", "temperature",
          "top_p", "top_k", "stop_sequences", "thinking", "output_config", "output_format", "context_management",
          "diagnostics"}
FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta", "user-agent", "x-app")
MAX_BODY = 8 * 1024 * 1024
MAX_REQUESTS = 1500  # per run: a runaway agent cannot drain the quota


def remote_source(value):
    """True when a content block asks the API to fetch a URL or to read an uploaded file."""
    if isinstance(value, list):
        return any(remote_source(v) for v in value)
    if not isinstance(value, dict):
        return False
    source = value.get("source")
    if isinstance(source, dict) and source.get("type") in ("url", "file"):
        return True
    return any(remote_source(v) for v in value.values())


def refusal(method, path, payload):
    """Why a request is not forwarded, or None when it is allowed."""
    if method != "POST" or path not in PATHS:
        return "route not allowed"
    if not isinstance(payload, dict):
        return "body is not a JSON object"
    extra = sorted(set(payload) - FIELDS)
    if extra:
        return "fields not allowed: " + ", ".join(extra)[:200]
    if not str(payload.get("model", "")).startswith("claude-haiku-"):
        return "model not allowed"
    tools = payload.get("tools") or []
    if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get("type") not in (None, "custom") for t in tools):
        return "server tools not allowed"
    if remote_source(payload.get("messages")) or remote_source(payload.get("system")):
        return "remote sources not allowed"
    return None


def upstream_headers(incoming, kind, credential):
    """Headers for the API: an allowlist, plus the real credential."""
    out = {"content-type": "application/json"}
    for name in FORWARDED_HEADERS:
        if incoming.get(name):
            out[name] = incoming.get(name)
    out.setdefault("anthropic-version", "2023-06-01")
    if kind == "oauth":
        out["authorization"] = "Bearer " + credential
        betas = [b for b in out.get("anthropic-beta", "").split(",") if b]
        if "oauth-2025-04-20" not in betas:
            betas.append("oauth-2025-04-20")
        out["anthropic-beta"] = ",".join(betas)
    else:
        out["x-api-key"] = credential
    return out


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, kind, credential, fake=None):
        self.kind, self.credential, self.fake = kind, credential, fake
        self.count, self.lock = 0, threading.Lock()
        super().__init__(path, Handler)

    def admit(self):
        with self.lock:
            self.count += 1
            return self.count <= MAX_REQUESTS


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 300

    def address_string(self):
        return "agent"

    def log_message(self, *args):
        pass  # never log what the agent sends

    def reply(self, status, data, content_type="application/json"):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def refuse(self, status, why):
        print(f"[proxy] refused: {why}", file=sys.stderr, flush=True)
        error = {"type": "error", "error": {"type": "invalid_request_error", "message": "eval sandbox: " + why}}
        self.reply(status, json.dumps(error).encode())

    def do_GET(self):
        self.refuse(403, "route not allowed")

    do_PUT = do_PATCH = do_DELETE = do_HEAD = do_GET

    def do_POST(self):
        try:
            size = int(self.headers.get("content-length", ""))
        except ValueError:
            return self.refuse(411, "content-length required")
        if not 0 <= size <= MAX_BODY:
            return self.refuse(413, "body too large")
        try:
            payload = json.loads(self.rfile.read(size))
        except (ValueError, RecursionError):
            return self.refuse(400, "body is not JSON")
        why = refusal("POST", self.path, payload)
        if why:
            return self.refuse(403, why)
        if not self.server.admit():
            return self.refuse(403, "request limit of this run reached")
        headers = upstream_headers(self.headers, self.server.kind, self.server.credential)
        if self.server.fake:
            return self.reply(*self.server.fake(self.path, payload))
        self.forward(payload, headers)

    def forward(self, payload, headers):
        conn = http.client.HTTPSConnection(HOST, timeout=300, context=ssl.create_default_context())
        started = False
        try:
            # re-serialized, so what was checked is exactly what is sent (no duplicate keys)
            conn.request("POST", self.path, json.dumps(payload).encode(), headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            started = True
            for name in ("content-type", "retry-after", "request-id"):
                if resp.getheader(name):
                    self.send_header(name, resp.getheader(name))
            self.send_header("connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as e:
            if not started:
                self.refuse(502, f"API unreachable ({type(e).__name__})")
        finally:
            conn.close()


def main(argv):
    kind = sys.stdin.readline().strip()
    credential = sys.stdin.readline().strip()
    if kind not in ("api_key", "oauth") or not credential:
        sys.exit("proxy: no credential on stdin")
    fake = None
    if argv == ["--fake"]:
        import fake_api
        fake = fake_api.answer
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    server = Server(SOCKET, kind, credential, fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("READY", flush=True)
    sys.stdin.read()


if __name__ == "__main__":
    main(sys.argv[1:])
