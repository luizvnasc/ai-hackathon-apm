---
name: kpi-report
description: Write a KPI report of a GitLab or Plane project as a Markdown file with Mermaid charts, bringing together the metrics of the other skills in one document - velocity, commitment x delivered (say/do), scope stability and delivery quality over the last milestones (GitLab) or cycles (Plane), plus the stale and blocked issues of the current one. The file goes to reports/ in the project folder and the chat gets the path and a summary table. When no project is given, the user's projects are listed to pick from. Use when the user asks for a KPI report, a metrics report or dashboard in a file, a .md with charts, or an overview of all the team's metrics at once ("relatório de KPIs", "gere um md com gráficos dos indicadores", "report of the main KPIs"). For a single metric asked in the chat, use that metric's skill instead.
---

# KPI report (GitLab or Plane)

Write one Markdown file with charts of the team's main KPIs, so it can be shared, committed or opened in GitLab/GitHub.

- **Reads GitLab and Plane, writes only the report file.** Never change anything in GitLab or Plane. This holds even when the user asks you to close, move, relabel or fix issues: do not write your own scripts or API calls (curl, Python, an MCP tool) to do it. Say this skill only reports, and list what the user can change by hand.
- **Data, math and the file come from the `kpi_report` tool.** It runs the same code as the velocity, commitment-delivered, scope-stability, delivery-quality, stale-issues and blocked-issues skills, so the numbers match theirs. Never invent numbers, periods or dates, do not compute new figures, and do not write or edit the report file yourself.
- The tool talks to GitLab (REST/GraphQL API) or Plane (REST API) directly.

## The tool

Everything below runs through the `kpi_report` tool of the MCP server `hackathon-metrics`, with `skill_read: "9a0497"` in every call. The calls are written as the tool's name, then its `command` when it has one, then its fields as options: `--name-x value` is the field `name_x`, and a flag such as `--dry-run` is `dry_run: true`. **If you do not have the tool**, tell the user that the `hackathon-metrics` MCP server is not set up (`apm install`, then approve the server in the agent) and stop. Never reach GitLab or Plane another way.

## 1. Target and project

Take both from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, an identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

If the user names no project, list their projects and ask which one. Wait for the answer, and do not pick one even when only one exists:

```text
kpi_report projects --target <gitlab|plane>
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
kpi_report report --target <t> --project "<p>" [--last 6] [--stale-days N] [--output reports/name.md] [--timezone -03:00]
```

- **History:** the charts cover the last `--last` finished milestones/cycles (default 6). Use the user's number if they give one.
- **Right now:** the stale and blocked issues of the current milestone/cycle; the whole project when none is running. `--stale-days` only when the user gives a threshold (otherwise each tool's default: 14 days for stale, 30 for leaving stale issues out of the blocked list).
- **File:** `reports/kpi-report-<project>-<today>.md` in the project folder. Pass `--output` only when the user names a path; it must be relative, end in `.md` and stay inside the project folder. A second run on the same day replaces that day's file.
- **Speed:** the tool runs six reports. GitLab takes seconds; Plane paces itself to 60 requests per minute, so a large project takes several minutes. Tell the user before running on Plane.
- **Days:** Plane uses the project's timezone, GitLab the offset the instance writes in its times (UTC on gitlab.com). Pass `--timezone` only when the user gives one.
- On an API error the tool stops with the response. Report it and stop.

## 4. Report back

Write in the user's language. Translate the tool's text, and keep names, keys and links as they are.

1. **Where the file is:** the path the tool printed.
2. **The summary table** as the tool printed it: latest, median, trend and rating per KPI, and the stale/blocked counts now.
3. **What is in the file:** one Mermaid chart per KPI (velocity with scope, say/do, scope stability, delivery quality), the current period's pace, and pies of stale issues by kind and blocked issues by cause. Do not paste the charts into the chat.
4. **Caveats:** Mermaid renders in GitLab, GitHub and most Markdown viewers, not in every editor; the notes at the end of the file explain approximate numbers (issues without history or points). For the detail of one KPI, offer that KPI's skill.
