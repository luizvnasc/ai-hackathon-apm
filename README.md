# hackathon-metrics (APM package)

The hackathon deliverable: portable **agent skills** that help teams run their **GitLab** and **Plane** projects: set them up, find blocked work, assign issues by expertise, find stale work, measure the team's velocity and compare commitment with delivery. They are packaged with [APM](https://github.com/microsoft/apm) so any team can install them in **Claude Code**, **GitHub Copilot** or **OpenCode**.

## Skills

| Skill | What it answers | Script |
|---|---|---|
| `populate-project` | "Populate project X in GitLab" / "popule o projeto Y no Plane". It creates the standard labels (workflow, priority, type, area) and a Kanban board whose columns are the workflow labels. In Plane, workflow labels become states and priority uses the built-in field. It shows the default labels and asks for changes before writing. Needs `GITLAB_TOKEN` (scope `api`) or `PLANE_API_KEY`. | `scripts/populate.py` |
| `blocked-issues` | "Which issues are blocked in v0.3?" / "issues bloqueadas no ciclo X do Plane". For GitLab or Plane, it reports each blocked issue's parent, children, linked issues and comments, with how long it has been blocked, the cause and the next step. Stale issues are left out. When no project is given, it lists the projects you are a member of; when no milestone or cycle is given, it lists the open ones so you can pick one or the whole project. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/blocked.py` |
| `assign-issues` | "Who should take #62?" / "qual a melhor task para a Carla na sprint atual?". For GitLab or Plane, it scores every developer per area (0–100) from the issues they were assignee, author or commenter on, then recommends the best developer for an issue or the best issues of the current milestone/cycle for a developer. Assigns only after you confirm. Needs `GITLAB_TOKEN` (scope `read_api`, `api` to assign) or `PLANE_API_KEY`. | `scripts/assign.py` |
| `team-velocity` | "What is the team's velocity?" / "quantos pontos planejar no próximo ciclo?". For GitLab or Plane, it reports the points (or issues) delivered on time per finished milestone/cycle, velocity per week, completion, stability and trend, and velocity per person. It recommends how much to plan next and says how the current milestone/cycle is going at that pace. Weekly throughput when there are no milestones/cycles. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/velocity.py` |
| `commitment-delivered` | "Did we deliver what we committed to?" / "comprometido x entregue no ciclo X". For GitLab or Plane, it rebuilds the commitment at the start of each milestone/cycle from its history and reports how much of it was delivered by the end (say/do ratio), what was removed, cancelled or carried over, the unplanned work added mid-period, total delivered against the commitment and the scope change. It adds the trend, a reliability rating, the split per person, the current period so far and whether the next one holds more than the team usually delivers. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/commitment.py` |
| `scope-stability` | "How much did the sprint change?" / "estabilidade de escopo da sprint X". For GitLab or Plane, it rebuilds each milestone's/cycle's scope at the end of its start day and every change after it (added, removed, cancelled, re-estimated; every movement counts, end-of-sprint rollover does not) and reports stability = 1 − changed ÷ initial with a rating (≥ 90% stable, ≥ 75% moderate), the change log, when it changed and the trend. Delivery plays no part. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/stability.py` |
| `stale-issues` | "Which tickets are stale in v1.0?" / "issues paradas no ciclo X". For a GitLab milestone, a Plane cycle or the whole project, it lists the open issues with a stale label/state or no activity for more than N days (default 14; for work not started the clock starts with the milestone/cycle). Each one gets a kind (stalled in progress, blocked, not started, marked stale), idle days, owner and a next step, plus the stale share of the open work, the split per person and the issues getting stale. Read-only. Needs `GITLAB_TOKEN` (scope `read_api`) or `PLANE_API_KEY`. | `scripts/stale.py` |

Ask your agent in plain language, for example "which issues are blocked in v1.0?", "who should take #62?" or "what is stale in v1.0?". The agent loads the matching skill.

How the skills stay portable:
- **Standard format.** Each skill is a plain `SKILL.md` (`name` + `description` frontmatter, no agent-specific fields).
- **No MCP server.** The scripts call the GitLab and Plane APIs directly, so the skills do not depend on which MCP tools an agent exposes or how it names them.
- **Shared calculation.** All data access and numbers come from a bundled stdlib-only Python script (3.8+). The agent only runs it and writes the report, so every agent produces the same results.

## Token cost

Average tokens one request adds to the agent's context, per skill. The **fixed** part is paid once per request: the `SKILL.md`, which loads when the skill triggers, plus the commands and their fixed-size output. The **per-issue** part grows with the size of the project. The **answer** is what the agent writes back.

| Skill | Fixed input | Per issue | Answer (output) | Seed example (67 issues) |
|---|---|---|---|---|
| `blocked-issues` | ~2,800 | ~240 per **blocked** issue; ~0 per other issue. That is **~55 per open issue** at the seed's rate of 22% blocked | ~300 + ~250 per blocked issue | whole project, 8 blocked: ~4,800 in / ~2,300 out |
| `assign-issues` | ~2,700 + ~250–450 of results | **~0**; grows with the number of developers (~25 each) and `--limit` (~40 per row) | ~500–800 | `suggest-member`: ~3,000 in / ~600 out |
| `team-velocity` | ~1,700 + ~250 of results | **~0**; grows with periods (~60 each) and people (~15 per person per period) | ~600–900 | 2 milestones, 4 people: ~2,200 in / ~800 out |
| `commitment-delivered` | ~2,100 + ~300–700 of results | **~0**; grows with periods (~70 each) and with the issues not delivered from the commitment (~25 each, they are listed) | ~700–1,000 | 2 milestones + the current one, 4 people: ~2,800 in / ~900 out (estimated from offline seed data) |
| `scope-stability` | ~2,200 + ~500 of results | **~0** per unchanged issue; ~40 per change (one table row) | ~500–900 | 3 milestones + the current one, 17 changes: ~3,400 in / ~700 out |
| `stale-issues` | ~1,800 + ~200 of results | ~75 per **stale** issue (one table row); ~0 per other issue | ~400 + ~60 per stale issue | v0.3 on the seed date, 9 of 18 stale: ~2,700 in / ~950 out |
| `populate-project` | ~3,000, two runs included (dry run + write) | **0**, it reads no issues; ~15 per label | ~600–800 | 23 labels: ~3,400 in / ~700 out |

Why most skills are flat:
- Every skill reads the issues inside its script. Only the finished table reaches the agent.
- A 500-issue project costs about the same as the 67-issue seed. It only takes longer: Plane allows 60 requests per minute, so `assign-issues` takes about 1 s per issue there (`--no-comments` skips that).

How the numbers were measured:
- Sizes of the real `SKILL.md` files and script outputs on the demo project `acme/inventory-manager` in GitLab: 67 issues, 36 open, 8 blocked, 4 developers, 2 finished milestones.
- Tokens are estimated at 3.5 characters per token.
- Plane outputs are about 1.5× longer, because their links carry UUIDs.
- Not included: the agent's own system prompt and tool definitions. Every turn re-reads them whatever the skill, and they are usually cached.
- Run `/cost` in Claude Code before and after a request to see the real figure for your project.

## Layout

```
.
  apm.yml                        # package manifest
  .apm/
    skills/
      populate-project/          # SKILL.md + labels.default.json + scripts/populate.py
      blocked-issues/            # SKILL.md + scripts/blocked.py
      assign-issues/             # SKILL.md + scripts/assign.py
      team-velocity/             # SKILL.md + scripts/velocity.py
      commitment-delivered/      # SKILL.md + scripts/commitment.py
      scope-stability/           # SKILL.md + scripts/stability.py
      stale-issues/              # SKILL.md + scripts/stale.py
  tests/
    unit/                        # stdlib unittest for the bundled scripts (no network)
    evals/                       # promptfoo evals: skill routing and behaviour (run in a sandbox, see tests/README.md)
