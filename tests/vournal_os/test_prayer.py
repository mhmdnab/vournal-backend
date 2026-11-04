"""Prayer anchors before the prayer section exists: every account is "no city set"."""

import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from vournal_os.support import OsTestCase


class Anchors(OsTestCase):
    def test_read_anchor(self):
        from app.os.common.prayer import read_anchor

        self.assertEqual(
            read_anchor({"start": {"prayer": "maghrib", "offset": 10}, "extra": 1}),
            {"start": {"prayer": "maghrib", "offset": 10}, "end": None},
        )
        self.assertEqual(
            read_anchor({"start": {"prayer": "fajr", "offset": 0}, "end": {"prayer": "sunrise", "offset": -5}})["end"],
            {"prayer": "sunrise", "offset": -5},
        )
        for raw in (None, {}, {"start": {"prayer": "noon", "offset": 0}}, {"start": {"prayer": "isha"}}, "x"):
            self.assertIsNone(read_anchor(raw))

    async def test_no_city_set(self):
        from app.os.common.prayer import SETTINGS_MISSING, resolve_or_fail, resolver

        user = await self.make_user()
        resolve = await resolver(user.id)
        self.assertIsNone(resolve("2026-11-03", {"start": {"prayer": "maghrib", "offset": 10}}))
        with self.assertRaises(HTTPException) as caught:
            await resolve_or_fail(user.id, "2026-11-03", {"start": {"prayer": "maghrib", "offset": 10}})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail["message"], SETTINGS_MISSING)

    async def test_saved_city_resolves_and_clearing_stops_it(self):
        from app.os.common.prayer import clear_settings, resolver, save_settings, settings
        from app.os.profile import ensure_os_profile

        user = await self.make_user()
        await ensure_os_profile(user)
        await save_settings(user.id, {"cityId": "beirut", "lat": 33.8938, "lng": 35.5018, "method": "LebanonAICP",
                                      "madhab": "shafi", "adjustments": {"isha": 2, "fajr": 99}})
        self.assertEqual((await settings(user.id))["adjustments"]["isha"], 2)
        start, end = (await resolver(user.id))("2026-11-03", {"start": {"prayer": "isha", "offset": 0}})
        self.assertEqual((start.isoformat(), end), ("2026-11-03T16:11:00+00:00", None))  # 18:09 + 2 minutes, Beirut
        await clear_settings(user.id)
        self.assertIsNone(await settings(user.id))
        self.assertIsNone((await resolver(user.id))("2026-11-03", {"start": {"prayer": "isha", "offset": 0}}))

    def test_anchor_validation(self):
        from pydantic import ValidationError

        from app.os.common.prayer import TimeAnchor

        TimeAnchor.model_validate({"start": {"prayer": "asr", "offset": -240}})
        for bad in ({"start": {"prayer": "asr", "offset": 241}}, {"start": {"prayer": "noon", "offset": 0}}, {"start": {"prayer": "asr", "offset": "5"}}):
            with self.assertRaises(ValidationError):
                TimeAnchor.model_validate(bad)


GOLDEN = Path(__file__).parent / "golden" / "prayer.json"  # tests/vournal_os/golden/prayer_golden.mjs


def _ms(value):
    return None if value is None else int(value.timestamp() * 1000)


def _golden_ms(value):
    return None if value is None else int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class PrayerMaths(unittest.TestCase):
    """The adhan port and prayer.util against NestJS's own answers."""

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text())

    def test_times_match_nestjs_to_the_millisecond(self):
        from app.os.common.prayer import prayer_times

        wrong = []
        for case in self.golden["times"]:
            settings = {k: case[k] for k in ("lat", "lng", "method", "madhab")}
            if "adjustments" in case:
                settings["adjustments"] = case["adjustments"]
            got = prayer_times(settings, case["date"])
            for prayer, expected in case["times"].items():
                if _ms(got[prayer]) != _golden_ms(expected):
                    wrong.append((case["place"], case["date"], case["method"], case["madhab"], prayer, got[prayer], expected))
        self.assertEqual(wrong[:5], [], f"{len(wrong)} of {len(self.golden['times']) * 6} times differ")

    def test_anchors_match_nestjs(self):
        from app.os.common.prayer import resolve_anchor

        settings = {"cityId": "beirut", "lat": 33.8938, "lng": 35.5018, "method": "LebanonAICP", "madhab": "shafi"}
        for case in self.golden["anchors"]:
            start, end = resolve_anchor(settings, case["date"], case["anchor"])
            self.assertEqual((_ms(start), _ms(end)), (_golden_ms(case["start"]), _golden_ms(case["end"])), case)

    def test_stored_values_read_as_nestjs_reads_them(self):
        from app.os.common.prayer import read_anchor, read_prayer_settings

        for case in self.golden["reads"]:
            self.assertEqual(read_anchor(case["raw"]), case["read"], case["raw"])
        for case in self.golden["settingsReads"]:
            self.assertEqual(read_prayer_settings(case["raw"]), case["read"], case["raw"])
        self.assertIsNone(read_anchor({"start": {"prayer": "asr", "offset": True}}))  # a bool is not a number in JS

    def test_adjustments_read_defensively(self):
        from app.os.common.prayer import NO_ADJUSTMENTS, read_adjustments

        self.assertEqual(read_adjustments({"fajr": 5, "isha": 99, "asr": 2.5, "dhuhr": "3"}), {**NO_ADJUSTMENTS, "fajr": 5})
        self.assertEqual(read_adjustments(None), NO_ADJUSTMENTS)

    def test_lebanon_calendar_every_day_and_the_clock_change(self):
        from app.os.common.prayer import PRAYERS, prayer_times

        official = {"lat": 33.8938, "lng": 35.5018, "method": "LebanonAICP", "madhab": "shafi"}
        beirut = ZoneInfo("Asia/Beirut")

        def row(day):
            t = prayer_times(official, day)
            return " ".join(t[p].astimezone(beirut).strftime("%H:%M") for p in PRAYERS)

        table = json.loads((Path(__file__).parents[2] / "app/os/data/aicp_beirut.json").read_text())
        for mmdd, line in table["days"].items():
            self.assertEqual(row(f"2026-{mmdd}"), line, mmdd)
        self.assertEqual(row("2026-03-28"), "04:02 05:25 11:48 15:20 18:02 19:19")
        self.assertEqual(row("2026-03-29"), "05:00 06:23 12:48 16:21 19:03 20:20")


if __name__ == "__main__":
    unittest.main()
