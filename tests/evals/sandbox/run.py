#!/usr/bin/env python3
"""Run the skill evals in a sandbox (stdlib only, needs Docker on Linux, macOS or WSL).

    python3 sandbox/run.py --selftest                      # containment proof; no credential, no API call
    python3 sandbox/run.py <skill> [<skill> ...] [--repeat N] [--filter-pattern REGEX]

A real run needs ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), in the
environment. The local Claude Code login is deliberately never read.

  agent container  no network, no host mount, read-only root, no capabilities, not root, no real secret
        |  Unix socket in a throwaway volume
  proxy container  holds the credential (sent on stdin), forwards only Haiku Messages API calls
"""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid

HERE = pathlib.Path(__file__).resolve().parent
EVALS = HERE.parent
ROOT = EVALS.parent.parent
USER = "10001:10001"
MAX_RESULT = 64 * 1024 * 1024
SECRET_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
DUMMY = "sandbox-placeholder-not-a-credential"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
# C0/C1 controls except tab and newline: output comes from an untrusted agent and reaches a terminal
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def docker_env():
    """The docker CLI never needs the secrets, so it never gets them."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_VARS}


def docker(*args, **kw):
    return subprocess.run(["docker", *args], env=docker_env(), **kw)


def credential(environ):
    if environ.get("ANTHROPIC_API_KEY"):
        return "api_key", environ["ANTHROPIC_API_KEY"]
    if environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "oauth", environ["CLAUDE_CODE_OAUTH_TOKEN"]
    sys.exit("NOT CONFIGURED: set ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN (create one with "
             "`claude setup-token`). The local Claude Code login is never read. "
             "--selftest needs no credential.")


# What the agent needs: the skills, the MCP server with the shared code it imports, and the evals
PAYLOAD_FOLDERS = (".apm/skills", "shared", "mcp", "tests/evals")


def allowed_file(rel):
    """Whether a repository file may enter the sandbox."""
    parts = pathlib.PurePosixPath(rel).parts
    if any(p in ("node_modules", ".workspace", "output", "__pycache__", "..") or p.startswith(".env") for p in parts):
        return False
    return rel.startswith(tuple(folder + "/" for folder in PAYLOAD_FOLDERS))


def payload(root):
    """A tar of the skills and evals: regular files inside the repository that git does not ignore."""
    names = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "-co", "--exclude-standard", "--", *PAYLOAD_FOLDERS],
                           check=True, stdout=subprocess.PIPE).stdout.decode().split("\0")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in sorted(set(filter(None, names))):
            path = root / rel
            if not allowed_file(rel) or not path.is_file():
                continue
            if path.resolve() != root.resolve() / rel:  # a symlink, or below one: could point at any host file
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(rel)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def build_image():
    files = {name: (EVALS / name).read_bytes() for name in ("package.json", "package-lock.json")}
    for name in ("proxy.py", "relay.py", "probe.py", "fake_api.py", "entrypoint.sh"):
        files["sandbox/" + name] = (HERE / name).read_bytes()
    files["Dockerfile"] = (HERE / "Dockerfile").read_bytes()
    digest = hashlib.sha256()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in sorted(files.items()):
            digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    tag = "apm-evals-sandbox:" + digest.hexdigest()[:16]
    if docker("image", "inspect", tag, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        print(f"building {tag} (first run only)", file=sys.stderr, flush=True)
        if docker("build", "-q", "-t", tag, "-", input=buf.getvalue(), stdout=subprocess.DEVNULL).returncode:
            sys.exit("could not build the sandbox image")
    return tag


def hardened(name):
    return ["run", "--rm", "-i", "--init", "--name", name, "--user", USER, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--ipc", "private", "--log-driver", "none"]


def proxy_args(image, name, volume, fake):
    args = hardened(name) + ["--network", "none" if fake else "bridge", "--memory", "512m", "--pids-limit", "256",
                             "-v", f"{volume}:/run/proxy", image, "python3", "-B", "/opt/sandbox/proxy.py"]
    return args + (["--fake"] if fake else [])


def agent_args(image, name, volume, kind, command, extra_env=None):
    env = {"HOME": "/home/agent", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
           "ANTHROPIC_API_KEY" if kind == "api_key" else "CLAUDE_CODE_OAUTH_TOKEN": DUMMY,
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
           "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_UPDATE": "1",
           "PROMPTFOO_EVAL_TIMEOUT_MS": "180000", "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    args = hardened(name) + ["--network", "none", "--memory", "6g", "--cpus", "6", "--pids-limit", "2048",
                             "--tmpfs", "/sandbox:rw,nosuid,nodev,size=256m,mode=1777",
                             "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                             "--tmpfs", "/home/agent:rw,nosuid,nodev,size=512m,mode=1777",
                             "-v", f"{volume}:/run/proxy:ro"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return args + [image, "bash", "/opt/sandbox/entrypoint.sh", *command]


def clean(text):
    return CONTROL.sub("", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text))


@contextlib.contextmanager
def running_proxy(image, kind, secret, fake):
    run_id = uuid.uuid4().hex[:12]
    name, volume = f"apm-evals-proxy-{run_id}", f"apm-evals-{run_id}"
    docker("volume", "create", volume, check=True, stdout=subprocess.DEVNULL)
    proc = None
    try:
        proc = subprocess.Popen(["docker", *proxy_args(image, name, volume, fake)], env=docker_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        proc.stdin.write(f"{kind}\n{secret}\n".encode())
        proc.stdin.flush()
        if proc.stdout.readline().strip() != b"READY":
            sys.exit("the sandbox proxy did not start")
        yield volume
    finally:
        if proc:
            try:
                proc.stdin.close()
                proc.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                docker("rm", "-f", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc.kill()
        docker("volume", "rm", "-f", volume, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_agent(image, volume, kind, data, command, limit, extra_env=None):
    """Runs one config. Returns (exit code, result bytes)."""
    name = f"apm-evals-agent-{uuid.uuid4().hex[:12]}"
    proc = subprocess.Popen(["docker", *agent_args(image, name, volume, kind, command, extra_env)], env=docker_env(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    kill = lambda: docker("rm", "-f", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = bytearray()

    def read_result():
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                return
            if len(result) + len(chunk) > MAX_RESULT:
                kill()
                return
            result.extend(chunk)

    def read_log():
        while True:
            line = proc.stderr.readline(65536)
            if not line:
                return
            print(clean(line.decode(errors="replace")).rstrip("\n"), file=sys.stderr, flush=True)

    readers = [threading.Thread(target=f, daemon=True) for f in (read_result, read_log)]
    for t in readers:
        t.start()
    try:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            code = proc.wait(timeout=limit + 90)
        except subprocess.TimeoutExpired:
            kill()
            code = 124
    finally:
        if proc.poll() is None:
            kill()
            proc.kill()
    for t in readers:
        t.join(timeout=15)
    return code, bytes(result)


def host_addresses():
    found = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # sends nothing: only selects the outgoing address
            found.add(s.getsockname()[0])
    except OSError:
        pass
    gateway = docker("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}",
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()
    if gateway:
        found.add(gateway)
    return sorted(found)


def selftest(image):
    """A real agent + a fake model that tells it to attack. Canaries on the host prove what it cannot reach."""
    secret = "apm-canary-credential-" + uuid.uuid4().hex
    with socket.socket() as listener, tempfile.NamedTemporaryFile(dir=str(pathlib.Path.home()), prefix=".apm-canary-") as f:
        listener.bind(("0.0.0.0", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=3).close()  # positive control
        f.write(b"canary")
        f.flush()
        tcp = [[host, port] for host in host_addresses() + ["host.docker.internal"]]
        tcp += [["127.0.0.1", 8929], ["127.0.0.1", 8090], ["169.254.169.254", 80], ["1.1.1.1", 443]]
        target = {"tcp": tcp,
                  "paths": [f.name, str(pathlib.Path.home() / ".bashrc"), str(ROOT / "apm.yml"), "/mnt/c"],
                  "secret_markers": [base64.b64encode(b"apm-canary-").decode()]}
        with running_proxy(image, "api_key", secret, fake=True) as volume:
            code, raw = run_agent(image, volume, "api_key", payload(ROOT),
                                  ["sandbox/selftest/promptfooconfig.yaml", "240"], 240,
                                  {"SELFTEST_TARGET": base64.b64encode(json.dumps(target).encode()).decode()})
    try:
        row = json.loads(raw)["results"]["results"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        print("SELFTEST FAILED: no result")
        return 1
    print(clean(str((row.get("response") or {}).get("output") or row.get("error"))))
    ok = code == 0 and row.get("success") is True and secret.encode() not in raw
    print("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skills", nargs="*", help="the skills to evaluate (only the ones being worked on)")
    ap.add_argument("--selftest", action="store_true", help="prove the containment with a fake model")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--filter-pattern")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per skill (default 900)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(build_image())
    known = sorted(p.parent.name for p in EVALS.glob("*/promptfooconfig.yaml"))
    if not a.skills or any(not NAME.fullmatch(s) or s not in known for s in a.skills):
        ap.error("name the skills to evaluate: " + ", ".join(known))
    kind, secret = credential(os.environ)
    if "\n" in secret or "\r" in secret:
        sys.exit("the credential contains a line break")
    options = ["--repeat", str(a.repeat)] + (["--filter-pattern", a.filter_pattern] if a.filter_pattern else [])
    image = build_image()
    data = payload(ROOT)
    out = EVALS / "output"
    out.mkdir(exist_ok=True)
    failed = []
    for skill in a.skills:
        # a proxy and a volume per skill: nothing is shared between runs
        with running_proxy(image, kind, secret, fake=False) as volume:
            code, raw = run_agent(image, volume, kind, data,
                                  [f"{skill}/promptfooconfig.yaml", str(a.timeout), *options], a.timeout)
        if raw:
            (out / f"{skill}.json").write_bytes(raw)
        if code:
            failed.append(skill)
        print(f"{skill}: {'FAILED' if code else 'OK'} (output/{skill}.json)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
