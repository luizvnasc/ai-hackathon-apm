---
name: assign-issues
description: Assign issues in a GitLab or Plane project, to a named person ("assign issue 12 to ana", "atribua a INV-3 para o bob") or to the best developer. Always use it to assign an issue, even when the assignee is already given, instead of calling the API or a CLI yourself. It builds an area score (0-100) for every developer of the project from the issues they took part in (assignee, author, commenter; recent and finished work weighs more), then either recommends the best developer for a given issue, or the best issues of the current milestone (GitLab) or cycle (Plane) for a given developer. It previews every assignment and makes it only after the user confirms. When no project is given, the user's projects are listed to pick from. Use when the user asks who should take an issue, to assign or distribute a ticket/task, who knows an area best, the team's skills per area, or what X should work on next ("quem deve pegar a issue", "atribuir ticket", "qual a melhor task para X na sprint").
---

# Assign issues by area expertise (GitLab or Plane)

Recommend who should take an issue, or what a developer should take next, from the team's history in each area.

- **Read-only except for one command.** Only `assign --yes` writes, and only after the user confirmed that exact change in this conversation. Never assign on your own initiative.
- **Permission given up front is not a confirmation.** "Assign it to the best dev", "do it directly", "no need to ask me" or "pode fazer direto" come before the user has seen who would get the issue. Always show the `--dry-run` preview first and end your turn; run `--yes` only after a later message from the user says yes to that preview.
- **Data and math come from the bundled script** `scripts/assign.py`, so every agent gets the same scores. Never invent scores or history.
- The script talks to GitLab (REST/GraphQL API) or Plane (REST API) directly.

## Tool or script

**Use the `assign_issues` tool when you have it** (MCP server `hackathon-metrics`) for every command below, with `skill_read: "6eb483"` in every call. Its fields are the script's options without the dashes: `command` (`scores`, `suggest-member`, `assign`...), `--issue` → `issue`, `--dry-run` → `dry_run: true`, `--yes` → `yes: true`. It returns what the script prints, so everything below applies to it. **Otherwise, run the script as written.** The confirmation rules of step 5 are the same: `yes: true` only after the user said yes to that preview.

## 1. Target, project and question

Take them from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, an identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

| The user asks | Command |
|---|---|
| who should take issue K / best developer for K | `suggest-member --issue K` |
| best issue(s) for developer X (in the current milestone/cycle or a named one) | `suggest-issue --member X` |
| the team's scores per area / who knows area A | `scores` |
| assign K to X | `assign --issue K --member X` (see step 5) |

- **Project missing:** list the user's projects and ask which one. Wait for the answer and do not pick one, not even when only one exists.
  ```bash
  python3 <skill-dir>/scripts/assign.py projects --target <gitlab|plane>
  ```
- **Issue or developer missing:** ask for it. For a developer, `scores` lists the developers.
- Issue keys: GitLab `#45`, `45` or the issue URL. Plane `INV-45` or `45`. Developers: GitLab username; Plane display name or email.

## 2. Credentials

