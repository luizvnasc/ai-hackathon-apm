---
name: team-velocity
description: Team velocity for a GitLab or Plane project. For the finished milestones (GitLab) or cycles (Plane) it reports the points (or issues) delivered on time, velocity per week, completion against scope, work closed late or left open, and statistics (mean, median, range, stability, trend), plus velocity per person. It recommends how much to plan for the next milestone/cycle and says how the current one is going at that pace. Teams without milestones/cycles get weekly throughput. When no project is given, the user's projects are listed to pick from. Use when the user asks for the team's velocity, throughput, capacity, how many points to plan, or how much the team delivers per sprint/milestone/cycle ("velocidade do time", "quantos pontos planejar").
---

# Team velocity (GitLab or Plane)

Measure how much the team delivers per milestone (GitLab) or cycle (Plane), and turn it into a planning number.

- **Read-only.** Never change anything in GitLab or Plane. This holds even when the user asks you to close, move, relabel or fix issues: do not write your own scripts or API calls (curl, Python, an MCP tool) to do it. Say this skill only reports, and list what the user can change by hand.
- **Data and math come from the `team_velocity` tool**, so every agent gets the same numbers. Never invent periods, points or dates. Do not derive new figures in your own words either (ranges, averages or totals over some sprints): quote the numbers the tool printed.
- The tool talks to GitLab (REST/GraphQL API) or Plane (REST API) directly.

## The tool

Everything below runs through the `team_velocity` tool of the MCP server `hackathon-metrics`, with `skill_read: "dacb2b"` in every call. The calls are written as the tool's name, then its `command` when it has one, then its fields as options: `--name-x value` is the field `name_x`, and a flag such as `--dry-run` is `dry_run: true`. **If you do not have the tool**, tell the user that the `hackathon-metrics` MCP server is not set up (`apm install`, then approve the server in the agent) and stop. Never reach GitLab or Plane another way.

## 1. Target and project

Take both from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, an identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

If the user names no project, list their projects and ask which one. Wait for the answer, and do not pick one even when only one exists:

```text
team_velocity projects --target <gitlab|plane>
```

## 2. Credentials

The tool reads these variables from the environment or, when one is not set there, from a `.env` file in the project root (the environment wins). A URL in `.env` is only used with the token of that same file; when the tool prints `IGNORED`, that file's URL is not used. **If any is missing, the tool stops with a `NOT CONFIGURED` message** that lists the missing variables and how to set them on Linux/macOS, Windows PowerShell and cmd, or in `.env`. Show that message to the user as it is, then stop and wait: do not try other values. A URL the user gives in the chat cannot be passed to the tool: ask the user to put it in `.env`. Never ask for a token's value in the chat and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` |
| GitLab | Token | `GITLAB_TOKEN`, a personal access token with scope `read_api` |
| Plane | URL | `PLANE_URL` (local stack: `http://localhost:8090`) |
| Plane | Workspace slug | optional: `PLANE_WORKSPACE`, or the workspace the user names (it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY`, created in Plane under **Workspace settings → API tokens** |

**Where the project is.** `PLANE_WORKSPACE` is optional, and so is the GitLab group. When the user names a Plane workspace, pass it as `--workspace <slug>` (it wins over `PLANE_WORKSPACE`). In GitLab, pass the project as the user said it: a name without its group is looked up among the user's projects. **When the tool answers `ASK THE USER:`**, ask the user that question in their language, wait for the answer, then call again with it (`--workspace <slug>`, or `--project group/project`). Never guess a workspace or a group.

## 3. Run

```text
team_velocity report --target <t> --project "<p>" \
  [--last 6] [--unit auto|points|count] [--by period|week] [--weeks 8] [--timezone -03:00] [--format json]
```

- `periods` lists the milestones/cycles with their status: finished, current or upcoming. Use it when the user asks about specific ones.
- **History:** the last `--last` finished periods (default 6). Use the user's number if they give one.
- **Unit:**
  - `auto` uses points when every issue in those periods has them. Otherwise it counts issues, and the report says how many issues have no points.
  - Points come from the `points::N` label in GitLab. In Plane they come from the issue's **estimate** (Project settings → Estimates, a numeric scale), never from labels: an issue without a numeric estimate has no points.
  - `--unit points` counts only pointed work.
- **No finished milestones or cycles, or the user asks per week:** `--by week` gives weekly throughput over the last `--weeks` full weeks. The tool also falls back to it on its own.
- **Days:** start, end and today are days of the project: Plane uses the project's timezone, GitLab the offset the instance writes in its times (UTC on gitlab.com). Pass `--timezone` (`-03:00` or `America/Sao_Paulo`) only when the user gives one. Relay any note that days are in UTC.
- On an API error the tool stops with the response. Report it and stop.

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

Write in the user's language. Translate the tool's text, and keep names, keys and links as they are.

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