```

See [tests/README.md](tests/README.md) for how to run them.

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

The skills run `python3` (or `python`) for their bundled scripts, so the agent needs a terminal tool and Python 3.8+. In VS Code, use Copilot Chat in **Agent** mode (skills are only available there).

or in your `apm.yml`:

```yaml
dependencies:
  apm:
    - luizvnasc/ai-hackathon-apm#main
```

## Configure access

The skills call the GitLab and Plane APIs. Their scripts read these variables from the environment, or from a `.env` file in the project root when a variable is not set there (the environment always wins):

| Variable | For | Value |
|---|---|---|
| `GITLAB_URL` | GitLab skills | base URL, e.g. `https://gitlab.acme.io` |
| `GITLAB_TOKEN` | GitLab skills | personal access token (**User settings → Access tokens**). Scope `read_api`; `api` for `populate-project` and for assigning in `assign-issues` |
| `PLANE_URL` | Plane skills | base URL, e.g. `https://plane.acme.io` |
| `PLANE_WORKSPACE` | Plane skills | workspace slug, as in the Plane URL `/<slug>/projects` |
| `PLANE_API_KEY` | Plane skills | API key (**Workspace settings → API tokens**) |

Set only what your team uses (GitLab, Plane or both). Pick one way:

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

  Start the agent from a terminal that has the variables, or restart it after `setx`. A `.env` file needs no restart, because the scripts read it on every run.

When a skill runs and something is missing, it stops and lists the missing variables, with these commands.

### Why no MCP server

Up to 0.8.0 the package registered the GitLab MCP server. The skills outgrew it:
- **It could not do the job.** The GitLab MCP server has no tool to list milestones, returns an empty `LINKED_ITEMS` widget, gives no member roles, and cannot create labels or boards.
- **The token was needed anyway.** The skills already called the API with `GITLAB_TOKEN`.

You can still connect the GitLab MCP server to your agent for ad-hoc questions ("what is the status of #45?"). The skills just do not depend on it.
