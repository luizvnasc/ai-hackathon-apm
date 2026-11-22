"""shared/days.py and the day limits of shared/dates.py."""
import datetime as dt
import unittest
from unittest import mock

from tests.unit._load import load_shared

dates = load_shared("dates")
days = load_shared("days")
UTC = dt.timezone.utc
SAO_PAULO = dt.timezone(dt.timedelta(hours=-3))


class LimitsTest(unittest.TestCase):
    def test_start_of_day(self):
        expected = dt.datetime(2026, 3, 15, 3, tzinfo=UTC)
        self.assertEqual(dates.start_of_day(dt.date(2026, 3, 15), SAO_PAULO), expected)

    def test_end_of_day_is_the_last_instant(self):
        next_day = dt.datetime(2026, 3, 16, 3, tzinfo=UTC)
        self.assertEqual(dates.end_of_day(dt.date(2026, 3, 15), SAO_PAULO), next_day - dt.timedelta(microseconds=1))

    def test_limits_are_in_utc(self):
        self.assertIs(dates.end_of_day(dt.date(2026, 3, 15), SAO_PAULO).tzinfo, UTC)

    def test_age_reference_is_the_last_minute(self):
        expected = dt.datetime(2026, 3, 16, 2, 59, tzinfo=UTC)
        self.assertEqual(dates.age_reference(dt.date(2026, 3, 15), SAO_PAULO), expected)

    def test_today_depends_on_the_zone(self):
        fake = mock.Mock(wraps=dt.datetime)
        fake.now.return_value = dt.datetime(2026, 3, 16, 1, tzinfo=UTC)
        with mock.patch.object(dates.dt, "datetime", fake):
            self.assertEqual(dates.today_in(SAO_PAULO), dt.date(2026, 3, 15))
            self.assertEqual(dates.today_in(UTC), dt.date(2026, 3, 16))

    def test_short_fractions_of_a_second(self):
        # fromisoformat refuses them before Python 3.11
        self.assertEqual(dates.parse_dt("2026-03-15T10:00:00.5Z").microsecond, 500000)
        self.assertEqual(dates.parse_dt("2026-03-15T10:00:00.1234567Z").microsecond, 123456)


class ParseTimezoneTest(unittest.TestCase):
    def test_offsets(self):
        for text, minutes in (("-03:00", -180), ("+05:30", 330), ("+14:00", 840), ("+00:00", 0)):
            self.assertEqual(days.parse_timezone(text).utcoffset(None), dt.timedelta(minutes=minutes))

    def test_utc(self):
        for text in ("UTC", "utc", "Z"):
            self.assertIs(days.parse_timezone(text), UTC)

    def test_refused(self):
        for text in ("+15:00", "-03:60", "3", "No/Where", ""):
            with self.assertRaises(ValueError):
                days.parse_timezone(text)

    def test_iana_name(self):
        try:
            import zoneinfo
            zoneinfo.ZoneInfo("America/Sao_Paulo")
        except (ImportError, KeyError):
            self.skipTest("no timezone database on this machine")
        zone = days.parse_timezone("America/Sao_Paulo")
        self.assertEqual(dates.end_of_day(dt.date(2026, 3, 15), zone), dates.end_of_day(dt.date(2026, 3, 15), SAO_PAULO))


class InstanceZoneTest(unittest.TestCase):
    def zone(self, *times):
        zone = days.InstanceZone()
        for text in times:
            zone.learn(text)
        return zone

    def test_utc_until_it_sees_a_time(self):
        zone = self.zone()
        self.assertEqual(str(zone), "UTC")
        self.assertEqual(dates.end_of_day(dt.date(2026, 3, 15), zone), dates.end_of_day(dt.date(2026, 3, 15), UTC))

    def test_one_offset(self):
        zone = self.zone("2026-03-10T10:00:00-03:00")
        self.assertEqual(str(zone), "UTC-03:00")
        self.assertEqual(dates.day_of(dates.parse_dt("2026-03-16T02:30:00Z"), zone), dt.date(2026, 3, 15))
        self.assertEqual(dates.end_of_day(dt.date(2026, 3, 15), zone), dates.end_of_day(dt.date(2026, 3, 15), SAO_PAULO))

    def test_follows_daylight_saving(self):
        zone = self.zone("2026-07-10T12:00:00+01:00", "2026-01-10T12:00:00+00:00")
        winter, summer = dt.date(2026, 1, 20), dt.date(2026, 7, 20)
        self.assertEqual(dates.start_of_day(winter, zone), dt.datetime(2026, 1, 20, 0, tzinfo=UTC))
        self.assertEqual(dates.start_of_day(summer, zone), dt.datetime(2026, 7, 19, 23, tzinfo=UTC))
        self.assertEqual(dates.day_of(dates.parse_dt("2026-07-19T23:30:00Z"), zone), summer)
        self.assertEqual(str(zone), "UTC/UTC+01:00")

    def test_today_has_the_offset_of_the_latest_time(self):
        zone = self.zone("2026-07-10T12:00:00+01:00", "2026-01-10T12:00:00+00:00")
        self.assertEqual(zone.utcoffset(None), dt.timedelta(hours=1))


class ProjectDaysTest(unittest.TestCase):
    def project(self):
        project = days.ProjectDays()
        project.tz = days.InstanceZone()
        return project

    def test_the_chosen_timezone_is_not_replaced_by_the_learned_one(self):
        project = self.project()
        project.set_days(SAO_PAULO)
        project.learn_times("2026-03-10T10:00:00+09:00")
        self.assertIs(project.tz, SAO_PAULO)

    def test_the_chosen_timezone_clears_the_note(self):
        project = self.project()
        project.use_timezone("No/Where")
        self.assertIn("--timezone", project.tz_note)
        project.set_days(SAO_PAULO)
        self.assertIsNone(project.tz_note)

    def test_missing_times_are_skipped(self):
        project = self.project()
        project.learn_times(None, "")
        self.assertEqual(str(project.tz), "UTC")

    def test_chosen_today_wins(self):
        project = self.project()
        project.set_days(today=dt.date(2026, 1, 2))
        self.assertEqual(project.today(), dt.date(2026, 1, 2))

    def test_local_day(self):
        project = self.project()
        project.set_days(SAO_PAULO)
        self.assertEqual(project.local_day("2026-03-16T02:59:00Z"), "2026-03-15")
        self.assertEqual(project.local_day("2026-03-16"), "2026-03-16")
        self.assertIsNone(project.local_day(None))


if __name__ == "__main__":
    unittest.main()
