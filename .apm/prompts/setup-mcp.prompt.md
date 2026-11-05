---
description: Setup wizard for hackathon-metrics — configures the MCP servers it needs (GitLab) in this project.
input:
  - gitlab_url: "Optional GitLab base URL, e.g. https://gitlab.acme.io"
allowed-tools: [Bash, Read, AskUserQuestion]
---
You are the setup wizard of the **hackathon-metrics** APM package. Configure the MCP servers this package needs in the current project. Today that is only **GitLab** (its built-in MCP server at `<gitlab>/api/v4/mcp`, GitLab 18.x+). Talk to the user in their language; keep it short.

GitLab URL given by the user (may be empty): ${input:gitlab_url}

## Steps

1. **Preflight.** Run `apm --version`. If APM is missing, stop and point to https://microsoft.github.io/apm/getting-started/installation/. Check `apm.yml` in the project root: if it already declares an MCP server named `gitlab`, tell the user and ask whether to reconfigure it.

2. **Ask** (one AskUserQuestion call, skip what is already known):
   - GitLab base URL. Default to `$GITLAB_URL` if set, else `http://localhost:8929`.
   - Authentication:
     - `oauth` (recommended): log in through the browser from `/mcp`. The only option on GitLab CE.
     - `token`: personal access token with the `mcp` scope (GitLab Premium/Ultimate), read from the `GITLAB_TOKEN` env var at runtime. Never ask for the token value itself and never write it to a file.
   - Scope: this project (default) or global (user scope, `--global`).

3. **Run the wizard script** non-interactively. Find it with
   `find apm_modules -path '*ai-hackaton-apm*/scripts/setup-mcp.sh' 2>/dev/null | head -1`
   (fall back to `scripts/setup-mcp.sh` when working inside the package repo itself), then run:
   `bash <script> -y --gitlab-url <url> --auth <oauth|token> [--global]`
   If the script stops because `.mcp.json` already has a hand-written `gitlab` entry (APM never overwrites connections it did not create), show the user that entry and ask with AskUserQuestion whether to replace it. On yes, re-run the same command with `--replace-existing` (the script keeps a timestamped backup and restores it if `apm install` fails); on no, stop and leave the file untouched.
   If the script cannot be found, run the equivalent command directly:
   `apm install --mcp gitlab --transport http --url <url>/api/v4/mcp` (add `--header 'Authorization=Bearer ${GITLAB_TOKEN}'` for token auth, `--global` for user scope).

4. **Report** what was written (the script prints the files APM touched) and the next steps:
   - The new server is only loaded after Claude Code restarts: exit, start `claude` again and approve the `gitlab` server.
   - OAuth: `/mcp` → `gitlab` → **Authenticate**, log in to GitLab in the browser, click **Authorize**. `/mcp` must show `gitlab · connected`.
   - Token: `export GITLAB_TOKEN=...` in the shell before starting `claude`.

If the endpoint check warns about 404, explain that the GitLab instance is probably older than 18.x or has MCP disabled; if it cannot connect, ask the user to check that GitLab is running and the URL is right.
