# hackathon-metrics (APM package)

The hackathon deliverable: portable **agent skills** that turn **GitLab** data (Jira and Plane coming) into engineering KPIs. They are packaged with [APM](https://github.com/microsoft/apm) so any team can install them in **Claude Code**, **GitHub Copilot** or **OpenCode**.

## Skills

| Skill | What it answers | Script |
|---|---|---|
| `gitlab-repo-kpis` | KPIs of a project or group: issue lead time, throughput, net flow, backlog aging, open bugs, split by assignee and label. With MRs/pipelines in GitLab it also gives MR lead time, time to first review, MR size, pipeline success rate, duration and time to restore. | `scripts/kpis.py` |
| `gitlab-milestone-forecast` | "Will milestone X ship on time?" It reports scope, scope change, weekly throughput, burndown, cycle time and aging WIP. The forecast is deterministic plus Monte Carlo (P50/P85/P95), with a verdict and the scope cut needed. | `scripts/forecast.py` |

Ask your agent in plain language, for example "get the KPIs of acme/inventory-manager" or "will v0.3 ship on time?". The agent loads the matching skill.

How the skills stay portable:
- **Standard format.** Each skill is a plain `SKILL.md` (`name` + `description` frontmatter, no agent-specific fields).
- **Neutral tool names.** Skills name the MCP tools by their bare names, with a table of the prefix each agent uses (`mcp__gitlab__*` in Claude Code, `gitlab_*` in OpenCode, the `gitlab` server's tools in Copilot).
- **Shared calculation.** All numbers come from a bundled stdlib-only Python script (3.8+). The agent only collects data through the MCP server and writes the report, so every agent produces the same KPIs.

## Layout

```
.
  apm.yml                        # package manifest
  .apm/
    prompts/setup-mcp.prompt.md  # /setup-mcp — setup wizard inside the agent
    skills/
      gitlab-repo-kpis/          # SKILL.md + scripts/kpis.py
      gitlab-milestone-forecast/ # SKILL.md + scripts/forecast.py
  scripts/setup-mcp.sh           # setup wizard for the terminal
```

## Install in your project

```bash
# from the root of your project; pick the agents your team uses
apm install luizvnasc/ai-hackaton-apm --target claude,copilot,vscode,opencode
```

Without `--target`, APM deploys to the agents it detects in the project (`.claude/`, `.github/`, `.opencode/`, ...). The target is stored in your `apm.yml`, so later `apm install` runs and the MCP wizard reuse it.

| Agent | Target | Skills deployed to | MCP config written to |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/` | `.mcp.json` |
| GitHub Copilot (CLI / coding agent) | `copilot` | `.agents/skills/` | `.github/mcp.json` |
| GitHub Copilot Chat in VS Code | `vscode` | `.agents/skills/` | `.vscode/mcp.json` |
| OpenCode | `opencode` | `.agents/skills/` | `opencode.json` |

The skills run `python3` (or `python`) to compute the KPIs, so the agent needs a terminal tool and Python 3.8+. In VS Code, use Copilot Chat in **Agent** mode (skills and MCP tools are only available there).

or in your `apm.yml`:

```yaml
dependencies:
  apm:
    - luizvnasc/ai-hackaton-apm#main
```

## Configure the MCP servers (wizard)

The package needs the **GitLab MCP server** (built into GitLab 18.x+ at `/api/v4/mcp`). Each team points it at its own GitLab, so the wizard asks for the URL and auth method and registers the server in **your** `apm.yml` with `apm install --mcp gitlab ...`. APM then writes the config for every agent it detects (`.mcp.json` for Claude Code, `.vscode/mcp.json`, `.cursor/mcp.json`, ...).

Pick one way to run it:

1. **Inside the agent** (after `apm install`): run `/setup-mcp` (Claude Code, OpenCode, Copilot prompt file).
2. **In the terminal:**
   ```bash
   bash "$(find apm_modules -path '*ai-hackaton-apm*/scripts/setup-mcp.sh' | head -1)"
   ```
   Non-interactive: `setup-mcp.sh -y --gitlab-url https://gitlab.acme.io --auth oauth`. See `--help` for every option (`--auth token`, `--global`, `--target`, `--dry-run`, ...).
3. **Automatically on every `apm install`**: add a lifecycle hook to your own `apm.yml` and trust it once (APM skips project scripts until you run `apm lifecycle trust`):
   ```yaml
   lifecycle:
     post-install:
       - type: command
         description: "hackathon-metrics MCP setup wizard"
         bash: 'f=$(find apm_modules -path "*ai-hackaton-apm*/scripts/setup-mcp.sh" | head -1); [ -n "$f" ] && bash "$f" --if-missing'
         timeoutSec: 600
   ```
   `--if-missing` makes it a no-op once the server is declared, and a guard prevents it from re-triggering itself.

### Existing hand-written `.mcp.json` entry

APM never overwrites an MCP connection it did not create. If your `.mcp.json` already has a `gitlab` server written by hand, the wizard shows it and asks whether to replace it (non-interactive: `--replace-existing`). It keeps a timestamped backup (`.mcp.json.bak-<timestamp>`) and restores it if `apm install` fails.

### Authentication

| Mode | When | How |
|---|---|---|
| `oauth` (default) | GitLab CE (no `mcp` token scope) or anyone who prefers browser login | After the wizard: restart `claude` → `/mcp` → `gitlab` → **Authenticate** → **Authorize** in GitLab. The token is stored by the agent, not in the repo. |
| `token` | GitLab Premium/Ultimate with a PAT that has the `mcp` scope | `export GITLAB_TOKEN=...`. The config only stores `Bearer ${GITLAB_TOKEN}`; the value is read at runtime. |

### Why the MCP server is not in this package's `dependencies.mcp`

The URL of the GitLab MCP server differs per team, and APM only accepts a literal `url` for a self-defined MCP server. Placeholders such as `${GITLAB_URL}` or `${input:...}` are allowed only in `headers` and `env`. An installable package cannot ship a URL that is correct for every team, so it declares no MCP server.

The wizard writes the `gitlab` server into **your** `apm.yml` with your URL and auth, which is the path APM supports. APM then generates the config for every target: `.mcp.json`, `.github/mcp.json`, `.vscode/mcp.json`, `opencode.json`.
