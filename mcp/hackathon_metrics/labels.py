"""What labels say: `scope::value`, points, and the words for cancelled, blocked and stale."""
import re

CANCELLED = {"canceled", "cancelled", "wontfix", "won't-fix", "duplicate"}
BLOCKED_VALUES = {"blocked", "on-hold", "on hold", "onhold", "impeded", "bloqueado", "bloqueada"}
STALE_VALUES = {"stale"}
POINT_SCOPES = ("points", "sp", "estimate")  # in order of preference


def label_value(label):
    """`workflow::Doing` -> `doing`. A label without scope is its own value."""
    return (label or "").rsplit("::", 1)[-1].strip().lower()


def scoped(labels, scope):
    """The values of the labels of one scope. The last :: is the separator."""
    values = []
    for label in labels:
        if "::" not in label:
            continue
        label_scope = label.rsplit("::", 1)[0]
        if label_scope.strip().lower() == scope:
            values.append(label_value(label))
    return values


def number(value):
    """The value as a float, or None when it is not a plain number (XL, 2h, -1)."""
    if value is None:
        return None
    text = str(value).strip()
    return float(text) if re.fullmatch(r"\d+(\.\d+)?", text) else None


def label_points(labels):
    """The first numeric value of points::, sp:: or estimate::."""
    for scope in POINT_SCOPES:
        for value in scoped(labels, scope):
            points = number(value)
            if points is not None:
                return points
    return None


def has_cancelled_label(labels):
    values = scoped(labels, "workflow") + scoped(labels, "status")
    return bool(set(values) & CANCELLED)
