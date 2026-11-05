---
name: gitlab-milestone-forecast
description: Delivery KPIs and a forecast for a GitLab milestone, read through the GitLab MCP server. KPIs are progress, scope change, weekly throughput, burndown, cycle time and aging WIP. The forecast is a deterministic projection plus a Monte Carlo simulation, ending in an on-track/at-risk/off-track verdict and the scope cut needed to finish on time. Use when the user asks about a milestone's status, velocity, burndown, delivery risk, or "will we finish X on time".
---

# GitLab milestone forecast

Analyze one GitLab milestone and forecast whether its remaining work will be delivered by the due date. Collect data **only** through the `gitlab` MCP server, using its read-only tools. Never call the GitLab REST API with curl, never invent data, and never modify anything in GitLab. The calculations are done by the bundled script so every agent gets the same numbers.

## MCP tool names per agent

The tools come from the MCP server named `gitlab` (set up with the `/setup-mcp` wizard of this package):

| Tool | Claude Code | GitHub Copilot (VS Code) | OpenCode |
|---|---|---|---|
| list_work_items | `mcp__gitlab__list_work_items` | `list_work_items` (server `gitlab`) | `gitlab_list_work_items` |
| get_work_item | `mcp__gitlab__get_work_item` | `get_work_item` | `gitlab_get_work_item` |
| list_projects / list_groups | `mcp__gitlab__list_projects` | `list_projects` | `gitlab_list_projects` |

If none of these tools is available, stop. Tell the user to run `/setup-mcp` (or `setup-mcp.sh`) and restart the agent.

## Workflow

1. **Inputs.** You need a milestone title and a project or group path. If one is missing, find candidates instead of guessing:
   - Call `list_projects` to find projects.
   - Call `get_work_item` on a few items to see which milestones exist.
   - Then ask the user. Optional inputs: an explicit due date, and a "today" date.

2. **Collect.**
   - Call `list_work_items` with `milestone_title: ["<title>"]`, `state: "all"`, `types: ["ISSUE", "TASK"]` and `first: 100`. Paginate with `after` until `hasNextPage` is false.
   - Then call `get_work_item` for every item to get `closedAt`, labels, assignees, weight and the milestone `startDate`/`dueDate`.

3. **Save** the items as a JSON list in a temporary file, for example `$TMPDIR/gitlab-forecast/<milestone>.json`. Never save it inside the user's repository. Use this shape per item:
   ```json
   {"iid": 44, "title": "...", "state": "CLOSED", "created": "2026-10-31T10:00:00-03:00",
    "closed": "2026-11-02T12:00:00-03:00", "labels": ["points::2", "workflow::done"],
    "assignees": ["bruno"], "milestone": "v0.3", "milestone_start": "2026-10-19",
    "milestone_due": "2026-11-15", "weight": null, "url": "..."}
   ```
   The raw `get_work_item` objects (`createdAt`, `closedAt`, `widgets`) are accepted as well.

4. **Compute.** Run the script that ships next to this file:
   ```bash
   python3 <this-skill-dir>/scripts/forecast.py --data <file> --milestone "<title>" --today <YYYY-MM-DD> [--due YYYY-MM-DD] [--unit auto|points|count]
   ```
   - Use `python` when `python3` is not found.
   - Add `--format json` when you need the raw numbers.
   - The **size unit** is chosen automatically. It uses the native `weight`, otherwise `points::N` / `sp::N` / `estimate::N` labels, but only if every item has a size. Otherwise every item counts as 1.
   - If Python is not available, follow the same method by hand and say so.

5. **Report.** Write the report in the user's language:
   - **Header:** milestone, project, start and due dates, the size unit used, and the sample size.
   - **The script's tables:** KPIs, weekly throughput with burndown, forecast and aging WIP.
   - **3–5 observations:**
     - blockers and aging items, with links
     - unassigned work
     - scope added after the start
     - how much scope to cut to reach 85% confidence
   - **Caveats:** proxy dates, low sample size, a partial current week.

## Method

| Step | Rule |
|---|---|
| Scope change | Items created after the milestone start date. Creation date is used as a proxy for the date the item was added. |
| Throughput | Size closed per ISO week, from the milestone start week up to the last complete week. Weeks with zero output count. The current partial week is used only when there are fewer than 3 complete weeks. |
| Deterministic forecast | Remaining size ÷ median weekly throughput. |
| Monte Carlo | 10,000 runs. Each simulated week draws a throughput value from the observed weeks, until the remaining size is done. Reports the P50/P85/P95 finish dates and the probability of finishing by the due date. Needs at least 3 samples; otherwise only the deterministic forecast is given, flagged as low confidence. |
| Verdict | **On track:** P85 ≤ due. **At risk:** P50 ≤ due < P85. **Off track:** P50 > due. |
| Scope cut | The size deliverable by the due date in 85% of the simulations, and how much must be cut to fit it. |
| Aging WIP | Open items older than the p85 cycle time. |
