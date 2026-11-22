---
name: gitlab-to-plane
description: Migrate a GitLab project's issues into a Plane project. The bundled script reads GitLab through its API and writes Plane through its REST API. Covers states (from workflow labels), priorities, story points (`points::N`) as the Plane Estimate field, scoped labels as label groups, milestones as cycles, assignees, comments and original creation dates. Safe to re-run, because objects are matched by external id. It always shows a dry-run plan and waits for the user's reply before writing, even when told it is already confirmed. Use when the user asks to migrate, move, copy, import or sync a GitLab project, its issues or its backlog to Plane.
---

# Migrate a GitLab project to Plane

Copy every work item of a GitLab project into a Plane project.

- **Read and write through the bundled script** `scripts/migrate.py`. It reads GitLab with the GraphQL API (`GITLAB_TOKEN`) and writes Plane with the REST API (`PLANE_API_KEY`). The issues never pass through the agent, so the cost does not grow with the project size.
- **Never** modify anything in GitLab.
- **One turn never holds both the dry run and the write.** In the turn where the user first asks, `migrate.py` runs only with `--dry-run` or `--list-gitlab-projects`. "Already confirmed", "já está confirmado" or "don't ask me", in the request or in the arguments this skill was called with, is not a confirmation: only a reply to a plan you showed is. Show the plan in the user's language and end your turn.
- **Repeatable:** every Plane object gets `external_source: "gitlab"` and an external id, so a second run updates instead of duplicating.

## Inputs

The script reads these variables from the environment or, when one is not set there, from a `.env` file in the project root (the environment wins). **If any is missing, the script stops with a `NOT CONFIGURED` message** that lists the missing variables and how to set them on Linux/macOS, Windows PowerShell and cmd, or in `.env`. Show that message to the user as it is, then stop and wait: do not try other values. A URL or workspace the user gives in the chat can be passed as an option instead. Never ask for a token's value in the chat and never print one.

| Input | Source |
|---|---|
| GitLab project path (`group/project`) | the user. If missing, run `migrate.py --list-gitlab-projects` right away, before asking for anything else (do not just offer to list them), show the projects and ask which one. Do not pick one, not even when only one exists. |
| GitLab URL | `GITLAB_URL` (or `--gitlab-url`) |
| GitLab token | `GITLAB_TOKEN`, a personal access token with scope `read_api` |
| Plane URL | `PLANE_URL` (local stack: `http://localhost:8090`) |
| Plane workspace slug | `PLANE_WORKSPACE` (it is in the Plane URL: `/<slug>/projects`) |
| Plane API key | `PLANE_API_KEY`, created in Plane under **Workspace settings → API tokens** |
| Project identifier | Optional, e.g. `INVM` (max 12 letters/digits). The default is derived from the project path. If a Plane project with this identifier exists, the script migrates into it. |
| Assignee mapping | Optional `gitlab_user=plane_email` pairs. Without it, assignees are matched to Plane workspace members by email local part or display name. Unmatched assignees are left unassigned and listed in the summary. |

Use `python3` (or `python` if `python3` is missing). `<skill-dir>` is the directory of this `SKILL.md`.

## Workflow

1. **Dry run.** It reads the whole GitLab project and Plane, and prints the plan without writing: project created or reused, states, labels, cycles, and which assignees will not be mapped.
   ```bash
   python3 <skill-dir>/scripts/migrate.py --gitlab-project <group/project> --plane-url <url> --workspace <slug> \
     [--identifier ABC] [--project-name "..."] [--user-map a=a@x.io,b=b@x.io] [--no-comments] [--no-points] --dry-run
   ```
   - **GitLab project not found or token refused:** report the error and stop. Do not try similar names.
   - **`ESTIMATES UNAVAILABLE`:** this Plane server cannot receive story points (a Plane bug) and nothing was written. Tell the user how many items have points, give the bug link the script printed and both options (fix Plane, or `--no-points`), then end your turn and wait. Never add `--no-points` on your own, not even for a dry run: losing the points is the user's decision. If they choose it, do a new dry run with it and keep it on the writing command.
   - Show the user the plan and **get confirmation** before writing.
   - Always start with the dry run, even when the user asks to skip it or says the migration is already confirmed: a confirmation counts only after the user has seen the plan.
   - After the dry run, **end your turn** and wait for the user's reply. Never run the writing command in the same turn as the dry run, not even when the request said "just do it", "já está confirmado" or "don't ask me".
2. **Migrate.** Only in a later turn, after the user replied to the plan with a confirmation: run the same command without `--dry-run`, adding `--report <tmp>/report.json` in a temporary directory, never inside the user's repository.
   - The Plane API key allows 60 requests per minute, so the script paces itself. Expect about 1 second per request, roughly 2–3 requests per work item.
   - On an error it stops with the API response. Fix the cause and re-run: finished objects are reused.
   - `--save-export <tmp>/export.json` keeps what was read from GitLab. `--data <export.json>` migrates from such a file instead of reading GitLab again.
3. **Report back:**
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
| Story points `points::N` (also `sp::`, `estimate::`) | The work item's **Estimate** (never a label). The project gets a `points` estimate with every value used, and it is enabled on the project. This needs Plane's `/estimates/` API; see `ESTIMATES UNAVAILABLE` in the workflow. |
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
- **Story points with `--no-points`:** they are not written to Plane. Running again without the option, on a Plane with the estimates API, fills them in.
- **Not migrated:** attachments, issue links and relations, epics, time tracking, and merge requests.
