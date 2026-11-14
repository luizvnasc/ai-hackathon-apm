---
name: gitlab-to-plane
description: Migrate a GitLab project's issues into a Plane project. Issues are read through the GitLab MCP server and written through the Plane REST API. Covers states (from workflow labels), priorities, scoped labels as label groups, milestones as cycles, assignees, comments and original creation dates. Safe to re-run, because objects are matched by external id. Use when the user asks to migrate, move, copy, import or sync a GitLab project, its issues or its backlog to Plane.
---

# Migrate a GitLab project to Plane

Copy every work item of a GitLab project into a Plane project.

- **Read:** only through the `gitlab` MCP server, never with curl against GitLab.
- **Write:** through the bundled script, which calls the Plane REST API with an API key.
- **Never** modify anything in GitLab.
- **Repeatable:** every Plane object gets `external_source: "gitlab"` and an external id, so a second run updates instead of duplicating.

## MCP tool names per agent

| Tool | Claude Code | GitHub Copilot (VS Code) | OpenCode |
|---|---|---|---|
| get_project | `mcp__gitlab__get_project` | `get_project` (server `gitlab`) | `gitlab_get_project` |
| list_work_items | `mcp__gitlab__list_work_items` | `list_work_items` | `gitlab_list_work_items` |
| get_work_item | `mcp__gitlab__get_work_item` | `get_work_item` | `gitlab_get_work_item` |

If these tools are missing, stop. Tell the user to run `/setup-mcp` (or `setup-mcp.sh`) and restart the agent.

## Inputs

Ask for anything missing in a single question. Never ask for the API key's value.

| Input | Source |
|---|---|
| GitLab project path (`group/project`) | the user; or `list_projects` |
| Plane URL | `PLANE_URL` env var, else ask (local stack: `http://localhost:8090`) |
| Plane workspace slug | `PLANE_WORKSPACE` env var, else ask (it is in the Plane URL: `/<slug>/projects`) |
| Plane API key | `PLANE_API_KEY` env var. The user creates it in Plane under **Workspace settings → API tokens** and exports it before starting the agent. |
| Project identifier | Optional, e.g. `INVM` (max 12 letters/digits). The default is derived from the project path. If a Plane project with this identifier exists, the script migrates into it. |
| Assignee mapping | Optional `gitlab_user=plane_email` pairs. Without it, assignees are matched to Plane workspace members by email local part or display name. Unmatched assignees are left unassigned and listed in the summary. |

## Workflow

1. **Export from GitLab.**
   - Call `get_project` for the name, description and URL.
   - Call `list_work_items` with `project_id`, `state: "all"`, `types: ["ISSUE", "TASK", "INCIDENT"]` and `first: 100`. Paginate with `after` until `hasNextPage` is false.
   - For **every** item, call `get_work_item` with `include: ["notes"]`. A single call returns the description, labels with colors, assignees, milestone dates, closed date and comments.
     - If an item's notes have `hasNextPage: true`, page them with `notes_after`.
     - System notes are skipped by the script automatically.
2. **Save** the export as a temporary file, never inside the user's repository, for example `$TMPDIR/gitlab-to-plane/<project>.json`:
   ```json
   {"project": {"path": "acme/inventory-manager", "name": "Inventory Manager",
                "description": "...", "url": "http://gitlab/acme/inventory-manager"},
    "work_items": [ <the get_work_item objects, unchanged> ]}
   ```
   Keep the raw `get_work_item` objects. Do not summarize them: the script reads `widgets` itself.
3. **Dry run.** Run:
   ```bash
   python3 <this-skill-dir>/scripts/migrate.py --data <export.json> --plane-url <url> --workspace <slug> \
     [--identifier ABC] [--project-name "..."] [--user-map a=a@x.io,b=b@x.io] --dry-run
   ```
   - Use `python` if `python3` is missing.
   - The dry run reads Plane and prints the plan without writing: project created or reused, states, labels, cycles, and which assignees will not be mapped.
   - Show the user the plan and **get confirmation** before writing.
4. **Migrate.** Run the same command without `--dry-run` and with `--report <tmp>/report.json`.
   - The API key allows 60 requests per minute, so the script paces itself. Expect about 1 second per request, roughly 2–3 requests per work item.
   - On an error it stops with the Plane response. Fix the cause and re-run: finished objects are reused.
5. **Report back:**
   - the Plane project (identifier and URL `<plane-url>/<workspace>/projects/<id>/issues`);
   - created and reused counts;
   - unmapped assignees, and how to fix them: invite those people to the Plane workspace, then re-run with `--user-map`;
   - the limitations below.

## Mapping

| GitLab | Plane |
|---|---|
| Project | Project, matched by identifier, otherwise created. Cycles are enabled when there are milestones. |
| `workflow::` / `status::` label + open/closed state | State. backlog → Backlog; ready/todo → Todo; in-progress/doing → In Progress; review/qa → In Review; blocked → Blocked; closed → Done; closed + canceled/wontfix/duplicate → Cancelled; no label → Backlog. Missing states are created in the right group. |
| `priority::` label | Priority field: critical/urgent → urgent, high, medium, low. |
| Other scoped label `scope::value` | Label `value` under the parent label `scope` (for example `type` → `bug`). Values that repeat across scopes keep the full `scope::value` name. |
| Plain label | Label, keeping the GitLab color. |
| Milestone | Cycle with the same start and due dates. The milestone's items are added to it. |
| Assignee | Plane workspace member, also added to the project. Unmatched assignees are left unassigned and listed in the summary. |
| Description (Markdown) | Description with exactly the same content, converted to HTML. Nothing is added. |
| Issue URL | A link on the Plane work item, titled `GitLab group/project#iid` and pointing to the GitLab issue. |
| Comment (non-system note) | Comment, prefixed with the original author and date. |
| Created date | `created_at`, preserved. |

## Limitations to tell the user

- **Completion dates:** the Plane API does not let a closed date be set. Plane stamps closed items with the migration time, so cycle-time metrics in Plane start at the migration. The GitLab link on each item leads to the original history.
- **Comment authors:** comments are posted by the API key's user. The original author and date are written in the comment text.
- **Not migrated:** attachments, issue links and relations, epics, time tracking, and merge requests.
