"""Which nudge moment is due (design 2026-11-18): 30-minute windows, quiet hours 22:00–08:00
(a morning the person set earlier is respected), and no midday nudge for free users."""

import unittest
from datetime import datetime

from app.nudges.schedule import allowed, due_moment, in_quiet_hours, moments_for


def at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 18, h, m)


class Schedule(unittest.TestCase):
    def test_morning_window(self):
        self.assertEqual(due_moment(at("08:00"), "08:00"), "morning")
        self.assertEqual(due_moment(at("08:29"), "08:00"), "morning")
        self.assertIsNone(due_moment(at("07:59"), "08:00"))
        self.assertIsNone(due_moment(at("08:30"), "08:00"))

    def test_custom_morning_0630_allowed_in_quiet_hours(self):
        self.assertEqual(due_moment(at("06:40"), "06:30"), "morning")
        self.assertTrue(in_quiet_hours(at("06:40")))
        self.assertTrue(allowed("morning", at("06:40")))

    def test_midday_and_evening_windows(self):
        self.assertEqual(due_moment(at("13:00"), "08:00"), "midday")
        self.assertEqual(due_moment(at("13:29"), "08:00"), "midday")
        self.assertIsNone(due_moment(at("13:30"), "08:00"))
        self.assertEqual(due_moment(at("20:15"), "08:00"), "evening")
        self.assertIsNone(due_moment(at("20:30"), "08:00"))

    def test_overlap_earlier_start_wins(self):
        self.assertEqual(due_moment(at("11:45"), "11:30"), "morning")
        self.assertEqual(due_moment(at("13:05"), "11:30"), "midday")

    def test_quiet_hours_bounds(self):
        self.assertFalse(in_quiet_hours(at("21:59")))
        self.assertTrue(in_quiet_hours(at("22:00")))
        self.assertTrue(in_quiet_hours(at("07:59")))
        self.assertFalse(in_quiet_hours(at("08:00")))
        self.assertFalse(allowed("evening", at("22:10")))
        self.assertTrue(allowed("evening", at("20:10")))

    def test_free_has_no_midday(self):
        self.assertEqual(moments_for(True), ("morning", "midday", "evening"))
        self.assertEqual(moments_for(False), ("morning", "evening"))
