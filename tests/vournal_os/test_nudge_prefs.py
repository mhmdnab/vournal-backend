"""Nudge preferences (design 2026-11-18): new keys in notificationPrefs, normalized like the rest.
The morning time is the app's own (05:00–11:30); the website's dailyDigestTime is untouched."""

import unittest

from app.os.settings import normalize_prefs


class NudgePrefs(unittest.TestCase):
    def test_defaults(self):
        p = normalize_prefs(None)
        self.assertEqual((p["nudges"], p["nudgePlan"], p["nudgeCheckIns"], p["nudgeSuggestions"], p["nudgeMorningTime"]),
                         (False, True, True, True, "08:00"))

    def test_morning_time_must_be_hhmm_in_range(self):
        self.assertEqual(normalize_prefs({"nudgeMorningTime": "06:30"})["nudgeMorningTime"], "06:30")
        self.assertEqual(normalize_prefs({"nudgeMorningTime": "05:00"})["nudgeMorningTime"], "05:00")
        self.assertEqual(normalize_prefs({"nudgeMorningTime": "11:30"})["nudgeMorningTime"], "11:30")
        for bad in ("4:00", "04:59", "11:31", "23:00", None, 7):
            self.assertEqual(normalize_prefs({"nudgeMorningTime": bad})["nudgeMorningTime"], "08:00", bad)

    def test_digest_time_untouched(self):
        self.assertIsNone(normalize_prefs({"dailyDigestTime": None})["dailyDigestTime"])

    def test_nudge_switches_survive(self):
        p = normalize_prefs({"nudges": True, "nudgeSuggestions": False, "bills": False})
        self.assertEqual((p["nudges"], p["nudgeSuggestions"], p["bills"]), (True, False, False))
