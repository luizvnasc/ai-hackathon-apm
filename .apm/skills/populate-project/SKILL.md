---
name: populate-project
description: Populate a GitLab or Plane project with a standard label set (workflow, priority, type, area) and a Kanban board whose columns are the workflow labels. The target tool comes from the request ("populate project X in GitLab", "popule o projeto Y no Plane"). The default labels are shown to the user, who can change them before anything is written. Use when the user asks to populate, set up, bootstrap, initialize or seed labels or a Kanban board in a GitLab or Plane project, and also when they only ask to see or preview the default labels, what they become, or what would be created.
---

# Populate a GitLab or Plane project

Create the common labels of a project and a Kanban board built from the workflow labels.

- **Write:** only through the bundled script `scripts/populate.py`. It uses the GitLab REST API or the Plane REST API directly; no MCP server is needed.
- **Never** delete or modify anything that already exists. Existing labels, states and boards are reused as they are.
- **Nothing is written** until the user has seen the label set and approved it.
- **Permission given up front is not an approval.** "Just do it", "don't ask me anything" or "pode fazer direto" come before the user has seen the labels and the plan. Always show the label set (and the dry-run plan) and end your turn; run the command without `--dry-run` only after a later message from the user approves it.
- **One turn never holds both the dry run and the write.** In the turn where the user first asks, `populate.py` runs only with `--dry-run`, whatever the request says. If you have not yet received a reply to a plan you showed, you have no approval.
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

The script reads these variables from the environment or, when one is not set there, from a `.env` file in the project root (the environment wins). **If any is missing, the script stops with a `NOT CONFIGURED` message** that lists the missing variables and how to set them on Linux/macOS, Windows PowerShell and cmd, or in `.env`. Show that message to the user as it is, then stop and wait: do not try other values. A URL or workspace the user gives in the chat can be passed as an option instead. Never ask for a token's value in the chat and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` (or `--gitlab-url`) |
| GitLab | Token | `GITLAB_TOKEN`, a personal access token with scope `api` (it writes labels and boards) |
| Plane | URL | `PLANE_URL` (or `--plane-url`; local stack: `http://localhost:8090`) |
| Plane | Workspace slug | `PLANE_WORKSPACE` (or `--workspace`; it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY`, created in Plane under **Workspace settings → API tokens** |

Use `python3` (or `python` if `python3` is missing). `<skill-dir>` below is the directory of this `SKILL.md`.

## 3. Check the project with a dry run

Run the default label set as a dry run. It validates the credentials and the project, and shows what already exists:

```bash
python3 <skill-dir>/scripts/populate.py --target <gitlab|plane> --project "<project>" \
  --labels <skill-dir>/labels.default.json --dry-run
```

- **Quote the script's `Summary:` line as printed** (`N to create, M reused`). N counts labels, states, the board and its lists together, so call them items, never "N labels".
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

Only in a later turn, after the user replied to the plan with an approval (never in the turn of the original request, even if it said not to ask): run the command without `--dry-run`, with the approved file and `--report $TMPDIR/populate-project/report.json`:

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
