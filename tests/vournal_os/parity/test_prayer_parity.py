"""Prayer times answer like NestJS did: the place and its times, saving and clearing it,
and what the times move once a city is set (anchored tasks, events, reminders and the
calendar). The account's timezone is Beirut on both sides."""

import unittest

from vournal_os.parity.harness import ParityCase

BEIRUT = {"cityId": "beirut", "lat": 33.8938, "lng": 35.5018, "method": "LebanonAICP", "madhab": "shafi"}


class PrayerParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def test_settings_and_times(self):
        self.assertParity(*await self.both("GET", "/prayer-times"))
        self.assertParity(*await self.both("GET", "/prayer-times?date=2026-03-28&days=3"))
        self.assertParity(*await self.both("PUT", "/prayer-times/settings", BEIRUT))
        for query in ("?date=2026-03-28&days=3", "?date=2026-10-24&days=2", "?date=2028-02-28&days=2", "?days=31",
                      "?date=2026-11-04T10:00:00Z", "?days=0", "?days=32", "?days=2.5", "?date=soon", "?bogus=1"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/prayer-times{query}"))
        tuned = {**BEIRUT, "method": "MuslimWorldLeague", "madhab": "hanafi",
                 "adjustments": {"fajr": 2, "sunrise": 0, "dhuhr": -1, "asr": 30, "maghrib": -30, "isha": 5}}
        self.assertParity(*await self.both("PUT", "/prayer-times/settings", tuned))
        # adjustments left out keep the current ones; a null city clears the city only
        self.assertParity(*await self.both("PUT", "/prayer-times/settings", {**BEIRUT, "cityId": None, "method": "Turkey"}))
        self.assertParity(*await self.both("PUT", "/prayer-times/settings", {"lat": 69.6492, "lng": 18.9553, "method": "MoonsightingCommittee", "madhab": "shafi"}))
        self.assertParity(*await self.both("GET", "/prayer-times?date=2026-06-21&days=2"))
        self.assertParity(*await self.both("GET", "/prayer-times?date=2026-12-21"))
        self.assertParity(*await self.both("DELETE", "/prayer-times/settings"))
        self.assertParity(*await self.both("GET", "/prayer-times"))
        self.assertParity(*await self.both("DELETE", "/prayer-times/settings"))

    async def test_settings_validation(self):
        for body in (
            {}, {**BEIRUT, "lat": 91}, {**BEIRUT, "lng": "35"}, {**BEIRUT, "method": "Jafari"}, {**BEIRUT, "madhab": "maliki"},
            {**BEIRUT, "cityId": "x" * 61}, {**BEIRUT, "cityId": 5}, {**BEIRUT, "bogus": 1},
            {**BEIRUT, "adjustments": {"fajr": 31, "sunrise": 0, "dhuhr": 0, "asr": 0, "maghrib": 0, "isha": 0}},
            {**BEIRUT, "adjustments": {"fajr": 1.5}}, {**BEIRUT, "adjustments": "none"}, {**BEIRUT, "adjustments": None},
            {**BEIRUT, "adjustments": {"fajr": 0, "sunrise": 0, "dhuhr": 0, "asr": 0, "maghrib": 0, "isha": 0, "sunset": 1}},
            {**BEIRUT, "adjustments": [1, 2]}, {**BEIRUT, "adjustments": [{"fajr": 99}, 3]}, {"lat": None, "lng": None, "method": None, "madhab": None},
        ):
            with self.subTest(body=body):
                nest, py = await self.both("PUT", "/prayer-times/settings", body)
                self.assertParity(nest, py)
                if nest.status_code == 400:
                    self.assertEqual(nest.json()["message"], py.json()["message"])  # word for word

    async def test_anchors_follow_the_city(self):
        self.assertParity(*await self.both("PUT", "/prayer-times/settings", BEIRUT))
        task = {"title": "Read", "dueAt": "2026-11-05", "timeAnchor": {"start": {"prayer": "maghrib", "offset": 15}}}
        nest, py = await self.both("POST", "/tasks", task)
        self.assertParity(nest, py)
        event = {"title": "Family dinner", "startAt": "2026-11-05T10:00:00.000Z", "endAt": "2026-11-05T11:00:00.000Z",
                 "timeAnchor": {"start": {"prayer": "isha", "offset": 0}, "end": {"prayer": "fajr", "offset": -30}}}
        self.assertParity(*await self.both("POST", "/events", event))
        weekly = {"title": "Halaqa", "startAt": "2026-11-05T10:00:00.000Z", "rrule": "FREQ=WEEKLY",
                  "timeAnchor": {"start": {"prayer": "asr", "offset": 0}, "end": {"prayer": "maghrib", "offset": 0}}}
        self.assertParity(*await self.both("POST", "/events", weekly))
        reminder = {"title": "Adhkar", "remindAt": "2026-11-05T05:00:00.000Z", "timeAnchor": {"start": {"prayer": "fajr", "offset": -10}}}
        self.assertParity(*await self.both("POST", "/reminders", reminder))
        self.assertParity(*await self.both("GET", "/calendar/feed?from=2026-11-04&to=2026-11-20"))
        self.assertParity(*await self.both("GET", "/events?from=2026-11-01&to=2026-11-30&expand=true"))
        # a new place moves what follows a prayer the next time it is worked out
        self.assertParity(*await self.both("PUT", "/prayer-times/settings", {**BEIRUT, "method": "MuslimWorldLeague"}))
        self.assertParity(*await self.both("GET", "/calendar/feed?from=2026-11-04&to=2026-11-20"))


if __name__ == "__main__":
    unittest.main()
