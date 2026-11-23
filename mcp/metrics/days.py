"""The timezone that decides the days of a project.

Plane: the project's timezone. GitLab: the instance's, read from the offsets of the times it writes.
--timezone replaces both.
"""
import argparse
import bisect
import datetime as dt
import re
import sys

from .dates import UTC, day_of, parse_dt, today_in

MAX_OFFSET_HOURS = 14
NO_OFFSET = dt.timedelta(0)


def parse_timezone(text):
    """An IANA name (America/Sao_Paulo) or an offset (+HH:MM, -HH:MM)."""
    offset = re.fullmatch(r"([+-])(\d\d):(\d\d)", text)
    if offset:
        return fixed_offset(*offset.groups())
    if text.upper() in ("UTC", "Z"):
        return UTC
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+, and the system needs a timezone database
        return ZoneInfo(text)
    except (ImportError, KeyError, ValueError, OSError):
        raise ValueError(f"unknown timezone {text!r}: use +HH:MM or -HH:MM") from None


def fixed_offset(sign, hours, minutes):
    if int(hours) > MAX_OFFSET_HOURS or int(minutes) >= 60:
        raise ValueError(f"unknown timezone {sign + hours + ':' + minutes!r}: use +HH:MM or -HH:MM")
    delta = dt.timedelta(hours=int(hours), minutes=int(minutes))
    return dt.timezone(-delta if sign == "-" else delta)


def timezone_arg(text):
    try:
        return parse_timezone(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


class InstanceZone(dt.tzinfo):
    """The timezone of a GitLab instance. The API has no field for it, but each time it writes has the offset
    in force at that moment, so a moment gets the offset of the nearest time seen. This follows daylight saving.
    """

    def __init__(self):
        self.moments, self.offsets = [], []  # sorted by moment

    def learn(self, text):
        moment = parse_dt(text)
        position = bisect.bisect_left(self.moments, moment)
        self.moments.insert(position, moment)
        self.offsets.insert(position, moment.utcoffset())

    def offset_at(self, moment):
        if not self.moments:
            return NO_OFFSET
        after = bisect.bisect_left(self.moments, moment)
        candidates = range(max(after - 1, 0), min(after + 1, len(self.moments)))
        nearest = min(candidates, key=lambda index: abs(self.moments[index] - moment))
        return self.offsets[nearest]

    def utcoffset(self, local_time):
        if local_time is None:
            return self.offsets[-1] if self.offsets else NO_OFFSET
        wall_clock = local_time.replace(tzinfo=UTC)
        first_guess = self.offset_at(wall_clock)
        return self.offset_at(wall_clock - first_guess)

    def fromutc(self, utc_time):
        return utc_time + self.offset_at(utc_time.replace(tzinfo=UTC))

    def dst(self, local_time):
        return None

    def tzname(self, local_time):
        return str(dt.timezone(self.utcoffset(local_time)))

    def __str__(self):
        used = sorted(set(self.offsets)) or [NO_OFFSET]
        return "/".join(str(dt.timezone(offset)) for offset in used)


class ProjectDays:
    """What the GitLab and Plane clients know about days."""

    tz = UTC
    tz_note = None
    chosen_today = None

    def set_days(self, zone=None, today=None):
        """What the user chose with --timezone and --today."""
        if zone:
            self.tz, self.tz_note = zone, None
        self.chosen_today = today

    def use_timezone(self, name):
        try:
            self.tz = parse_timezone(name)
        except ValueError:
            self.tz_note = (f"The project's timezone {name} is not available on this machine: days are "
                            "in UTC. Pass --timezone +HH:MM or -HH:MM.")

    def learn_times(self, *values):
        """Times the GitLab instance wrote, to learn its offsets."""
        if not isinstance(self.tz, InstanceZone):
            return  # the user chose the timezone
        for value in values:
            if value:
                self.tz.learn(value)

    def today(self):
        return self.chosen_today or today_in(self.tz)

    def local_day(self, value):
        """The day of a time, as YYYY-MM-DD. A value that is only a date keeps it."""
        if not value:
            return None
        if "T" not in value:
            return value[:10]
        return day_of(parse_dt(value), self.tz).isoformat()

    def warn_about_days(self):
        if self.tz_note:
            print(f"note: {self.tz_note}", file=sys.stderr)
