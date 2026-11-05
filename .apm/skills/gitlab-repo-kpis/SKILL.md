---
name: gitlab-repo-kpis
description: Engineering KPIs for a GitLab project or group, read through the GitLab MCP server. Covers issue lead time, weekly throughput, backlog aging, open bugs, and the split by assignee and label. When the data exists it also covers merge request lead time, time to first review, MR size, pipeline success rate, pipeline duration and time to restore. Use when the user asks for GitLab metrics, KPIs, delivery speed, review times, pipeline health or a metrics report for a repository.
---

# GitLab repository KPIs

Build a KPI report for one or more GitLab projects (or a group). Collect data **only** through the `gitlab` MCP server. Never call the GitLab REST API with curl, never invent numbers, and never modify anything in GitLab. Compute the numbers with the bundled script so the results are the same in every agent.

## MCP tool names per agent

The tools come from the MCP server named `gitlab` (set up with the `/setup-mcp` wizard of this package). Each agent prefixes them differently:

| Tool | Claude Code | GitHub Copilot (VS Code) | OpenCode |
|---|---|---|---|
| list_projects | `mcp__gitlab__list_projects` | `list_projects` (server `gitlab`) | `gitlab_list_projects` |
| list_work_items | `mcp__gitlab__list_work_items` | `list_work_items` | `gitlab_list_work_items` |
| get_work_item | `mcp__gitlab__get_work_item` | `get_work_item` | `gitlab_get_work_item` |

Other tools follow the same pattern: `list_merge_requests`, `get_merge_request`, `list_pipelines`, `list_commits`, `list_groups`. If none of these tools is available, stop. Tell the user to run `/setup-mcp` (or `setup-mcp.sh`) and restart the agent.

## Workflow

1. **Scope.**
   - Project path(s) or group, and a time window. Default window: last 30 days for flow metrics; the backlog is always "now".
   - If no project is given, call `list_projects` and pick the project when there is only one sensible choice. Otherwise list the projects and ask the user.

2. **Collect.** Paginate every list call with `after`/`page` until it is exhausted.
   - **Work items:** call `list_work_items` (`state: "all"`, `first: 100`). Then call `get_work_item` for each item: the list call does not return `closedAt`, labels, assignees or milestone. Get the open items and the items closed inside the window; skip items closed before the window.
   - **Merge requests:** call `list_merge_requests` (`state: "all"`).
     - For merged MRs in the window, call `get_merge_request` with `include: ["diffs"]` and `detail: "none"` to get the size.
     - Call it again with `include: ["notes"]`. The first note by someone other than the author is `first_review`.
     - Skip these detail calls when there are more than 50 MRs, and say so in the report.
   - **Pipelines:** call `list_pipelines` (`per_page: 100`, `created_after` = window start). Use the project's default branch from `get_project`.
   - **Commits:** call `list_commits` (`since` = window start).

3. **Save** the data as JSON in a temporary directory, for example `$TMPDIR/gitlab-kpis/<project>/`. Never save it inside the user's repository. Use one file per source; any file may be missing. Copy values exactly as the tools return them.
   - `work_items.json`: `[{"iid", "title", "state", "created", "closed", "labels": [], "assignees": [], "milestone", "url"}]`
   - `merge_requests.json`: `[{"iid", "title", "state", "author", "created", "merged", "closed", "first_review", "additions", "deletions", "files", "url"}]`
   - `pipelines.json`: `[{"id", "status", "ref", "created", "updated", "duration"}]`
   - `commits.json`: `[{"sha", "author", "date"}]`

   Raw tool output also works: `createdAt`, `closedAt`, `widgets`, `created_at` and similar fields are normalized by the script.

4. **Compute.** Run the script that ships next to this file:
   ```bash
   python3 <this-skill-dir>/scripts/kpis.py --data <dir> --since <YYYY-MM-DD> --today <YYYY-MM-DD> [--default-branch main] [--stale-days 30]
   ```
   - Use `python` when `python3` is not found.
   - Add `--format json` when you need the raw numbers.
   - If Python is not available at all, compute the same metrics yourself and say that you did.

5. **Report.** Write the report in the user's language:
   - **Header:** project(s), window, today's date, and sample sizes.
   - **The script's tables.** Trim sections that have no data down to one line saying the metric cannot be computed and why. For example, "0 MRs → no review time".
   - **3–5 observations:**
     - trends (created vs. closed per week)
     - outliers and aging items, with links
     - open bugs older than the bug p85
     - unassigned work
     - risks
   - **Data caveats:**
     - lead time is created → closed, not true cycle time
     - small samples (n < 10) make percentiles unstable
     - any proxy dates used

## Metric definitions

| KPI | Definition |
|---|---|
| Lead time (work items) | `closedAt − createdAt` of items closed in the window. Median, p85, mean and max, also split by type (`type::*` label, else a "Bug:" title prefix, else the work item type). |
| Throughput | Items closed per ISO week (weeks start on Monday), compared with items created per week. |
| Net flow | Items created minus items closed in the window. A positive value means the backlog is growing. |
| Backlog aging | Age of the open items: median and p85, how many are older than `--stale-days`, and the 10 oldest. |
| Open bugs | Open items of type bug, flagged when they are older than the p85 lead time of closed bugs. |
| MR lead time | `merged − created` for MRs merged in the window. |
| Time to first review | `first_review − created`. |
| MR size | Median of additions + deletions, and of changed files. |
| Pipeline success rate | `success / (success + failed)` on the default branch. Falls back to all refs when the default branch has no runs. |
| Time to restore | Time from the first failed pipeline on a ref to the next successful one on that ref. |
