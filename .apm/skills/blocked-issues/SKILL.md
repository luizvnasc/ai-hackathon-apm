---
name: blocked-issues
description: Report on the blocked issues of a GitLab or Plane project, for one milestone (GitLab) or cycle (Plane) or for the whole project. When no project is given, the projects the user is a member of are listed to pick from; when no milestone or cycle is given, the open ones are listed so the user can pick one or the whole project. Stale issues are left out. For each blocked issue it reads the parent and children, the linked issues (blocked by / blocks / relates to) and the comments, then gives how long it has been blocked, the cause (dependency, parent, external, unknown) and the next step. Use when the user asks about blocked, stuck or impeded issues, blockers, impediments or dependencies ("issues bloqueadas", "o que está travado na sprint/ciclo/milestone").
---

# Blocked issues report (GitLab or Plane)

Find the blocked issues of a project and explain, for each one, why it is blocked and what unblocks it.

- **Read-only.** Never change anything in GitLab or Plane: no comments, no labels, no links, no state changes. Suggest changes in the report instead. This holds even when the user asks you to close, move, relabel or fix issues: do not write your own scripts or API calls (curl, Python, an MCP tool) to do it. Say this skill only reports, and list what the user can change by hand.
- **Data and diagnosis come from the `blocked_issues` tool**, so every agent gets the same result. Never invent issues, causes or dates.
- The tool talks to GitLab (REST/GraphQL API) or Plane (REST API) directly.

## The tool

Everything below runs through the `blocked_issues` tool of the MCP server `hackathon-metrics`, with `skill_read: "cd2ab2"` in every call. The calls are written as the tool's name, then its `command` when it has one, then its fields as options: `--name-x value` is the field `name_x`, and a flag such as `--dry-run` is `dry_run: true`. **If you do not have the tool**, tell the user that the `hackathon-metrics` MCP server is not set up (`apm install`, then approve the server in the agent) and stop. Never reach GitLab or Plane another way.

## 1. Target and project

Take both from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, a project identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

- Project: GitLab takes `group/project`, or just the project name. Plane takes the project identifier (for example `INV`) or its name.
- **If the user names no project, list the projects they are a member of** (after the credentials of step 2 are in place) and ask which one to check:
  ```text
  blocked_issues projects --target <gitlab|plane>
  ```
  - GitLab lists the non-archived projects the token's user belongs to, directly or through a group, most recently active first, with their open issue count.
  - Plane lists the projects of the workspace the API key's user is a member of.
  - Show the table and ask: **"Which project should I check?"** Wait for the answer. Do not pick one, not even when only one exists.
  - Then continue with step 3 for the chosen project.

## 2. Credentials

