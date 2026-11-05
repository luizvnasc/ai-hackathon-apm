#!/usr/bin/env python3
"""Milestone delivery KPIs and forecast (deterministic + Monte Carlo).

Stdlib only (Python 3.8+), so it runs the same under Claude Code, GitHub Copilot
and OpenCode. The agent collects the milestone's work items with the GitLab MCP
server, saves them as a JSON list and runs:

    python3 forecast.py --data work_items.json [--milestone TITLE]
                        [--start YYYY-MM-DD] [--due YYYY-MM-DD] [--today YYYY-MM-DD]
                        [--unit auto|points|count] [--sims 10000] [--seed 42]
                        [--format md|json]

Each work item may be the compact shape
    {"iid", "title", "state", "created", "closed", "labels": [...],
     "assignees": [...], "milestone", "milestone_start", "milestone_due",
     "weight", "url"}
or the raw object returned by get_work_item (createdAt, closedAt, widgets, ...).
Milestone start/due dates are read from the items when present; --start/--due
override them.
"""
import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

DAY = 86400.0
POINT_LABEL = re.compile(r"^(points|sp|story[-_ ]?points|estimate)::\s*(\d+(?:\.\d+)?)$", re.I)


def parse_dt(value):
    if not value:
        return None
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
        if obj.get(k) not in (None, ""):
            return obj[k]
    return None


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[max(0, math.ceil(p / 100.0 * len(v)) - 1)]


def median(values):
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def week_start(d):
    d = d.date() if isinstance(d, datetime) else d
    return d - timedelta(days=d.weekday())


def norm(raw):
    labels, assignees = raw.get("labels"), raw.get("assignees")
    ms = raw.get("milestone")
    ms_start, ms_due = raw.get("milestone_start"), raw.get("milestone_due")
    weight = raw.get("weight")
    for w in raw.get("widgets") or []:
        t = w.get("type")
        if t == "LABELS" and labels is None:
            labels = [n["title"] for n in (w.get("labels") or {}).get("nodes", [])]
        elif t == "ASSIGNEES" and assignees is None:
            assignees = [n.get("username") for n in (w.get("assignees") or {}).get("nodes", [])]
        elif t == "MILESTONE" and w.get("milestone"):
            ms = ms or w["milestone"]
        elif t == "WEIGHT" and weight is None:
            weight = w.get("weight")
    if isinstance(ms, dict):
        ms_start = ms_start or ms.get("startDate") or ms.get("start_date")
        ms_due = ms_due or ms.get("dueDate") or ms.get("due_date")
        ms = ms.get("title")
    labels = [l["title"] if isinstance(l, dict) else l for l in (labels or [])]
    points = None
    if isinstance(weight, (int, float)):
        points = float(weight)
    else:
        for l in labels:
            m = POINT_LABEL.match(l.strip())
            if m:
                points = float(m.group(2))
                break
    closed = parse_dt(pick(raw, "closed", "closedAt", "closed_at"))
    state = str(raw.get("state") or "").lower()
    workflow = next((l.split("::", 1)[1] for l in labels if l.lower().startswith(("workflow::", "status::"))), None)
    return {
        "iid": raw.get("iid"),
        "title": raw.get("title") or "",
        "done": state == "closed" or closed is not None,
        "created": parse_dt(pick(raw, "created", "createdAt", "created_at")),
        "closed": closed or (parse_dt(pick(raw, "updated", "updatedAt")) if state == "closed" else None),
        "closed_is_proxy": closed is None and state == "closed",
        "labels": labels,
        "assignees": [a.get("username") if isinstance(a, dict) else a for a in (assignees or []) if a],
        "milestone": ms,
        "ms_start": parse_dt(ms_start),
        "ms_due": parse_dt(ms_due),
        "points": points,
        "workflow": workflow,
        "url": pick(raw, "url", "webUrl", "web_url"),
    }


