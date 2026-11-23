---
name: stale-issues
description: List the stale issues of a GitLab milestone or a Plane cycle (or of the whole project). An open issue is stale when it has a stale label/state or nothing changed on it for more than N days (default 14; for work not started yet the clock starts at the beginning of the milestone/cycle). Each stale issue gets a kind (stalled in progress, blocked, not started, marked stale), how long it has been idle, its assignees, priority and points, and a next step, plus the share of the open work that is stale, the split per person and the issues getting stale. When no project is given, the user's projects are listed to pick from; when no milestone or cycle is given, the open ones are listed with their stale counts. Use when the user asks about stale, idle, forgotten, abandoned or untouched issues or tickets, work with no activity or no progress ("tickets stale", "issues paradas", "o que está parado na sprint/milestone/ciclo").
---

# Stale issues (GitLab or Plane)

Find the open issues of a milestone (GitLab) or cycle (Plane) that nobody has touched for too long, and say what to do with each one.

- **Read-only.** Never change anything in GitLab or Plane: no labels, comments, assignments or moves. Suggest them in the report instead. This holds even when the user asks you to close, move, relabel or fix issues: do not write your own scripts or API calls (curl, Python, an MCP tool) to do it. Say this skill only reports, and list what the user can change by hand.
- **Data and classification come from the `stale_issues` tool**, so every agent gets the same result. Never invent issues, dates or owners.
- The tool talks to GitLab (REST/GraphQL API) or Plane (REST API) directly.

## The tool

Everything below runs through the `stale_issues` tool of the MCP server `hackathon-metrics`, with `skill_read: "618fef"` in every call. The calls are written as the tool's name, then its `command` when it has one, then its fields as options: `--name-x value` is the field `name_x`, and a flag such as `--dry-run` is `dry_run: true`. **If you do not have the tool**, tell the user that the `hackathon-metrics` MCP server is not set up (`apm install`, then approve the server in the agent) and stop. Never reach GitLab or Plane another way.

## 1. Target and project

Take both from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, an identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

If the user names no project, list their projects and ask which one. Wait for the answer, and do not pick one even when only one exists:

```text
stale_issues projects --target <gitlab|plane>
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

## 3. Scope: milestone, cycle or whole project

If the user named a milestone or cycle, use it ("the current one": the open one whose dates contain today). If they said "the whole project", use `--all`. Naming only the project ("no projeto INV", "in project X") is not the whole project. Otherwise list the open ones and ask:

```text
stale_issues periods --target <gitlab|plane> --project "<p>" [--stale-days 14]
```

- GitLab lists the active milestones; Plane the cycles not completed, archived or past their end date.
- The table shows open and stale issues per milestone/cycle and for the whole project.
- Ask: **"Which milestone/cycle should I check, or the whole project?"** Wait for the answer, even when only one exists: end your turn here and do not run the report (not even `--all`) until the user picks.

## 4. Run

```text
stale_issues report --target <t> --project "<p>" \
  (--period "<milestone title | cycle name>" | --all) [--stale-days 14] [--today YYYY-MM-DD] [--timezone -03:00] [--format json]
```

- **Threshold:** `--stale-days` (default 14). Use the user's number if they give one ("parado há mais de uma semana" → 7).
- **Days:** start, end and today are days of the project: Plane uses the project's timezone, GitLab the offset the instance writes in its times (UTC on gitlab.com). Pass `--timezone` (`-03:00` or `America/Sao_Paulo`) only when the user gives one. Relay any note that days are in UTC.
- On an API error the tool stops with the response. Report it and stop.

### Definitions (explain briefly when you report)

| Term | Meaning |
|---|---|
| Stale | Open, and a `stale` label/state, or no update for more than `--stale-days` days. An update is any change to the issue: edits, labels, assignees, state, comments (in Plane comments may not count). |
| Idle | Days since the last update. For work **not started**, the clock starts at the milestone/cycle start: waiting in the backlog before it began is not staleness. |
| Stalled | Work started (in progress, review, testing) and then stopped. The most urgent kind: it holds capacity and usually hides a problem. |
| Blocked | Marked blocked or on hold. The `blocked-issues` skill explains why and what unblocks it. |
| Not started | Still in backlog / to do / ready inside the period. |
| Marked stale | Carries a stale label or state. |
| Getting stale | Open issues idle for more than half the threshold: the next ones to watch. |

## 5. Report back

Write in the user's language. Translate the tool's text, and keep names, keys and links as they are.

1. **Header:** tool, project, scope (milestone/cycle and its dates, or whole project), the threshold and today's date. Say how many days the milestone/cycle has left, or that it already ended.
2. **Overview:** stale issues out of the open ones, their share of the open points (or issues), the split by kind and how many are unassigned.
3. **The table**, stalled first: issue (link), title, kind, status, idle days, assignees, priority, points, next step.
4. **What stands out:** for example stalled work with one person, high-priority issues never started, a period ending with much untouched work. Suggest moving out what will not fit.
5. **Per person:** the table, framed as where follow-up is needed, never as a performance ranking.
6. **Getting stale:** the issues to watch next, if any.
7. If any are blocked, offer the `blocked-issues` skill for those.
