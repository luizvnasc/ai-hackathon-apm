"""Builders shared by the scope-stability tests. The period S runs from 2026-03-02 to 2026-03-13."""
import argparse
import datetime as dt

from tests.unit._load import load

ss = load("scope-stability", "stability.py")
TODAY = dt.date(2026, 4, 1)
NEXT = {"id": "N", "name": "Next", "start": "2026-03-16", "end": "2026-03-27", "closed": False}
OLD = {"id": "O", "name": "Old", "start": "2026-02-16", "end": "2026-02-27", "closed": True}


def period(**kw):
    return {"id": "S", "name": "Sprint", "start": "2026-03-02", "end": "2026-03-13", "closed": False, **kw}


def at(value):
    """'03-05' or '03-05T22:00:00-03:00' in 2026."""
    return ss.parse_dt("2026-" + (value if "T" in value else value + "T12:00:00Z"))


def issue(key="I-1", points=5.0, start="S", moves=(), estimates=(), first_points=None, created="02-20", **kw):
    """An issue that was in `start` when created. moves: (when, destination). estimates: (when, value)."""
    for field in ("closed", "cancelled"):
        if kw.get(field):
            kw[field] = at(kw[field])
    moves = [(at(t), to) for t, to in moves]
    estimates = [(at(t), v) for t, v in estimates]
    return ss.new_issue(key=key, title="Task " + key, url="https://x/" + key, created=at(created),
                        period=moves[-1][1] if moves else start, initial=start, moves=moves,
                        points=estimates[-1][1] if estimates else points,
                        points_initial=first_points if estimates else points, estimates=estimates, **kw)


def args(**kw):
    return argparse.Namespace(**{"last": 6, "period": None, "unit": "auto", "grace_days": 0, **kw})


def analyze(issues, periods=None, today=TODAY, tz=ss.UTC, **kw):
    periods = ss.mark_status([dict(p) for p in (periods or [OLD, period(), NEXT])], today)
    return ss.analyze("Demo", "milestone", periods, issues, args(**kw), today, tz)


def row(issues, **kw):
    return next(r for r in analyze(issues, **kw)["rows"] if r["id"] == "S")
