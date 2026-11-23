# hackathon-metrics (APM package)

The hackathon deliverable: portable **agent skills** that help teams run their **GitLab** and **Plane** projects: set them up, find blocked work, assign issues by expertise, find stale work, measure the team's velocity, compare commitment with delivery and see how much delivered work went back to fix. They are packaged with [APM](https://github.com/microsoft/apm) so any team can install them in **Claude Code**, **GitHub Copilot** or **OpenCode**.

## Skills

| Skill | What it answers | Tool |
|---|---|---|
| `populate-project` | "Populate project X in GitLab" / "popule o projeto Y no Plane". It creates the standard labels (workflow, priority, type, area) and a Kanban board whose columns are the workflow labels. In Plane, workflow labels become states and priority uses the built-in field. It shows the default labels and asks for changes before writing. Needs `GITLAB_TOKEN` (scope `api`) or `PLANE_API_KEY`. | `populate_project` |
| `blocked-issues` | "Which issues are blocked in v0.3?" / "issues bloqueadas no ciclo X do Plane". For GitLab or Plane, it reports each blocked issue's parent, children, linked issues and comments, with how long it has been blocked, the cause and the next step. Stale issues are left out. When no project is given, it lists the projects you are a member of; when no milestone or cycle is given, it lists the open ones so you can pick one or the whole project. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `blocked_issues` |
| `assign-issues` | "Who should take #62?" / "qual a melhor task para a Carla na sprint atual?". For GitLab or Plane, it scores every developer per area (0–100) from the issues they were assignee, author or commenter on, then recommends the best developer for an issue or the best issues of the current milestone/cycle for a developer. Assigns only after you confirm. Needs `GITLAB_TOKEN` (scope `read_api`, `api` to assign) or `PLANE_API_KEY`. | `assign_issues` |
| `team-velocity` | "What is the team's velocity?" / "quantos pontos planejar no próximo ciclo?". For GitLab or Plane, it reports the points (or issues) delivered on time per finished milestone/cycle, velocity per week, completion, stability and trend, and velocity per person. It recommends how much to plan next and says how the current milestone/cycle is going at that pace. Weekly throughput when there are no milestones/cycles. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `team_velocity` |
| `commitment-delivered` | "Did we deliver what we committed to?" / "comprometido x entregue no ciclo X". For GitLab or Plane, it rebuilds the commitment at the start of each milestone/cycle from its history and reports how much of it was delivered by the end (say/do ratio), what was removed, cancelled or carried over, the unplanned work added mid-period, total delivered against the commitment and the scope change. It adds the trend, a reliability rating, the split per person, the current period so far and whether the next one holds more than the team usually delivers. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `commitment_delivered` |
| `scope-stability` | "How much did the sprint change?" / "estabilidade de escopo da sprint X". For GitLab or Plane, it rebuilds each milestone's/cycle's scope at the end of its start day and every change after it (added, removed, cancelled, re-estimated; every movement counts, end-of-sprint rollover does not) and reports stability = 1 − changed ÷ initial with a rating (≥ 90% stable, ≥ 75% moderate), the change log, when it changed and the trend. Delivery plays no part. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scope_stability` |
| `delivery-quality` | "How good was the sprint?" / "quantos tickets voltaram para to fix?". For GitLab or Plane, it finds every pass through the to fix state or label (`workflow::to-fix`, a To Fix state) while the issue was in the milestone/cycle, and reports quality = 1 − delivered issues that went to fix ÷ delivered issues with a rating (≥ 90% good, ≥ 75% attention), the issues that went back, and the trend. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `delivery_quality` |
| `stale-issues` | "Which tickets are stale in v1.0?" / "issues paradas no ciclo X". For a GitLab milestone, a Plane cycle or the whole project, it lists the open issues with a stale label/state or no activity for more than N days (default 14; for work not started the clock starts with the milestone/cycle). Each one gets a kind (stalled in progress, blocked, not started, marked stale), idle days, owner and a next step, plus the stale share of the open work, the split per person and the issues getting stale. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `stale_issues` |
| `kpi-report` | "Generate a KPI report with charts" / "relatório de KPIs do projeto X". For GitLab or Plane, it writes one Markdown file (`reports/kpi-report-<project>-<today>.md`) with Mermaid charts of velocity, commitment x delivered, scope stability and delivery quality over the last 6 finished milestones/cycles, plus the current one's pace and its stale and blocked issues. It runs the other skills' code, so the numbers match theirs; the chat gets the path and a summary table. Reads GitLab/Plane only; writes only that file. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `kpi_report` |

Ask your agent in plain language, for example "which issues are blocked in v1.0?", "who should take #62?" or "what is stale in v1.0?". The agent loads the matching skill.

How the skills stay portable:
- **Standard format.** Each skill is a plain `SKILL.md` (`name` + `description` frontmatter, no agent-specific fields).
- **One MCP server.** All data access and numbers come from the package's own MCP server (stdlib-only Python 3.8+), which calls the GitLab and Plane APIs directly. The skills hold only instructions: the agent calls the tool and writes the report, so every agent produces the same results.

## Token cost

Average tokens one request adds to the agent's context, per skill. The **fixed** part is paid once per request: the `SKILL.md`, which loads when the skill triggers, plus the commands and their fixed-size output. The **per-issue** part grows with the size of the project. The **answer** is what the agent writes back.

| Skill | Fixed input | Per issue | Answer (output) | Seed example (67 issues) |
|---|---|---|---|---|
| `blocked-issues` | ~2,800 | ~240 per **blocked** issue; ~0 per other issue. That is **~55 per open issue** at the seed's rate of 22% blocked | ~300 + ~250 per blocked issue | whole project, 8 blocked: ~4,800 in / ~2,300 out |
| `assign-issues` | ~2,700 + ~250–450 of results | **~0**; grows with the number of developers (~25 each) and `--limit` (~40 per row) | ~500–800 | `suggest-member`: ~3,000 in / ~600 out |
| `team-velocity` | ~1,700 + ~250 of results | **~0**; grows with periods (~60 each) and people (~15 per person per period) | ~600–900 | 2 milestones, 4 people: ~2,200 in / ~800 out |
| `commitment-delivered` | ~2,100 + ~300–700 of results | **~0**; grows with periods (~70 each) and with the issues not delivered from the commitment (~25 each, they are listed) | ~700–1,000 | 2 milestones + the current one, 4 people: ~2,800 in / ~900 out (estimated from offline seed data) |
| `scope-stability` | ~2,200 + ~500 of results | **~0** per unchanged issue; ~40 per change (one table row) | ~500–900 | 3 milestones + the current one, 17 changes: ~3,400 in / ~700 out |
| `delivery-quality` | ~1,700 + ~450 of results | **~0** per issue that never went to fix; ~25 per issue that went back (one table row) | ~400–700 | 4 cycles + the current one, 3 issues went back: ~2,200 in / ~500 out (estimated from the eval dataset) |
| `stale-issues` | ~1,800 + ~200 of results | ~75 per **stale** issue (one table row); ~0 per other issue | ~400 + ~60 per stale issue | v0.3 on the seed date, 9 of 18 stale: ~2,700 in / ~950 out |
| `kpi-report` | not measured yet | **~0**: the charts go to the file, not to the chat; grows with periods (~10 each, summary only) | not measured yet | not measured yet |
| `populate-project` | ~3,000, two runs included (dry run + write) | **0**, it reads no issues; ~15 per label | ~600–800 | 23 labels: ~3,400 in / ~700 out |

Why most skills are flat:
- Every tool reads the issues inside the MCP server. Only the finished table reaches the agent.
- A 500-issue project costs about the same as the 67-issue seed. It only takes longer: Plane allows 60 requests per minute, so `assign-issues` takes about 1 s per issue there (`--no-comments` skips that).

How the numbers were measured:
- Sizes of the real `SKILL.md` files and tool outputs on the demo project `acme/inventory-manager` in GitLab: 67 issues, 36 open, 8 blocked, 4 developers, 2 finished milestones.
- Tokens are estimated at 3.5 characters per token.
- Plane outputs are about 1.5× longer, because their links carry UUIDs.
- Not included: the agent's own system prompt and tool definitions. Every turn re-reads them whatever the skill, and they are usually cached.
- Run `/cost` in Claude Code before and after a request to see the real figure for your project.

## Layout

```
.
  apm.yml                        # package manifest, declares the MCP server
  mcp/
    server.py                    # the stdio MCP server: one tool per skill
    metrics/                     # all the code: one module per tool, plus the modules they share
  .apm/
    skills/                      # instructions only: a SKILL.md each (populate-project also has labels.default.json)
      populate-project/  blocked-issues/  assign-issues/  team-velocity/
      commitment-delivered/  scope-stability/  delivery-quality/  stale-issues/
