---
name: team-velocity
description: Team velocity for a GitLab or Plane project. For the finished milestones (GitLab) or cycles (Plane) it reports the points (or issues) delivered on time, velocity per week, completion against scope, work closed late or left open, and statistics (mean, median, range, stability, trend), plus velocity per person. It recommends how much to plan for the next milestone/cycle and says how the current one is going at that pace. Teams without milestones/cycles get weekly throughput. When no project is given, the user's projects are listed to pick from. Use when the user asks for the team's velocity, throughput, capacity, how many points to plan, or how much the team delivers per sprint/milestone/cycle ("velocidade do time", "quantos pontos planejar").
---

# Team velocity (GitLab or Plane)

Measure how much the team delivers per milestone (GitLab) or cycle (Plane), and turn it into a planning number.

- **Read-only.** Never change anything in GitLab or Plane.
- **Data and math come from the bundled script** `scripts/velocity.py`, so every agent gets the same numbers. Never invent periods, points or dates.
- The script uses the GitLab REST/GraphQL API or the Plane REST API. The GitLab MCP server cannot list milestones.

## 1. Target and project

Take both from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, an identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

If the user names no project, list their projects and ask which one. Wait for the answer, and do not pick one even when only one exists:

```bash
python3 <skill-dir>/scripts/velocity.py projects --target <gitlab|plane>
```

## 2. Credentials

Ask for anything missing in a single question. Never ask for a token's value and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` env var, else derived from the `gitlab` MCP entry (`.mcp.json`, `.vscode/mcp.json` or `opencode.json`), else ask |
| GitLab | Token | `GITLAB_TOKEN` env var with the `read_api` scope. Only when it is not set in the environment and the repository's `.env` defines a non-empty value, load that file in the same shell command with `set -a; . ./.env; set +a`. Never source a `.env` when the variable is already set. |
| Plane | URL | `PLANE_URL` env var, else ask (local stack: `http://localhost:8090`) |
| Plane | Workspace slug | `PLANE_WORKSPACE` env var, else ask (it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY` env var, created in Plane under **Workspace settings → API tokens** |

Pass a missing URL or workspace with `--gitlab-url`, `--plane-url` or `--workspace`. Use `python3` (or `python`). `<skill-dir>` is the directory of this `SKILL.md`.

## 3. Run

```bash
python3 <skill-dir>/scripts/velocity.py report --target <t> --project "<p>" \
  [--last 6] [--unit auto|points|count] [--by period|week] [--weeks 8] [--format json]
```

- `periods` lists the milestones/cycles with their status: finished, current or upcoming. Use it when the user asks about specific ones.
- **History:** the last `--last` finished periods (default 6). Use the user's number if they give one.
- **Unit:**
  - `auto` uses `points::N` labels (Plane: the `points` label group) when every issue in those periods has one. Otherwise it counts issues, and the report says how many issues have no points.
  - `--unit points` counts only pointed work.
- **No finished milestones or cycles, or the user asks per week:** `--by week` gives weekly throughput over the last `--weeks` full weeks. The script also falls back to it on its own.
- On an API error the script stops with the response. Report it and stop.

### Definitions (explain briefly when you report)

| Term | Meaning |
|---|---|
| Velocity | Size of the period's issues closed by its end date. Cancelled issues do not count. |
| Per week | Velocity ÷ period length in weeks, so periods of different lengths compare fairly. |
| Scope | Every non-cancelled issue in the period **today**. The tools do not keep the scope at the start, so completion is approximate when scope changed. |
| Completion | Velocity ÷ scope. |
| Closed late / still open | Work of the period closed after its end, or not closed. |
| Stability | Coefficient of variation: < 20% stable, < 40% variable, otherwise unstable. |
| Trend | Least-squares slope per period: rising or falling when it moves more than 10% of the mean per period. |
| Per person | On-time work split evenly between the assignees. It shows how work was spread, not individual performance. |
| Next period | Plan ≈ median velocity per week × its length. Conservative = the lowest of the last 3 per week × length. Compared with what the next period already holds. |
| Current period | Done so far, pace per week against the historical median, and the expected total by the end at that median: on track, at risk (≤ 15% short) or off track. |

## 4. Report back

Write in the user's language. Translate the script's text, and keep names, keys and links as they are.

1. **Header:** tool, project, unit (points or issues), how many periods were used, and today's date.
2. **The velocity table** per milestone/cycle (or per week).
3. **Summary:** mean and median velocity, median per week, stability and trend, in one or two sentences.
4. **Planning number** for the next period, its conservative variant, and whether its current scope fits.
5. **Current period:** progress, pace against the median, and the verdict.
6. **Per person:** the table, framed as how the work was spread, never as a performance ranking.
7. **Caveats:**
   - fewer than 3 periods of history;
   - issues without points;
   - scope measured today, not at the start;
   - velocity only counts work closed by the end date.