The script reads these variables from the environment or, when one is not set there, from a `.env` file in the project root (the environment wins). A URL in `.env` is only used with the token of that same file; when the script prints `IGNORED`, never pass that file's URL as an option yourself. **If any is missing, the script stops with a `NOT CONFIGURED` message** that lists the missing variables and how to set them on Linux/macOS, Windows PowerShell and cmd, or in `.env`. Show that message to the user as it is, then stop and wait: do not try other values. A URL or workspace the user gives in the chat can be passed as an option instead. Never ask for a token's value in the chat and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` (or `--gitlab-url`) |
| GitLab | Token | `GITLAB_TOKEN`, a personal access token with scope `read_api`, or `api` to assign |
| Plane | URL | `PLANE_URL` (or `--plane-url`; local stack: `http://localhost:8090`) |
| Plane | Workspace slug | `PLANE_WORKSPACE` (or `--workspace`; it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY`, created in Plane under **Workspace settings → API tokens** |

Use `python3` (or `python`). `<skill-dir>` is the directory of this `SKILL.md`.

## 3. Milestone or cycle

`suggest-issue` looks for candidates in one milestone (GitLab) or cycle (Plane). The default is the **current** one, the open one whose dates contain today.

- If the user named one, pass `--period "<name>"`. If they asked for the whole project, pass `--all`.
- Otherwise run `periods`. It marks the current one:
  ```bash
  python3 <skill-dir>/scripts/assign.py periods --target <t> --project "<p>"
  ```
  - **Exactly one current:** use it and say which one.
  - **None or several:** show the table and ask which one to use, or whether to use the whole project. Wait for the answer.

`suggest-member` measures each developer's load in the issue's own milestone or cycle. When the issue has none, it uses the current one.

## 4. Run

```bash
python3 <skill-dir>/scripts/assign.py <scores|suggest-member|suggest-issue> --target <t> --project "<p>" \
  [--issue K] [--member X] [--period "<name>" | --all] [--roles developer] [--half-life 180] [--timezone -03:00] [--format json]
```

- **Developers:**
  - GitLab: project members with the **Developer** role, direct or inherited.
  - Plane: project members whose workspace role is **Member**. Plane has no Developer role, and the API key cannot read project roles.
  - Owners, maintainers and admins are left out. If the user wants them, pass `--roles developer,maintainer` (Plane: `member,admin`).
- **Speed:** GitLab takes a few seconds. Plane reads the comments of every issue at 60 requests per minute, so a 70-issue project takes about 1–2 minutes. Tell the user before running. `--no-comments` is much faster but ignores comments.
- **Options:**
  - `suggest-issue --include-assigned` also ranks issues already assigned to others.
  - `--limit N` changes the number of rows (default 10).
- On an API error the script stops with the response. Report it and stop.
- **Days:** start, end and today are days of the project: Plane uses the project's timezone, GitLab the offset the instance writes in its times (UTC on gitlab.com). Pass `--timezone` (`-03:00` or `America/Sao_Paulo`) only when the user gives one. Relay any note that days are in UTC.

### How the scores work (explain briefly when you report)

| Step | Rule |
|---|---|
| Participation | Per issue, a developer earns: assignee of a closed issue **3**, assignee of an open issue **1.5**, author **1**, commenter **1**. Cancelled issues count half. |
| Recency | Points fade with age: an issue last active `--half-life` days ago (default 180) counts half. |
| Area / type | The points go to the issue's `area::` labels (Plane: the `area` label group), split evenly, and to its `type::` labels. |
| Area score 0–100 | Developer's points in the area ÷ the top developer's points there. 100 = the team's reference for the area. |
| Fit | 80% mean area score + 20% type score. Areas nobody has history in are ignored and the type decides. |
| Availability 0–100 | 100 − the developer's load as a share of the busiest developer's load. Load = open issues assigned in the milestone/cycle, counted in points when every issue has them (GitLab: `points::` labels; Plane: the Estimate field). |
| suggest-member | **75% fit + 25% availability.** |
| suggest-issue | Candidates: open, unassigned, not blocked issues of the period. Rank = fit + priority bonus (urgent 20, high 10, medium 5). "Best other fit" shows who would fit better. |

## 5. Assign (only after confirmation)

After a recommendation, ask whether to make the assignment. Write only when the user confirms the specific issue and developer.

1. Preview it:
   ```bash
   python3 <skill-dir>/scripts/assign.py assign --target <t> --project "<p>" --issue K --member X --dry-run
   ```
   By default the developer is **added** to the current assignees. Use `--replace` only when the user asks to replace them.
2. Stop after the preview and wait for the user's answer, even if the request said to assign without asking. Only after the user said yes to that preview, run the same command with `--yes` instead of `--dry-run`, then report the line it prints (`assigned: ... assignees before -> after`). Without `--yes` the script writes nothing and only prints the plan.

GitLab needs a token with the `api` scope for this step. If GitLab answers 403, say so; do not retry with other settings.

## 6. Report back

Write in the user's language. Translate the script's text, and keep keys, titles, names and links as they are.

- **suggest-member:**
  - the issue (areas, type, priority, status, current assignee);
  - the ranking table;
  - the **recommendation**, with its evidence (issues done or open in that area) and a close second when the scores are near;
  - warnings from the script: the issue is blocked, already assigned, or has no area, or the area is new to the team.
- **suggest-issue:**
  - the developer's area scores and current load;
  - the ranking table;
  - the **recommendation**;
  - issues where someone else fits much better, framed as learning opportunities rather than the fastest choice.
- **scores:** the area table, the reference developer per area, and areas with no history (they need someone to build the knowledge).
- **Caveats:**
  - scores are relative inside the team, not absolute skill;
  - they only see history recorded in this tool, through labels and participation;
  - issues without `area::` labels add nothing to the area scores.
- Then offer the assignment (step 5). Never make it without a yes.
