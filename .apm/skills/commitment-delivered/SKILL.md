---
name: commitment-delivered
description: Commitment x delivered (say/do ratio) for a GitLab or Plane project. For each milestone (GitLab) or cycle (Plane) it rebuilds what the team committed to at the start from the milestone/cycle history, then reports how much of that commitment was delivered by the end, what was removed, cancelled or carried over, the unplanned work added mid-period, total delivered against the commitment and the scope change, plus statistics (mean, median, trend, reliability) and the split per person. It also checks the current period and whether the next one holds more than the team usually delivers. When no project is given, the user's projects are listed to pick from. Use when the user asks about commitment vs delivery, say/do ratio, planned vs done, sprint/milestone/cycle predictability or reliability, overcommitment, or scope creep ("comprometido x entregue", "commitment x delivered", "o time entrega o que promete?").
---

# Commitment x delivered (GitLab or Plane)

Compare what the team committed to at the start of each milestone (GitLab) or cycle (Plane) with what it delivered by the end.

- **Read-only.** Never change anything in GitLab or Plane.
- **Data and math come from the bundled script** `scripts/commitment.py`, so every agent gets the same numbers. Never invent periods, points or dates.
- The script talks to GitLab (REST/GraphQL API) or Plane (REST API) directly; no MCP server is needed.

## 1. Target and project

Take both from the user's words. Never guess them.

| The request says | Target |
|---|---|
| "no gitlab", "in GitLab", a milestone, a `group/project` path, a GitLab URL | `gitlab` |
| "no plane", "in Plane", a cycle, an identifier such as `INV`, a Plane URL | `plane` |
| neither | ask: "GitLab or Plane?" |

If the user names no project, list their projects and ask which one. Wait for the answer, and do not pick one even when only one exists:

```bash
python3 <skill-dir>/scripts/commitment.py projects --target <gitlab|plane>
```

## 2. Credentials

The script reads these variables from the environment or, when one is not set there, from a `.env` file in the project root (the environment wins). **If any is missing, the script stops with a `NOT CONFIGURED` message** that lists the missing variables and how to set them on Linux/macOS, Windows PowerShell and cmd, or in `.env`. Show that message to the user as it is, then stop and wait: do not try other values. A URL or workspace the user gives in the chat can be passed as an option instead. Never ask for a token's value in the chat and never print one.

| Target | Input | Source |
|---|---|---|
| GitLab | URL | `GITLAB_URL` (or `--gitlab-url`) |
| GitLab | Token | `GITLAB_TOKEN`, a personal access token with scope `read_api` |
| Plane | URL | `PLANE_URL` (or `--plane-url`; local stack: `http://localhost:8090`) |
| Plane | Workspace slug | `PLANE_WORKSPACE` (or `--workspace`; it is in the Plane URL: `/<slug>/projects`) |
| Plane | API key | `PLANE_API_KEY`, created in Plane under **Workspace settings → API tokens** |

Use `python3` (or `python`). `<skill-dir>` is the directory of this `SKILL.md`.

## 3. Run

```bash
python3 <skill-dir>/scripts/commitment.py report --target <t> --project "<p>" \
  [--last 6] [--period "<name>" ...] [--unit auto|points|count] [--grace-days 0] [--format json]
```

- `periods` lists the milestones/cycles with their status: finished, current or upcoming. Use it when the user names one you need to match.
- **Which periods:** by default the last `--last` finished ones (default 6) plus the current one. `--period` (repeatable) analyzes only the named ones. Use the user's choice if they give one.
- **Commitment cutoff:** the end of the start day. If the team plans during the first days, pass `--grace-days N` (the user's number; ask only if they mention planning late).
- **Unit:**
  - `auto` uses `points::N` labels (Plane: the `points` label group) when every issue involved has one. Otherwise it counts issues, and the report says how many have no points.
  - `--unit points` counts only pointed work.
- **Time:** the history is read for every issue updated since the first analyzed period began (one request each). In Plane that is about 1 s per issue (60 requests per minute). Tell the user when the project is large.
- On an API error the script stops with the response. Report it and stop.

### Definitions (explain briefly when you report)

| Term | Meaning |
|---|---|
| Committed | Issues in the period at the end of its start day (+ grace days), rebuilt from the milestone events (GitLab) or cycle activity (Plane). An issue without history counts as in its current period since it was created. |
| Delivered | Committed issues closed, not cancelled, by the end of the period while still in it. |
| Say/do | Delivered ÷ committed. The main number: how much of the promise was kept. |
| Removed / Cancelled | Committed issues taken out of the period before its end, or closed as cancelled, won't fix or duplicate. They count against say/do. |
| Carried over | Committed issues still open at the end (or closed after it). For the current period: still open. |
| Added | Issues that joined after the cutoff (unplanned work), and how many of them were delivered. |
| Total delivered / vs commitment | Delivered + added delivered, and that ÷ committed. It can pass 100% when the team took on extra work. |
| Scope change | (Added − removed) ÷ committed. |
| Reliability | Median say/do over finished periods: ≥ 90% reliable, ≥ 70% fairly reliable, otherwise overcommits. |
| Trend | Least-squares slope of say/do per period, in percentage points: rising or falling beyond ±5 pp. |
| Pooled | Every delivered point ÷ every committed point, so big periods weigh more. |
| Per person | Committed and delivered work split evenly between the current assignees. It shows how commitment was spread, not individual performance. |
| Next period | Its scope today against the median delivered commitment per period. |

## 4. Report back

Write in the user's language. Translate the script's text, and keep names, keys and links as they are.

1. **Header:** tool, project, unit (points or issues), the commitment cutoff, the periods used and today's date.
2. **The table** per milestone/cycle: committed, delivered, say/do, removed, cancelled, carried over, added, total delivered, vs commitment, scope change.
3. **Summary:** median and mean say/do, reliability and trend, and how much scope usually changes, in one or two sentences.
4. **What broke the commitment:** the carried-over, removed and cancelled issues per period, with links. Point out a pattern if there is one (same area, same size, mostly added work).
5. **Current period:** say/do so far, what is still open and how much was added. Say it is still running.
6. **Next period:** whether its scope fits what the team usually delivers from a commitment; suggest committing about the median delivered when it holds much more.
7. **Per person:** the table, framed as how the commitment was spread, never as a performance ranking.
8. **Caveats:**
   - fewer than 3 finished periods;
   - issues without points;
   - no milestone/cycle history found (the commitment is then approximate);
   - points are read as they are today, not as they were at the start.
