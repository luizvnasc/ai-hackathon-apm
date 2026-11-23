"""Credentials: the environment, the project's .env and the NOT CONFIGURED help."""
import os
import re
import sys


def require_config(gitlab=False, plane=False, gitlab_url=None, plane_url=None, workspace=None, scope="read_api"):
    """Stop with setup instructions (Linux/macOS and Windows) when a required variable is missing, or ask for the
    Plane workspace when neither PLANE_WORKSPACE nor the call gives one."""
    missing = []
    if gitlab:
        if not gitlab_url:
            missing.append(("GITLAB_URL", "https://gitlab.example.com"))
        if not os.environ.get("GITLAB_TOKEN"):
            missing.append(("GITLAB_TOKEN", f"<personal access token, scope {scope}: GitLab > User settings > Access tokens>"))
    if plane:
        if not plane_url:
            missing.append(("PLANE_URL", "https://plane.example.com"))
        if not os.environ.get("PLANE_API_KEY"):
            missing.append(("PLANE_API_KEY", "<API key: Plane > Workspace settings > API tokens>"))
    if not missing:
        if plane and not workspace:
            ask_for_workspace()
        return
    key, example = missing[0]
    lines = [f"NOT CONFIGURED: missing {', '.join(k for k, _ in missing)}.", "",
             "Set them as environment variables, or add them to a .env file in the project root",
             "(one KEY=value per line; keep .env out of git):", ""]
    lines += [f"  {k}={v}" for k, v in missing]
    lines += ["", "How to set an environment variable:",
              f'  Linux/macOS (bash/zsh):  export {key}="{example}"',
              f'  Windows PowerShell:      $env:{key} = "{example}"',
              f'                           persistent: setx {key} "{example}"  (then open a new terminal)',
              f"  Windows cmd:             set {key}={example}",
              "", "Restart the agent after changing environment variables (.env is read on every run), then try again."]
    print("\n".join(lines), file=sys.stderr)
    sys.exit(2)


def ask_for_workspace():
    """PLANE_WORKSPACE is optional: without it, the workspace must come from the user."""
    print("ASK THE USER: which Plane workspace holds the project? PLANE_WORKSPACE is not set, so the workspace "
          "comes from the user: it is the slug in the Plane URL, /<slug>/projects. Then call again with workspace "
          "set to that slug.", file=sys.stderr)
    sys.exit(2)


def load_dotenv(path=".env"):
    """Fill unset GITLAB_*/PLANE_* variables from the project's .env; the environment always wins.

    The .env of the current directory may come with a cloned repository, so its URL is used only with the
    token of that same file: it can never choose the server that receives a token from the environment.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    found = {}
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?((?:GITLAB|PLANE)_[A-Z0-9_]+)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            found.setdefault(key, value)
    for url_key, token_key in (("GITLAB_URL", "GITLAB_TOKEN"), ("PLANE_URL", "PLANE_API_KEY")):
        if url_key in found and not os.environ.get(url_key) and (os.environ.get(token_key) or token_key not in found):
            del found[url_key]
            print(f"IGNORED {url_key} from .env: a .env URL is only used with the {token_key} of the same file, "
                  f"never with one from the environment. Set {url_key} in the environment.", file=sys.stderr)
    for key, value in found.items():
        if not os.environ.get(key):
            os.environ[key] = value