The tool reads these variables from the environment or, when one is not set there, from a `.env` file in the project root (the environment wins). A URL in `.env` is only used with the token of that same file; when the tool prints `IGNORED`, that file's URL is not used. **If any is missing, the tool stops with a `NOT CONFIGURED` message** that lists the missing variables and how to set them on Linux/macOS, Windows PowerShell and cmd, or in `.env`. Show that message to the user as it is, then stop and wait: do not try other values, and do not add how long the report would take or what `--deep` costs (nothing ran). A URL the user gives in the chat cannot be passed to the tool: ask the user to put it in `.env`. Never ask for a token's value in the chat and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` |
| GitLab | Token | `GITLAB_TOKEN`, a personal access token with scope `read_api` |
| Plane | URL | `PLANE_URL` (local stack: `http://localhost:8090`) |
| Plane | Workspace slug | optional: `PLANE_WORKSPACE`, or the workspace the user names (it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY`, created in Plane under **Workspace settings → API tokens** |

**Where the project is.** `PLANE_WORKSPACE` is optional, and so is the GitLab group. When the user names a Plane workspace, pass it as `--workspace <slug>` (it wins over `PLANE_WORKSPACE`). In GitLab, pass the project as the user said it: a name without its group is looked up among the user's projects. **When the tool answers `ASK THE USER:`**, ask the user that question in their language, wait for the answer, then call again with it (`--workspace <slug>`, or `--project group/project`). Never guess a workspace or a group.

## 3. Scope: milestone, cycle or whole project

If the user named a milestone (GitLab) or cycle (Plane), use it. If the user said "the whole project", use `--all`. Naming only the project ("no projeto INV", "in project X") is not the whole project.

Otherwise, list the open ones and let the user choose:

```text
blocked_issues periods --target <gitlab|plane> --project "<project>"
```

- GitLab lists the active milestones, including group milestones. Plane lists the cycles that are not completed or archived and whose end date has not passed, so current and upcoming cycles appear.
- The table shows open issues and issues marked blocked per milestone or cycle, plus a last row for the **whole project**.
- Show the table and ask: **"Which milestone/cycle should I check, or the whole project?"** Then end your turn and wait for the answer: do not run the report (not even `--all`) until the user picks. Do not pick one for the user, not even when only one exists.
- With no open milestone or cycle, say so and offer the whole project.

## 4. Run the report

```text
blocked_issues report --target <gitlab|plane> --project "<project>" \
  (--period "<milestone title | cycle name>" | --all) [--stale-days 30] [--today YYYY-MM-DD] [--timezone -03:00] [--deep]
```

- **Blocked** means open and either marked blocked (a `workflow::blocked`-style label or a Blocked / On hold state) or blocked by an open issue through a link or relation.
  - GitLab always finds issues that are only blocked by a link.
  - Plane finds them only with `--deep`, which reads the relations of every open issue (one request per issue; the API key allows 60 per minute). Add `--deep` when the user wants hidden dependencies too.
- **Stale issues are left out.** An issue is stale when it has a `stale` label or state, or when nothing changed on it for more than `--stale-days` days (default 30). They are listed at the end of the report, not analyzed. Use the user's threshold if they give one. `--stale-days 0` turns the time rule off.
- Add `--format json` when you need the raw fields.
- **Days:** start, end and today are days of the project: Plane uses the project's timezone, GitLab the offset the instance writes in its times (UTC on gitlab.com). Pass `--timezone` (`-03:00` or `America/Sao_Paulo`) only when the user gives one. Relay any note that days are in UTC.
- Plane paces itself to the rate limit: expect about 3–4 seconds per blocked issue.
- On an API error the tool stops with the response. Report it and stop. Do not try other project names.

## 5. What the tool reads for each blocked issue

| Source | GitLab | Plane | Used for |
|---|---|---|---|
| Parent | work item hierarchy (task → issue, issue → epic) | `parent` field | a blocked parent explains the block; a closed parent questions the issue |
| Children | hierarchy children | issues whose parent is this one (inside the loaded scope only) | blocked children |
| Linked issues | issue links; the direction of "blocks" links is read from the link itself, because GitLab Free reports "blocks" on both ends | relations `blocked_by`, `blocking`, `relates_to`, `duplicate` | the blockers: their state, assignee, last update, and whether they are blocked or abandoned too |
| Comments and description | non-system notes | comments | block reasons ("blocked", "waiting", "aguardando", "depende"…), issues mentioned but not linked, external tickets such as `INFRA-312` |
| Blocked since | when the blocked label was added | when the state changed to Blocked, if the activity log has it | days blocked. Fallbacks: first comment about the block, link date, last update (marked approx.) |

Causes, in order of priority: `dependency` (open blocker), `dependency resolved` (every blocker is closed), `parent blocked`, `children blocked`, `mentioned issue (not linked)`, `external`, `unknown` (nothing says why).

## 6. Report back

Write the report in the user's language. Translate the tool's text, and keep issue keys, titles, links and quoted comments as they are.

1. **Header:** tool, project, scope (milestone, cycle or whole project), date, and the stale rule used.
2. **Overview:** number of blocked issues, the split by cause, and how many stale issues were left out.
3. **The summary table** from the tool.
4. **One section per blocked issue**, as the tool prints it: status, assignee, priority, blocked since, parent, children, linked issues, comments about the block, mentions, diagnosis and next steps.
5. **3–5 observations across the issues**, for example:
   - blockers that block several issues;
   - chains, where a blocker is blocked too;
   - abandoned or unassigned blockers;
   - issues with no stated cause;
   - high-priority issues blocked the longest;
   - issues whose blockers are already closed (quick wins).
6. **Caveats:** dates marked approx.; in Plane, children outside the chosen cycle are not seen, and without `--deep` relation-only blocks are missed; comments are matched by keywords, so read the quoted ones.

Offer, without doing it, the follow-ups the report suggests: adding the missing "blocked by" links, moving issues whose blockers are closed, or asking the owners for a cause.
