#!/usr/bin/env python3
"""Compute engineering KPIs from GitLab data collected through the GitLab MCP server.

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. The agent collects the data with the MCP tools, writes it as JSON
files into one directory and runs:

    python3 kpis.py --data DIR [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                    [--today YYYY-MM-DD] [--stale-days 30] [--default-branch main]
                    [--format md|json]

Every file is optional; metrics whose input is missing are reported as "n/a".

  work_items.json      list of work items. Either the compact shape
                       {"iid", "title", "state", "created", "closed", "type",
                        "labels": [...], "assignees": [...], "milestone", "url"}
                       or the raw objects returned by get_work_item / list_work_items
                       (createdAt, closedAt, widgets, ...) — both are accepted.
  merge_requests.json  list of {"iid", "title", "state", "author", "created",
                       "merged", "closed", "first_review", "additions",
                       "deletions", "files", "url"}  (raw camelCase also accepted)
  pipelines.json       list of {"id", "status", "ref", "created", "updated",
                       "duration"}  (raw REST shape with created_at etc. accepted)
  commits.json         list of {"sha", "author", "date"} (raw committedDate accepted)
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

DAY = 86400.0


# ---------------------------------------------------------------- helpers
def parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick(obj, *keys):
    for k in keys:
        if isinstance(obj, dict) and obj.get(k) not in (None, ""):
            return obj[k]
    return None


def pct(values, p):
    """Nearest-rank percentile."""
    if not values:
        return None
    v = sorted(values)
    k = max(0, math.ceil(p / 100.0 * len(v)) - 1)
    return v[k]


def median(values):
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def fmt_days(x):
    return "n/a" if x is None else f"{x:.1f} d"


def fmt_hours(x):
    if x is None:
        return "n/a"
    return f"{x * 24:.1f} h" if x < 2 else f"{x:.1f} d"


def dist(values, fmt=fmt_days):
    if not values:
        return {"n": 0, "median": "n/a", "p85": "n/a", "mean": "n/a", "max": "n/a"}
    return {
        "n": len(values),
        "median": fmt(median(values)),
        "p85": fmt(pct(values, 85)),
        "mean": fmt(sum(values) / len(values)),
        "max": fmt(max(values)),
    }


def week_start(d):
    d = d.date() if isinstance(d, datetime) else d
    return d - timedelta(days=d.weekday())


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):  # tolerate raw tool responses
        for key in ("work_items", "nodes", "items", "merge_requests", "pipelines", "commits"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data


def in_window(d, since, until):
    return d is not None and (since is None or d >= since) and (until is None or d < until)


# ---------------------------------------------------------------- normalizers
def norm_work_item(raw):
    labels = raw.get("labels")
    assignees = raw.get("assignees")
    milestone = raw.get("milestone")
    weight = raw.get("weight")
    for w in raw.get("widgets") or []:
        t = w.get("type")
        if t == "LABELS" and labels is None:
            labels = [n["title"] for n in (w.get("labels") or {}).get("nodes", [])]
        elif t == "ASSIGNEES" and assignees is None:
            assignees = [n.get("username") or n.get("name") for n in (w.get("assignees") or {}).get("nodes", [])]
        elif t == "MILESTONE" and milestone is None and w.get("milestone"):
            milestone = w["milestone"].get("title")
        elif t == "WEIGHT" and weight is None:
            weight = w.get("weight")
    if isinstance(milestone, dict):
        milestone = milestone.get("title")
    labels = [l["title"] if isinstance(l, dict) else l for l in (labels or [])]
    assignees = [a.get("username") if isinstance(a, dict) else a for a in (assignees or [])]
    title = raw.get("title") or ""
    kind = raw.get("type")
    if not kind:
        scoped = [l.split("::", 1)[1] for l in labels if l.lower().startswith("type::")]
        if scoped:
            kind = scoped[0]
        elif re.match(r"\s*\[?bug\]?[:\s]", title, re.I) or any(l.lower() == "bug" for l in labels):
            kind = "bug"
        else:
            wit = raw.get("workItemType")
            kind = (wit.get("name") if isinstance(wit, dict) else wit) or "issue"
    state = str(raw.get("state") or "").lower()
    closed = parse_dt(pick(raw, "closed", "closedAt", "closed_at"))
    return {
        "iid": raw.get("iid"),
        "title": title,
        "state": "closed" if state == "closed" or closed else "opened",
        "created": parse_dt(pick(raw, "created", "createdAt", "created_at")),
        "closed": closed,
        "closed_is_proxy": closed is None and state == "closed",
        "updated": parse_dt(pick(raw, "updated", "updatedAt", "updated_at")),
        "type": str(kind).lower(),
        "labels": labels,
        "assignees": [a for a in assignees if a],
        "milestone": milestone,
        "url": pick(raw, "url", "webUrl", "web_url"),
    }


def norm_mr(raw):
    author = raw.get("author")
    if isinstance(author, dict):
        author = author.get("username") or author.get("name")
    return {
        "iid": raw.get("iid"),
        "title": raw.get("title") or "",
        "state": str(raw.get("state") or "").lower(),
        "author": author,
        "created": parse_dt(pick(raw, "created", "createdAt", "created_at")),
        "merged": parse_dt(pick(raw, "merged", "mergedAt", "merged_at")),
        "closed": parse_dt(pick(raw, "closed", "closedAt", "closed_at")),
        "first_review": parse_dt(pick(raw, "first_review", "firstReview")),
        "additions": pick(raw, "additions"),
        "deletions": pick(raw, "deletions"),
        "files": pick(raw, "files", "changed_files", "fileCount"),
        "url": pick(raw, "url", "webUrl", "web_url"),
    }


def norm_pipeline(raw):
    return {
        "id": raw.get("id"),
        "status": str(raw.get("status") or "").lower(),
        "ref": raw.get("ref"),
        "created": parse_dt(pick(raw, "created", "created_at", "createdAt")),
        "updated": parse_dt(pick(raw, "updated", "updated_at", "updatedAt", "finished_at")),
        "duration": pick(raw, "duration"),
    }


def norm_commit(raw):
    return {
        "sha": pick(raw, "sha", "id"),
        "author": pick(raw, "author", "authorName", "author_name"),
        "date": parse_dt(pick(raw, "date", "committedDate", "committed_date", "authoredDate")),
    }


# ---------------------------------------------------------------- KPI blocks
def work_item_kpis(items, since, until, now, stale_days):
    out = {"available": items is not None}
    if items is None:
        return out
    items = [norm_work_item(i) for i in items]
    created_in = [i for i in items if in_window(i["created"], since, until)]
    closed_in = [i for i in items if i["state"] == "closed" and in_window(i["closed"] or i["updated"], since, until)]
    open_now = [i for i in items if i["state"] == "opened"]

    def lead(i):
        end = i["closed"] or i["updated"]
        return (end - i["created"]).total_seconds() / DAY if end and i["created"] else None

    lt = [x for x in (lead(i) for i in closed_in) if x is not None]
    by_type = defaultdict(list)
    for i in closed_in:
        x = lead(i)
        if x is not None:
            by_type[i["type"]].append(x)

    ages = sorted((((now - i["created"]).total_seconds() / DAY, i) for i in open_now if i["created"]), key=lambda x: x[0])
    weekly = defaultdict(lambda: [0, 0])
    for i in created_in:
        weekly[week_start(i["created"])][0] += 1
    for i in closed_in:
        weekly[week_start(i["closed"] or i["updated"])][1] += 1

    assignee = defaultdict(lambda: [0, 0])
    for i in items:
        for a in i["assignees"] or ["(unassigned)"]:
            assignee[a][0 if i["state"] == "opened" else 1] += 1

    scoped = defaultdict(Counter)
    for i in open_now:
        for l in i["labels"]:
            if "::" in l:
                k, v = l.split("::", 1)
                if k.lower() not in ("points", "sp", "estimate"):
                    scoped[k][v] += 1

    open_by_type = Counter(i["type"] for i in open_now)
    open_bugs = [i for i in open_now if i["type"] == "bug"]
    p85_bug = pct(by_type.get("bug", []), 85)

    out.update({
        "total": len(items),
        "created_in_window": len(created_in),
        "closed_in_window": len(closed_in),
        "open_now": len(open_now),
        "net_flow": len(created_in) - len(closed_in),
        "closed_date_proxy_used": sum(1 for i in closed_in if i["closed_is_proxy"]),
        "lead_time": dist(lt),
        "lead_time_by_type": {k: dist(v) for k, v in sorted(by_type.items())},
        "open_age": dist([a for a, _ in ages]),
        "open_older_than_stale": sum(1 for a, _ in ages if a > stale_days),
        "oldest_open": [{"iid": i["iid"], "title": i["title"], "age_days": round(a, 1), "url": i["url"]}
                        for a, i in reversed(ages[-10:])],
        "open_by_type": dict(open_by_type),
        "open_bugs": [{"iid": i["iid"], "title": i["title"],
                       "age_days": round((now - i["created"]).total_seconds() / DAY, 1),
                       "over_bug_p85": p85_bug is not None and (now - i["created"]).total_seconds() / DAY > p85_bug,
                       "url": i["url"]} for i in open_bugs if i["created"]],
        "weekly": [{"week": str(w), "created": c, "closed": d} for w, (c, d) in sorted(weekly.items())],
        "by_assignee": {a: {"open": o, "closed": c} for a, (o, c) in sorted(assignee.items(), key=lambda x: -sum(x[1]))},
        "open_by_scoped_label": {k: dict(v.most_common()) for k, v in sorted(scoped.items())},
    })
    return out


def mr_kpis(mrs, since, until, now):
    out = {"available": mrs is not None}
    if mrs is None:
        return out
    mrs = [norm_mr(m) for m in mrs]
    merged = [m for m in mrs if m["merged"] and in_window(m["merged"], since, until)]
    opened = [m for m in mrs if m["state"] == "opened"]
    lead = [(m["merged"] - m["created"]).total_seconds() / DAY for m in merged if m["created"]]
    review = [(m["first_review"] - m["created"]).total_seconds() / DAY
              for m in mrs if m["first_review"] and m["created"] and in_window(m["created"], since, until)]
    sizes = [(m["additions"] or 0) + (m["deletions"] or 0) for m in merged
             if m["additions"] is not None or m["deletions"] is not None]
    files = [m["files"] for m in merged if isinstance(m["files"], (int, float))]
    weekly = Counter(week_start(m["merged"]) for m in merged)
    out.update({
        "total": len(mrs),
        "merged_in_window": len(merged),
        "open_now": len(opened),
        "closed_unmerged_in_window": sum(1 for m in mrs if m["state"] == "closed" and not m["merged"]
                                         and in_window(m["closed"], since, until)),
        "lead_time": dist(lead, fmt_hours),
        "time_to_first_review": dist(review, fmt_hours),
        "size_lines_median": median(sizes),
        "size_files_median": median(files),
        "open_age": dist([(now - m["created"]).total_seconds() / DAY for m in opened if m["created"]]),
        "weekly_merged": [{"week": str(w), "merged": n} for w, n in sorted(weekly.items())],
        "by_author": dict(Counter(m["author"] or "?" for m in merged).most_common()),
    })
    return out


def pipeline_kpis(pipes, since, until, default_branch):
    out = {"available": pipes is not None}
    if pipes is None:
        return out
    pipes = [norm_pipeline(p) for p in pipes]
    pipes = [p for p in pipes if in_window(p["created"], since, until)]
    scope = [p for p in pipes if not default_branch or p["ref"] == default_branch] or pipes
    ok = sum(1 for p in scope if p["status"] == "success")
    bad = sum(1 for p in scope if p["status"] == "failed")
    dur = []
    for p in scope:
        if p["status"] not in ("success", "failed"):
            continue
        if isinstance(p["duration"], (int, float)):
            dur.append(p["duration"] / DAY)
        elif p["created"] and p["updated"]:
            dur.append((p["updated"] - p["created"]).total_seconds() / DAY)
    # time to restore: first failure -> next success on the same ref
    restore = []
    by_ref = defaultdict(list)
    for p in scope:
        if p["created"]:
            by_ref[p["ref"]].append(p)
    for runs in by_ref.values():
        runs.sort(key=lambda p: p["created"])
        broken_at = None
        for p in runs:
            if p["status"] == "failed" and broken_at is None:
                broken_at = p["created"]
            elif p["status"] == "success" and broken_at is not None:
                restore.append(((p["updated"] or p["created"]) - broken_at).total_seconds() / DAY)
                broken_at = None
    out.update({
        "ref_scope": default_branch if default_branch and any(p["ref"] == default_branch for p in pipes) else "all refs",
        "total": len(scope),
        "success": ok,
        "failed": bad,
        "success_rate": None if ok + bad == 0 else round(100.0 * ok / (ok + bad), 1),
        "duration": dist(dur, fmt_hours),
        "time_to_restore": dist(restore, fmt_hours),
        "by_status": dict(Counter(p["status"] for p in scope)),
    })
    return out


def commit_kpis(commits, since, until):
    out = {"available": commits is not None}
    if commits is None:
        return out
    cs = [c for c in (norm_commit(c) for c in commits) if in_window(c["date"], since, until)]
    weekly = Counter(week_start(c["date"]) for c in cs)
    out.update({
        "total": len(cs),
        "authors": dict(Counter(c["author"] or "?" for c in cs).most_common()),
        "weekly": [{"week": str(w), "commits": n} for w, n in sorted(weekly.items())],
    })
    return out


# ---------------------------------------------------------------- report
def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def dist_row(label, d):
    return [label, d["n"], d["median"], d["p85"], d["mean"], d["max"]]


DIST_H = ["", "n", "median", "p85", "mean", "max"]


def render_md(r):
    out = [f"# GitLab KPIs\n\nWindow: {r['window']} · today: {r['today']}\n"]
    w = r["work_items"]
    out.append("## Work items")
    if not w["available"]:
        out.append("n/a — work_items.json not provided.")
    else:
        out.append(table(["total", "created (window)", "closed (window)", "open now", "net flow", f"open > {r['stale_days']:g} d"],
                         [[w["total"], w["created_in_window"], w["closed_in_window"], w["open_now"],
                           f"{w['net_flow']:+d}", w["open_older_than_stale"]]]))
        if w["closed_date_proxy_used"]:
            out.append(f"\n> {w['closed_date_proxy_used']} closed item(s) had no closed date; updated date used as a proxy.")
        out.append("\n### Lead time (created → closed)")
        rows = [dist_row("all", w["lead_time"])] + [dist_row(k, v) for k, v in w["lead_time_by_type"].items()]
        out.append(table(DIST_H, rows))
        out.append("\n### Open item age")
        out.append(table(DIST_H, [dist_row("open", w["open_age"])]))
        if w["oldest_open"]:
            out.append("\nOldest open items:\n")
            out.append(table(["#", "title", "age"], [[i["iid"], i["title"], fmt_days(i["age_days"])] for i in w["oldest_open"]]))
        if w["open_bugs"]:
            out.append("\n### Open bugs")
            out.append(table(["#", "title", "age", "> bug p85?"],
                             [[b["iid"], b["title"], fmt_days(b["age_days"]), "yes" if b["over_bug_p85"] else "no"] for b in w["open_bugs"]]))
        if w["weekly"]:
            out.append("\n### Weekly flow (week starting Monday)")
            out.append(table(["week", "created", "closed"], [[x["week"], x["created"], x["closed"]] for x in w["weekly"]]))
        if w["by_assignee"]:
            out.append("\n### By assignee")
            out.append(table(["assignee", "open", "closed"], [[a, v["open"], v["closed"]] for a, v in w["by_assignee"].items()]))
        if w["open_by_scoped_label"]:
            out.append("\n### Open items by scoped label")
            for k, v in w["open_by_scoped_label"].items():
                out.append(f"- **{k}**: " + ", ".join(f"{name} {n}" for name, n in v.items()))
    m = r["merge_requests"]
    out.append("\n## Merge requests")
    if not m["available"]:
        out.append("n/a — merge_requests.json not provided.")
    elif m["total"] == 0:
        out.append("No merge requests found — MR lead time, review time, size and throughput cannot be computed.")
    else:
        out.append(table(["total", "merged (window)", "open now", "closed unmerged", "median size (lines)", "median files"],
                         [[m["total"], m["merged_in_window"], m["open_now"], m["closed_unmerged_in_window"],
                           m["size_lines_median"] if m["size_lines_median"] is not None else "n/a",
                           m["size_files_median"] if m["size_files_median"] is not None else "n/a"]]))
        out.append("")
        out.append(table(DIST_H, [dist_row("lead time (created → merged)", m["lead_time"]),
                                  dist_row("time to first review", m["time_to_first_review"]),
                                  dist_row("open MR age", m["open_age"])]))
        if m["weekly_merged"]:
            out.append("\n" + table(["week", "merged"], [[x["week"], x["merged"]] for x in m["weekly_merged"]]))
    p = r["pipelines"]
    out.append("\n## Pipelines")
    if not p["available"]:
        out.append("n/a — pipelines.json not provided.")
    elif p["total"] == 0:
        out.append("No pipelines found — success rate, duration and time to restore cannot be computed.")
    else:
        out.append(table(["scope", "runs", "success", "failed", "success rate"],
                         [[p["ref_scope"], p["total"], p["success"], p["failed"],
                           "n/a" if p["success_rate"] is None else f"{p['success_rate']}%"]]))
        out.append("")
        out.append(table(DIST_H, [dist_row("duration", p["duration"]), dist_row("time to restore", p["time_to_restore"])]))
    c = r["commits"]
    out.append("\n## Commits")
    if not c["available"]:
        out.append("n/a — commits.json not provided.")
    else:
        out.append(f"{c['total']} commit(s) in window by {len(c['authors'])} author(s).")
        if c["weekly"]:
            out.append("\n" + table(["week", "commits"], [[x["week"], x["commits"]] for x in c["weekly"]]))
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="directory with work_items.json, merge_requests.json, ...")
    ap.add_argument("--since", help="window start (inclusive), YYYY-MM-DD")
    ap.add_argument("--until", help="window end (exclusive), YYYY-MM-DD")
    ap.add_argument("--today", help="reference date for ages (default: now)")
    ap.add_argument("--stale-days", type=float, default=30)
    ap.add_argument("--default-branch", help="restrict pipeline KPIs to this ref")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    a = ap.parse_args(argv)

    since, until = parse_dt(a.since), parse_dt(a.until)
    now = parse_dt(a.today) if a.today else datetime.now(timezone.utc)
    if a.today and not a.until:
        now = now + timedelta(days=1)  # end of the given day
    j = lambda name: load(os.path.join(a.data, name))
    report = {
        "window": f"{a.since or 'beginning'} → {a.until or 'now'}",
        "today": (a.today or date.today().isoformat()),
        "stale_days": a.stale_days,
        "work_items": work_item_kpis(j("work_items.json"), since, until, now, a.stale_days),
        "merge_requests": mr_kpis(j("merge_requests.json"), since, until, now),
        "pipelines": pipeline_kpis(j("pipelines.json"), since, until, a.default_branch),
        "commits": commit_kpis(j("commits.json"), since, until),
    }
    if a.format == "json":
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
    else:
        sys.stdout.write(render_md(report))


if __name__ == "__main__":
    main()