def simulate(remaining, samples, start_week, sims, seed, cap_weeks=520):
    """Return a sorted list of finish weeks (week-start dates) from Monte Carlo runs."""
    rng = random.Random(seed)
    if not samples or max(samples) <= 0:
        return None
    finishes = []
    for _ in range(sims):
        left, weeks = remaining, 0
        while left > 1e-9 and weeks < cap_weeks:
            left -= rng.choice(samples)
            weeks += 1
        finishes.append(weeks)
    finishes.sort()
    return finishes


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def num(x):
    return "n/a" if x is None else (f"{x:.0f}" if float(x).is_integer() else f"{x:.1f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="JSON file with the milestone's work items")
    ap.add_argument("--milestone", help="keep only items in this milestone (title)")
    ap.add_argument("--start")
    ap.add_argument("--due")
    ap.add_argument("--today")
    ap.add_argument("--unit", choices=["auto", "points", "count"], default="auto")
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--format", choices=["md", "json"], default="md")
    a = ap.parse_args(argv)

    with open(a.data, encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict):
        raw = raw.get("work_items") or raw.get("nodes") or [raw]
    items = [norm(r) for r in raw]
    if a.milestone:
        items = [i for i in items if i["milestone"] in (None, a.milestone)]
    if not items:
        sys.exit("no work items to analyze")

    today = parse_dt(a.today) if a.today else datetime.now(timezone.utc)
    now = today + timedelta(days=1) if a.today else today
    start = parse_dt(a.start) or next((i["ms_start"] for i in items if i["ms_start"]), None)
    due = parse_dt(a.due) or next((i["ms_due"] for i in items if i["ms_due"]), None)
    caveats = []
    if start is None:
        start = min(i["created"] for i in items if i["created"])
        caveats.append("Milestone start date unknown; earliest item creation date used.")
    if due is None:
        caveats.append("Milestone due date unknown; no on-time verdict, projected finish only.")

    with_points = sum(1 for i in items if i["points"] is not None)
    unit = a.unit
    if unit == "auto":
        unit = "points" if with_points == len(items) else "count"
        if 0 < with_points < len(items):
            caveats.append(f"Only {with_points}/{len(items)} items have points; using issue count.")
    if unit == "points" and with_points < len(items):
        missing = len(items) - with_points
        avg = sum(i["points"] for i in items if i["points"] is not None) / max(1, with_points)
        caveats.append(f"{missing} item(s) without points were given the average ({avg:.1f}).")
        for i in items:
            if i["points"] is None:
                i["points"] = avg
    size = (lambda i: i["points"]) if unit == "points" else (lambda i: 1.0)
    proxies = sum(1 for i in items if i["closed_is_proxy"])
    if proxies:
        caveats.append(f"{proxies} closed item(s) without closedAt; updatedAt used as proxy.")

    total = sum(size(i) for i in items)
    done_items = [i for i in items if i["done"]]
    open_items = [i for i in items if not i["done"]]
    done = sum(size(i) for i in done_items)
    remaining = total - done
    added = [i for i in items if i["created"] and i["created"] > start + timedelta(days=1)]

    cycle = [(i["closed"] - i["created"]).total_seconds() / DAY for i in done_items if i["closed"] and i["created"]]
    p85_cycle = pct(cycle, 85)

    # weekly throughput from the milestone start week to the current week
    first_w, this_w = week_start(start), week_start(today)
    weeks = []
    w = first_w
    while w <= this_w:
        weeks.append(w)
        w += timedelta(days=7)
    thr = Counter()
    for i in done_items:
        if i["closed"]:
            thr[max(week_start(i["closed"]), first_w)] += size(i)
    complete = [w for w in weeks if w < this_w]
    samples = [thr[w] for w in complete]
    partial_used = False
    if len(samples) < 3:
        samples = [thr[w] for w in weeks]
        partial_used = True

    # burndown: remaining at the end of each week (scope known at that time)
    burndown = []
    for w in weeks:
        end = datetime.combine(w + timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)
        scope = sum(size(i) for i in items if not i["created"] or i["created"] < end)
        closed = sum(size(i) for i in done_items if i["closed"] and i["closed"] < end)
        burndown.append((w, thr[w], scope - closed))

    med_thr = median(samples) if samples else None
    det_weeks = None if not med_thr else remaining / med_thr
    det_finish = None if det_weeks is None else (today + timedelta(days=7 * det_weeks)).date()

    mc = None
    if remaining <= 0:
        verdict = "Done"
    elif len(samples) >= 3 and any(s > 0 for s in samples):
        fin = simulate(remaining, samples, this_w, a.sims, a.seed)
        # a simulated week k means finishing by the end of week this_w + k - 1 (current week counts)
        to_date = lambda k: this_w + timedelta(days=7 * k - 1)
        p50, p85, p95 = (to_date(pct(fin, p)) for p in (50, 85, 95))
        prob = None
        if due:
            prob = round(100.0 * sum(1 for k in fin if to_date(k) <= due.date()) / len(fin), 1)
        mc = {"p50": str(p50), "p85": str(p85), "p95": str(p95), "prob_by_due": prob,
              "samples": samples, "partial_week_included": partial_used}
        if not due:
            verdict = "No due date"
        elif p85 <= due.date():
            verdict = "On track"
        elif p50 <= due.date():
            verdict = "At risk"
        else:
            verdict = "Off track"
    else:
        verdict = "Low confidence (fewer than 3 weekly samples)" if due is None or det_finish is None else (
            "On track (low confidence)" if det_finish <= due.date() else "Off track (low confidence)")

    # scope that fits by the due date at 85% confidence
    cut = None
    if due and samples and remaining > 0:
        weeks_left = max(0, (week_start(due) - this_w).days // 7 + 1)
        rng = random.Random(a.seed)
        sums = sorted(sum(rng.choice(samples) for _ in range(weeks_left)) for _ in range(a.sims))
        fit85 = sums[int(0.15 * len(sums))]  # 85% of runs deliver at least this much
        cut = {"weeks_left": weeks_left, "deliverable_p85": fit85, "cut_needed": max(0.0, remaining - fit85)}

    aging = sorted(
        ({"iid": i["iid"], "title": i["title"], "age_days": round((now - i["created"]).total_seconds() / DAY, 1),
          "workflow": i["workflow"], "assignees": i["assignees"], "size": size(i), "url": i["url"]}
         for i in open_items if i["created"]), key=lambda x: -x["age_days"])
    for x in aging:
        x["over_p85_cycle"] = p85_cycle is not None and x["age_days"] > p85_cycle

    report = {
        "milestone": a.milestone or next((i["milestone"] for i in items if i["milestone"]), None),
        "start": start.date().isoformat(), "due": due.date().isoformat() if due else None,
        "today": today.date().isoformat(), "unit": unit,
        "scope": {"items": len(items), "total": total, "done": done, "remaining": remaining,
                  "pct_complete": round(100.0 * done / total, 1) if total else None,
                  "open_items": len(open_items), "unassigned_open": sum(1 for i in open_items if not i["assignees"])},
        "scope_change": {"added_after_start": len(added), "added_size": sum(size(i) for i in added),
                         "items": [{"iid": i["iid"], "title": i["title"]} for i in added]},
        "cycle_time": {"n": len(cycle), "median": median(cycle), "p85": p85_cycle},
        "throughput": {"weekly": [{"week": str(w), "done": t, "remaining": r} for w, t, r in burndown],
                       "mean": sum(samples) / len(samples) if samples else None, "median": med_thr},
        "forecast": {"deterministic_weeks": det_weeks, "deterministic_finish": str(det_finish) if det_finish else None,
                     "monte_carlo": mc, "verdict": verdict, "scope_cut": cut},
        "aging_wip": aging,
        "workflow": dict(Counter(i["workflow"] or "(none)" for i in open_items)),
        "caveats": caveats,
    }
    if a.format == "json":
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
        return

    s, f = report["scope"], report["forecast"]
    u = "pts" if unit == "points" else "items"
    out = [f"# Milestone forecast: {report['milestone'] or '(unnamed)'}\n",
           f"Start {report['start']} · due {report['due'] or 'unknown'} · today {report['today']} · unit: {unit}\n",
           "## KPIs",
           table(["metric", "value"], [
               ["scope", f"{num(s['total'])} {u}" + (f" ({s['items']} items)" if unit == "points" else "")],
               ["done", f"{num(s['done'])} {u} ({s['pct_complete']}%)"],
               ["remaining", f"{num(s['remaining'])} {u} ({s['open_items']} items, {s['unassigned_open']} unassigned)"],
               ["scope added after start", f"{report['scope_change']['added_after_start']} items, {num(report['scope_change']['added_size'])} {u}"],
               ["cycle time (created → closed)", f"median {num(report['cycle_time']['median'])} d, p85 {num(report['cycle_time']['p85'])} d (n={report['cycle_time']['n']})"],
               ["weekly throughput", f"mean {num(report['throughput']['mean'])}, median {num(report['throughput']['median'])} {u}/week"],
           ]),
           "\n## Weekly throughput and burndown",
           table(["week", f"done ({u})", f"remaining ({u})"],
                 [[x["week"], num(x["done"]), num(x["remaining"])] for x in report["throughput"]["weekly"]]),
           "\n## Forecast"]
    rows = [["deterministic finish", f"{f['deterministic_finish'] or 'n/a'} ({num(f['deterministic_weeks'])} weeks at median throughput)"]]
    if mc:
        rows += [["Monte Carlo P50 / P85 / P95", f"{mc['p50']} / {mc['p85']} / {mc['p95']}"],
                 ["probability of finishing by due", "n/a" if mc["prob_by_due"] is None else f"{mc['prob_by_due']}%"]]
    if f["scope_cut"]:
        c = f["scope_cut"]
        rows.append(["deliverable by due (85% confidence)",
                     f"{num(c['deliverable_p85'])} {u} in {c['weeks_left']} week(s); cut {num(c['cut_needed'])} {u} to reach 85%"])
    rows.append(["verdict", f"**{f['verdict']}**"])
    out.append(table(["", ""], rows))
    if aging:
        out.append("\n## Aging WIP (open items)")
        out.append(table(["#", "title", "age", "workflow", "assignee", u, "> p85 cycle?"],
                         [[x["iid"], x["title"], f"{x['age_days']} d", x["workflow"] or "-",
                           ", ".join(x["assignees"]) or "-", num(x["size"]), "yes" if x["over_p85_cycle"] else "no"]
                          for x in aging]))
    if report["scope_change"]["items"]:
        out.append("\nAdded after start: " + ", ".join(f"#{x['iid']} {x['title']}" for x in report["scope_change"]["items"]))
    if caveats or (mc and mc["partial_week_included"]):
        out.append("\n## Caveats")
        out += [f"- {c}" for c in caveats]
        if mc and mc["partial_week_included"]:
            out.append("- Fewer than 3 complete weeks; the current partial week was included in the samples.")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
