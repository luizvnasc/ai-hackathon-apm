# hackathon-metrics (APM package)

The hackathon deliverable: Claude agents, skills and prompts that turn **GitLab** (Jira and Plane coming) data into engineering metrics, packaged with [APM](https://github.com/microsoft/apm) so any team can install it.

## Layout

```
apm/
  apm.yml                        # package manifest
  .apm/
    prompts/setup-mcp.prompt.md  # /setup-mcp — setup wizard inside the agent
  scripts/setup-mcp.sh           # setup wizard for the terminal
```

Agents, skills and further prompts go under `.apm/agents/*.agent.md`, `.apm/skills/<name>/SKILL.md` and `.apm/prompts/*.prompt.md`.

## Install in your project

```bash
# from the root of your project
apm install gitlab.com/<group>/ai-hackathon/apm   # path to this directory in the repo
```

or in your `apm.yml`:

```yaml
dependencies:
  apm:
    - git: https://gitlab.com/<group>/ai-hackathon.git
      path: apm
      ref: main
```

## Configure the MCP servers (wizard)

The package needs the **GitLab MCP server** (built into GitLab 18.x+ at `/api/v4/mcp`). Each team points it at its own GitLab, so the wizard asks for the URL and auth method and registers the server in **your** `apm.yml` with `apm install --mcp gitlab ...`. APM then writes the config for every agent it detects (`.mcp.json` for Claude Code, `.vscode/mcp.json`, `.cursor/mcp.json`, ...).

Pick one way to run it:

1. **Inside Claude Code** (after `apm install`): run `/setup-mcp`.
2. **In the terminal:**
   ```bash
   bash "$(find apm_modules -path '*/apm/scripts/setup-mcp.sh' | head -1)"
   ```
   Non-interactive: `setup-mcp.sh -y --gitlab-url https://gitlab.acme.io --auth oauth`. See `--help` for every option (`--auth token`, `--global`, `--target`, `--dry-run`, ...).
3. **Automatically on every `apm install`**: add a lifecycle hook to your own `apm.yml` and trust it once (APM skips project scripts until you run `apm lifecycle trust`):
   ```yaml
   lifecycle:
     post-install:
       - type: command
         description: "hackathon-metrics MCP setup wizard"
         bash: 'f=$(find apm_modules -path "*/apm/scripts/setup-mcp.sh" | head -1); [ -n "$f" ] && bash "$f" --if-missing'
         timeoutSec: 600
   ```
   `--if-missing` makes it a no-op once the server is declared, and a guard prevents it from re-triggering itself.

### Authentication

| Mode | When | How |
|---|---|---|
| `oauth` (default) | GitLab CE (no `mcp` token scope) or anyone who prefers browser login | After the wizard: restart `claude` → `/mcp` → `gitlab` → **Authenticate** → **Authorize** in GitLab. The token is stored by the agent, not in the repo. |
| `token` | GitLab Premium/Ultimate with a PAT that has the `mcp` scope | `export GITLAB_TOKEN=...`. The config only stores `Bearer ${GITLAB_TOKEN}`; the value is read at runtime. |

### Why the MCP server is not in this package's `dependencies.mcp`

APM does not install self-defined MCP servers that come from a dependency package unless the consumer passes `--trust-transitive-mcp`, and the URL differs per team anyway. Re-declaring the server in the consumer's `apm.yml` — which is what the wizard does — is the path APM recommends.
