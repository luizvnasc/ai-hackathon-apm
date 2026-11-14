---
name: populate-project
description: Populate a GitLab or Plane project with a standard label set (workflow, priority, type, area) and a Kanban board whose columns are the workflow labels. The target tool comes from the request ("populate project X in GitLab", "popule o projeto Y no Plane"). The default labels are shown to the user, who can change them before anything is written. Use when the user asks to populate, set up, bootstrap, initialize or seed labels or a Kanban board in a GitLab or Plane project.
---

# Populate a GitLab or Plane project

Create the common labels of a project and a Kanban board built from the workflow labels.

- **Write:** only through the bundled script `scripts/populate.py`. It uses the GitLab REST API (the GitLab MCP server has no tools to create labels or boards) or the Plane REST API.
- **Never** delete or modify anything that already exists. Existing labels, states and boards are reused as they are.
- **Nothing is written** until the user has seen the label set and approved it.
- **Repeatable:** a second run reuses what exists and creates only what is missing.

## 1. Identify the target and the project

The target and the project come from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", "gitlab project", a GitLab URL | `gitlab` |
| "no plane", "in Plane", "plane project", a Plane URL | `plane` |
| neither, or both | ask: "GitLab or Plane?" |

Examples: "popule o projeto acme/shop no gitlab" → `gitlab`, project `acme/shop`. "populate project Shop in Plane" → `plane`, project `Shop`.

- **Project:** GitLab takes `group/project` or the numeric id. Plane takes the project name or its identifier (for example `SHOP`).
- If the project is missing from the request, ask for it. Do not pick one, not even when only one exists.

## 2. Inputs and credentials

Ask for anything missing in a single question. Never ask for a token's value and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` env var, else derived from the `gitlab` entry in `.mcp.json` (`…/api/v4/mcp` is stripped), else ask |
| GitLab | Token | `GITLAB_TOKEN` env var: a personal or project access token with the `api` scope. Only when it is not set in the environment and the repository's `.env` defines a non-empty value, load that file in the same shell command with `set -a; . ./.env; set +a`. Never source a `.env` when the variable is already set: an empty entry there would erase it |
| Plane | URL | `PLANE_URL` env var, else ask (local stack: `http://localhost:8090`) |
| Plane | Workspace slug | `PLANE_WORKSPACE` env var, else ask (it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY` env var. The user creates it in Plane under **Workspace settings → API tokens** and exports it before starting the agent. |

Use `python3` (or `python` if `python3` is missing). `<skill-dir>` below is the directory of this `SKILL.md`.

## 3. Check the project with a dry run

Run the default label set as a dry run. It validates the credentials and the project, and shows what already exists:

```bash
python3 <skill-dir>/scripts/populate.py --target <gitlab|plane> --project "<project>" \
  --labels <skill-dir>/labels.default.json --dry-run
```

- **GitLab project not found or token refused:** report the error and stop. Do not try similar names.
- **Plane project not found** (exit code 3, the existing projects are listed): ask whether to create it. If yes, add `--create-project` (optionally `--identifier ABC`, at most 12 letters/digits) to every later command.

## 4. Show the default labels and ask for changes

Before writing anything, show the user the label set from `labels.default.json` as a table grouped by scope, and say what each group becomes in the target:

| Scope | Default values | GitLab | Plane |
|---|---|---|---|
| `workflow` | backlog, ready, in-progress, review, blocked, done | scoped labels + one Kanban board list per label, in this order | the **states** of the project (the Kanban columns): backlog → Backlog, ready → Todo, in-progress → In Progress, review → In Review, blocked → Blocked, done → Done |
| `priority` | critical, high, medium, low | scoped labels | Plane's built-in **Priority** field (Urgent/High/Medium/Low), so no labels. Offer `--priority-labels` if the user also wants them as labels. |
| `type` | feature, bug, chore, tech-debt, docs | scoped labels | label group `type` with child labels |
| `area` | frontend, backend, api, database, infra | scoped labels | label group `area` with child labels |

Also mention what the dry run found (labels, states or a board that already exist and will be reused).

Then ask: **"Do you want to change anything (add, remove, rename or recolor labels)?"** Point out that `area` usually depends on the project's modules. Wait for the answer.

- Apply the requested changes and show the final list again until the user approves it.
- Keep the `scope::value` naming. Colors are `#rrggbb`; pick a sensible one when the user gives none.
- Keep at least one `workflow::` label, since the Kanban columns come from them. Their order in the file is the column order.

## 5. Write the final label set

Save the approved set to a temporary file, never inside the user's repository, for example `$TMPDIR/populate-project/labels.json`. Use the same format as `labels.default.json`:

```json
{"labels": [{"name": "workflow::backlog", "color": "#dbd9d9", "description": "Not planned yet"}]}
```

If the user changed nothing, use `labels.default.json` directly.

## 6. Populate

Run the command without `--dry-run`, with the approved file and `--report $TMPDIR/populate-project/report.json`:

```bash
python3 <skill-dir>/scripts/populate.py --target <gitlab|plane> --project "<project>" \
  --labels <labels.json> [--create-project] [--priority-labels] [--board-name Kanban] \
  --report <tmp>/report.json
```

- GitLab: a board with the given name (default `Kanban`) is reused if it exists, otherwise created. If GitLab refuses to create it, the script stops with the error: report that error to the user as it is. Never put the lists on a different existing board and never retry with another name unless the user asks.
- Plane: the API key allows 60 requests per minute, so the script paces itself (about 1 second per request).
- On an error the script stops with the API response. Fix the cause and re-run: what was already created is reused.

## 7. Report back

- the project and the board URL printed by the script;
- how many labels, states and board lists were created and how many were reused;
- the script's `!` notes, for example labels whose existing color was kept, or Plane states outside the workflow that were left in place;
- Plane only: open the project's work items and pick the **Board** layout (grouped by state) to see the Kanban. The API cannot set a user's layout.
