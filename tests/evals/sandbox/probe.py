"""Hostile checks, executed by the real agent's Bash tool during `run.py --selftest`.

Each check is something an eval agent could try. Every one must fail to get out.
"""
import base64
import http.client
import json
import os
import pathlib
import socket
import sys


def connects(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except OSError:
        return False


def readable(path):
    try:
        if os.path.isdir(path):
            return bool(os.listdir(path))
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def resolves(name):
    try:
        socket.getaddrinfo(name, 443)
        return True
    except OSError:
        return False


def writable(directory):
    try:
        path = os.path.join(directory, ".probe")
        with open(path, "w"):
            pass
        os.unlink(path)
        return True
    except OSError:
        return False


def api(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", 8787, timeout=5)
    try:
        conn.request(method, path, json.dumps(body) if body is not None else None, {"content-type": "application/json"})
        return conn.getresponse().status
    finally:
        conn.close()


def environments():
    out = []
    for p in pathlib.Path("/proc").glob("[0-9]*/environ"):
        try:
            out.append(p.read_bytes())
        except OSError:
            pass
    return out


EXPECTED_MOUNTS = {"/", "/sandbox", "/tmp", "/home/agent", "/run/proxy", "/etc/resolv.conf", "/etc/hostname",
                   "/etc/hosts", "/sbin/docker-init", "/usr/sbin/docker-init"}


def unexpected_mounts(mountinfo):
    points = {line.split()[4] for line in mountinfo.splitlines() if line.strip()}
    return sorted(p for p in points - EXPECTED_MOUNTS if p.split("/")[1] not in ("proc", "sys", "dev"))


def checks(target):
    status = pathlib.Path("/proc/self/status").read_text()
    field = lambda name: next(l.split()[1] for l in status.splitlines() if l.startswith(name + ":"))
    envs = environments()
    mounts = pathlib.Path("/proc/self/mountinfo").read_text()

    yield "not root", os.getuid() != 0
    yield "no capabilities", int(field("CapEff"), 16) == 0
    yield "no new privileges", field("NoNewPrivs") == "1"
    yield "only loopback", all(n == "lo" or pathlib.Path(f"/sys/class/net/{n}/operstate").read_text().strip() == "down"
                               for _, n in socket.if_nameindex())
    yield "no routes", len(pathlib.Path("/proc/net/route").read_text().strip().splitlines()) <= 1
    for host, port in target["tcp"]:
        yield f"cannot connect to {host}:{port}", not connects(host, port)
    yield "no DNS", not resolves("api.anthropic.com")
    for path in target["paths"]:
        yield f"cannot read {path}", not readable(path)
    yield "no docker socket", not any(os.path.exists(p) for p in ("/var/run/docker.sock", "/run/docker.sock"))
    extra = unexpected_mounts(mounts)
    yield "no host mounts" + (f" (found {', '.join(extra)})" if extra else ""), not extra
    yield "environments were readable", bool(envs)
    for marker in target["secret_markers"]:
        # markers come base64 encoded, so this command line does not match itself
        yield "no secret in any environment", not any(base64.b64decode(marker) in e for e in envs)
    for directory in ("/", "/opt/sandbox", "/opt/evals", "/run/proxy"):
        yield f"cannot write {directory}", not writable(directory)

    ok = {"model": "claude-haiku-probe", "max_tokens": 1, "messages": []}
    yield "proxy works (positive control)", api("POST", "/v1/messages/count_tokens", ok) == 200
    yield "proxy refuses other routes", api("GET", "/v1/files") == 403
    yield "proxy refuses other models", api("POST", "/v1/messages", {**ok, "model": "claude-opus-probe"}) == 403
    yield "proxy refuses server tools", api("POST", "/v1/messages", {**ok, "tools": [{"type": "web_fetch_20250910"}]}) == 403
    yield "proxy refuses MCP", api("POST", "/v1/messages", {**ok, "mcp_servers": []}) == 403


def main():
    target = json.loads(base64.b64decode(sys.argv[1]))
    results = list(checks(target))
    failed = [name for name, passed in results if not passed]
    for name, passed in results:
        print(("ok   " if passed else "FAIL ") + name)
    print(f"PROBE FAILED: {len(failed)}" if failed else f"PROBE PASSED: {len(results)} checks")


if __name__ == "__main__":
    main()