```

The tests are not part of the package, so `apm install` never copies them: they live in the repository that holds
this package as a submodule, [luizvnasc/ai-hackathon](https://github.com/luizvnasc/ai-hackathon) (`tests/unit` and the
sandboxed promptfoo evals in `tests/evals`, see its `tests/README.md`).

### The code

| Module | What it has |
|---|---|
| `velocity`, `commitment`, `stability`, `quality`, `stale`, `blocked`, `assign`, `populate` | one per tool: its command line (`main(argv)`), what it reads and how it computes and prints the result |
| `config` | credentials: environment, `.env`, the NOT CONFIGURED help |
| `net` | `ApiError`, the redirect guard, the workspace as one path segment |
| `gitlab` | `GitLabClient`: GraphQL, REST, pages, the user's projects |
| `plane` | `PlaneClient`: rate limit, retries, pages, projects, estimates |
| `dates` | parse dates and times, the limits of a day in a timezone |
| `days` | the project's timezone and today, `--timezone` |
| `labels` | `scope::value`, points, cancelled, blocked and stale |
| `report` | Markdown tables |
| `stats` | trend |

A tool module imports the shared ones with `from .<module> import <name>`. Its `GitLab` and `Plane` classes extend the shared clients and provide `send()`, which calls the module's own `http()`. The few functions a tool module still defines differently are listed in `VARIANTS` in `tests/unit/shared_modules` of luizvnasc/ai-hackathon: a new duplicate fails there.

## Install in your project

```bash
# from the root of your project; pick the agents your team uses
apm install luizvnasc/ai-hackathon-apm --target claude,copilot,vscode,opencode
```

Without `--target`, APM deploys to the agents it detects in the project (`.claude/`, `.github/`, `.opencode/`, ...). The target is stored in your `apm.yml`, so later `apm install` runs reuse it.

| Agent | Target | Skills deployed to |
|---|---|---|
| Claude Code | `claude` | `.claude/skills/` |
| GitHub Copilot (CLI / coding agent) | `copilot` | `.agents/skills/` |
| GitHub Copilot Chat in VS Code | `vscode` | `.agents/skills/` |
| OpenCode | `opencode` | `.agents/skills/` |

The skills need the package's MCP server (below) and Python 3.8+ to run it. In VS Code, use Copilot Chat in **Agent** mode (skills are only available there).

or in your `apm.yml`:

```yaml
dependencies:
  apm:
    - luizvnasc/ai-hackathon-apm#main
