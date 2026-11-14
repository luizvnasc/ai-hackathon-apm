# hackathon-metrics (APM package)

The hackathon deliverable: portable **agent skills** that help teams run their **GitLab** and **Plane** projects: set them up, migrate between them, find blocked work, assign issues by expertise and measure the team's velocity. They are packaged with [APM](https://github.com/microsoft/apm) so any team can install them in **Claude Code**, **GitHub Copilot** or **OpenCode**.

## Skills

| Skill | What it answers | Script |
|---|---|---|
| `gitlab-to-plane` | "Migrate project X from GitLab to Plane." It brings over issues with states, priorities, label groups, milestones as cycles, assignees, comments and the original creation dates. It always does a dry run first. Re-running it updates instead of duplicating. Needs `PLANE_API_KEY` (Plane → Workspace settings → API tokens). | `scripts/migrate.py` |
| `populate-project` | "Populate project X in GitLab" / "popule o projeto Y no Plane". It creates the standard labels (workflow, priority, type, area) and a Kanban board whose columns are the workflow labels. In Plane, workflow labels become states and priority uses the built-in field. It shows the default labels and asks for changes before writing. Needs `GITLAB_TOKEN` (scope `api`) or `PLANE_API_KEY`. | `scripts/populate.py` |
| `blocked-issues` | "Which issues are blocked in v0.3?" / "issues bloqueadas no ciclo X do Plane". For GitLab or Plane, it reports each blocked issue's parent, children, linked issues and comments, with how long it has been blocked, the cause and the next step. Stale issues are left out. When no project is given, it lists the projects you are a member of; when no milestone or cycle is given, it lists the open ones so you can pick one or the whole project. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/blocked.py` |
| `assign-issues` | "Who should take #62?" / "qual a melhor task para a Carla na sprint atual?". For GitLab or Plane, it scores every developer per area (0–100) from the issues they were assignee, author or commenter on, then recommends the best developer for an issue or the best issues of the current milestone/cycle for a developer. Assigns only after you confirm. Needs `GITLAB_TOKEN` (scope `read_api`, `api` to assign) or `PLANE_API_KEY`. | `scripts/assign.py` |
| `team-velocity` | "What is the team's velocity?" / "quantos pontos planejar no próximo ciclo?". For GitLab or Plane, it reports the points (or issues) delivered on time per finished milestone/cycle, velocity per week, completion, stability and trend, and velocity per person. It recommends how much to plan next and says how the current milestone/cycle is going at that pace. Weekly throughput when there are no milestones/cycles. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/velocity.py` |

Ask your agent in plain language, for example "which issues are blocked in v1.0?", "who should take #62?" or "migrate acme/inventory-manager to Plane". The agent loads the matching skill.

How the skills stay portable:
- **Standard format.** Each skill is a plain `SKILL.md` (`name` + `description` frontmatter, no agent-specific fields).
- **Neutral tool names.** Skills name the MCP tools by their bare names, with a table of the prefix each agent uses (`mcp__gitlab__*` in Claude Code, `gitlab_*` in OpenCode, the `gitlab` server's tools in Copilot).
- **Shared calculation.** All data access and numbers come from a bundled stdlib-only Python script (3.8+). The agent only runs it and writes the report, so every agent produces the same results.

## Token cost

Average tokens one request adds to the agent's context, per skill. The **fixed** part is paid once per request: the `SKILL.md`, which loads when the skill triggers, plus the commands and their fixed-size output. The **per-issue** part grows with the size of the project. The **answer** is what the agent writes back.

| Skill | Fixed input | Per issue | Answer (output) | Seed example (67 issues) |
|---|---|---|---|---|
| `blocked-issues` | ~2,800 | ~240 per **blocked** issue; ~0 per other issue. That is **~55 per open issue** at the seed's rate of 22% blocked | ~300 + ~250 per blocked issue | whole project, 8 blocked: ~4,800 in / ~2,300 out |
| `assign-issues` | ~2,700 + ~250–450 of results | **~0**; grows with the number of developers (~25 each) and `--limit` (~40 per row) | ~500–800 | `suggest-member`: ~3,000 in / ~600 out |
| `team-velocity` | ~1,700 + ~250 of results | **~0**; grows with periods (~60 each) and people (~15 per person per period) | ~600–900 | 2 milestones, 4 people: ~2,200 in / ~800 out |
| `populate-project` | ~3,000, two runs included (dry run + write) | **0**, it reads no issues; ~15 per label | ~600–800 | 23 labels: ~3,400 in / ~700 out |
| `gitlab-to-plane` | ~2,000 | **~1,000 in + ~900 out**: `list_work_items` (~100) + `get_work_item` with notes (~700–1,250, about +250 per comment), then the agent writes the raw item into the export file | ~500 | 67 issues: ~69,000 in / ~61,000 out |

Why most skills are flat:
- `blocked-issues`, `assign-issues`, `team-velocity` and `populate-project` read the issues inside their script. Only the finished table reaches the agent.
- A 500-issue project costs those four skills about the same as the 67-issue seed. It only takes longer: Plane allows 60 requests per minute, so `assign-issues` takes about 1 s per issue there (`--no-comments` skips that).
- `gitlab-to-plane` is the exception. It exports through the GitLab MCP server, so every issue passes through the agent twice: read, then written to the export file. Each tool call also re-reads the whole conversation, so the cumulative input grows faster than linearly. With prompt caching most of it is billed as cache reads, but a project with hundreds of issues should be migrated in batches.

How the numbers were measured:
- Sizes of the real `SKILL.md` files, script outputs and MCP responses on the demo project `acme/inventory-manager` in GitLab: 67 issues, 36 open, 8 blocked, 4 developers, 2 finished milestones.
- Tokens are estimated at 3.5 characters per token.
- Plane outputs are about 1.5× longer, because their links carry UUIDs.
- Not included: the agent's own system prompt and tool definitions. Every turn re-reads them whatever the skill, and they are usually cached.
- Run `/cost` in Claude Code before and after a request to see the real figure for your project.

## Layout

```
.
  apm.yml                        # package manifest
  .apm/
    prompts/setup-mcp.prompt.md  # /setup-mcp — setup wizard inside the agent
    skills/
      gitlab-to-plane/           # SKILL.md + scripts/migrate.py
      populate-project/          # SKILL.md + labels.default.json + scripts/populate.py
      blocked-issues/            # SKILL.md + scripts/blocked.py
      assign-issues/             # SKILL.md + scripts/assign.py
      team-velocity/             # SKILL.md + scripts/velocity.py
  scripts/setup-mcp.sh           # setup wizard for the terminal
```

## Install in your project

```bash
# from the root of your project; pick the agents your team uses
apm install luizvnasc/ai-hackathon-apm --target claude,copilot,vscode,opencode
```

Without `--target`, APM deploys to the agents it detects in the project (`.claude/`, `.github/`, `.opencode/`, ...). The target is stored in your `apm.yml`, so later `apm install` runs and the MCP wizard reuse it.

| Agent | Target | Skills deployed to | MCP config written to |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/` | `.mcp.json` |
| GitHub Copilot (CLI / coding agent) | `copilot` | `.agents/skills/` | `.github/mcp.json` |
| GitHub Copilot Chat in VS Code | `vscode` | `.agents/skills/` | `.vscode/mcp.json` |
| OpenCode | `opencode` | `.agents/skills/` | `opencode.json` |

The skills run `python3` (or `python`) for their bundled scripts, so the agent needs a terminal tool and Python 3.8+. In VS Code, use Copilot Chat in **Agent** mode (skills and MCP tools are only available there).

or in your `apm.yml`:

```yaml
dependencies:
  apm:
    - luizvnasc/ai-hackathon-apm#main
```

## Configure the MCP servers (wizard)

The package needs the **GitLab MCP server** (built into GitLab 18.x+ at `/api/v4/mcp`). Each team points it at its own GitLab, so the wizard asks for the URL and auth method and registers the server in **your** `apm.yml` with `apm install --mcp gitlab ...`. APM then writes the config for every agent it detects (`.mcp.json` for Claude Code, `.vscode/mcp.json`, `.cursor/mcp.json`, ...).

Pick one way to run it:

1. **Inside the agent** (after `apm install`): run `/setup-mcp` (Claude Code, OpenCode, Copilot prompt file).
2. **In the terminal:**
   ```bash
   bash "$(find apm_modules -path '*ai-hacka*on-apm*/scripts/setup-mcp.sh' | head -1)"
   ```
   Non-interactive: `setup-mcp.sh -y --gitlab-url https://gitlab.acme.io --auth oauth`. See `--help` for every option (`--auth token`, `--global`, `--target`, `--dry-run`, ...).
3. **Automatically on every `apm install`**: add a lifecycle hook to your own `apm.yml` and trust it once (APM skips project scripts until you run `apm lifecycle trust`):
   ```yaml
   lifecycle:
     post-install:
       - type: command
         description: "hackathon-metrics MCP setup wizard"
         bash: 'f=$(find apm_modules -path "*ai-hacka*on-apm*/scripts/setup-mcp.sh" | head -1); [ -n "$f" ] && bash "$f" --if-missing'
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