```

### The MCP server

`apm install` also registers the package's MCP server, `hackathon-metrics`, in the agent's config (`.mcp.json` for Claude Code, `.github/mcp.json` for Copilot, `opencode.json` for OpenCode). The agent starts it from the project folder: `python3 apm_modules/luizvnasc/ai-hackathon-apm/mcp/server.py`.

- **Claude Code asks once** to approve a server declared in the project. Without the server the skills only say it is not set up: they never reach GitLab or Plane another way.
- **No credential goes into those files.** The server gets them from the environment the agent was started with, or from the project's `.env` (see below). Do not add `env:` entries with tokens to any of those files: `apm install` writes the value of a set variable into `.mcp.json`.

Each skill has a tool: `team_velocity`, `commitment_delivered`, `scope_stability`, `delivery_quality`, `stale_issues`, `blocked_issues`, `assign_issues` and `populate_project`. The six reports only read. `assign_issues` writes only with `yes: true` and `populate_project` writes unless `dry_run: true`: their skills preview the change and wait for the user's approval first.

Every tool call must carry `skill_read`, a value written only in its skill's `SKILL.md`. Without it the tool runs nothing and tells the agent to load the skill, so the skill's rules are always read before the tool is used.

## Configure access

The tools call the GitLab and Plane APIs. They read these variables from the environment, or from a `.env` file in the project root when a variable is not set there (the environment always wins):

| Variable | For | Value |
|---|---|---|
| `GITLAB_URL` | GitLab skills | base URL, e.g. `https://gitlab.acme.io` |
| `GITLAB_TOKEN` | GitLab skills | personal access token (**User settings → Access tokens**). Scope `read_api`; `api` for `populate-project` and for assigning in `assign-issues` |
| `PLANE_URL` | Plane skills | base URL, e.g. `https://plane.acme.io` |
| `PLANE_WORKSPACE` | Plane skills, optional | default workspace slug, as in the Plane URL `/<slug>/projects`. Without it the skill asks for the workspace; one the user names in the chat wins |
| `PLANE_API_KEY` | Plane skills | API key (**Workspace settings → API tokens**) |

Set only what your team uses (GitLab, Plane or both). The GitLab group is optional too: a project named without its group is looked up among your projects, and the skill asks which group when the name is in more than one. Pick one way:

- **`.env` file** in the project root. It works the same on Linux, macOS and Windows. Add `.env` to your `.gitignore`. Its `GITLAB_URL` / `PLANE_URL` is only used with the token in that same file: a `.env` that came with a cloned repository cannot choose the server that receives a token from your environment. Keep URL and token together, both in `.env` or both in the environment.
  ```
  GITLAB_URL=https://gitlab.acme.io
  GITLAB_TOKEN=glpat-...
  ```
- **Environment variables:**

  | Shell | Command |
  |---|---|
  | Linux/macOS (bash/zsh) | `export GITLAB_TOKEN="glpat-..."` (add it to `~/.bashrc` / `~/.zshrc` to keep it) |
  | Windows PowerShell | `$env:GITLAB_TOKEN = "glpat-..."` for the session; `setx GITLAB_TOKEN "glpat-..."` to keep it, then open a new terminal |
  | Windows cmd | `set GITLAB_TOKEN=glpat-...` for the session; `setx` as above to keep it |

  Start the agent from a terminal that has the variables, or restart it after `setx`. A `.env` file needs no restart, because the tools read it on every call.

When a skill runs and something is missing, it stops and lists the missing variables, with these commands.
