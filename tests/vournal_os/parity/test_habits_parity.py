"""Habits answer like NestJS did, in Beirut time: daily, custom and weekly schedules,
back-dated check-ins, streaks, history and stats, archive and reorder."""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from vournal_os.parity.harness import ParityCase


def local_day(offset: int) -> str:
    return (datetime.now(ZoneInfo("Asia/Beirut")) + timedelta(days=offset)).date().isoformat()


class HabitsParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut", "weekStartsOn": 1}))

    async def make(self, body):
        nest, py = await self.both("POST", "/habits", body)
        self.assertParity(nest, py)
        return nest.json().get("id"), py.json().get("id")

    async def on_both(self, method, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (await self.nest.request(method, f"/habits/{ids[0]}{suffix}", **kwargs),
                await self.py.request(method, f"/api/habits/{ids[1]}{suffix}", **kwargs))

    async def test_habits(self):
        water = await self.make({"name": " Water ", "targetPerDay": 3, "unit": "glasses", "startDate": local_day(-20), "icon": "droplet", "color": "#0088ff"})
        gym = await self.make({"name": "Gym", "frequency": "CUSTOM", "daysOfWeek": [5, 1, 3], "startDate": local_day(-30)})
        read = await self.make({"name": "Read", "frequency": "WEEKLY", "timesPerWeek": 3, "startDate": local_day(-25), "reminderTime": "21:30"})
        for offset in (-10, -9, -8, -5, -4, -3, -2, -1):
            self.assertParity(*await self.on_both("POST", water, "/check", {"date": local_day(offset), "count": 3}))
        for offset in range(-21, 0, 2):
            self.assertParity(*await self.on_both("POST", gym, "/check", {"date": local_day(offset)}))
        for offset in (-15, -14, -13, -8, -6, -1):
            self.assertParity(*await self.on_both("POST", read, "/check", {"date": local_day(offset), "note": "chapter"}))
        self.assertParity(*await self.on_both("POST", water, "/check", {}))
        self.assertParity(*await self.on_both("POST", water, "/check", {"count": 0, "date": local_day(-4)}))
        self.assertParity(*await self.on_both("DELETE", water, f"/check?date={local_day(-3)}"))
        for query in ("", "?today=true", "?archived=false"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/habits{query}"))
        for ids in (water, gym, read):
            self.assertParity(*await self.on_both("GET", ids))
            self.assertParity(*await self.on_both("GET", ids, f"/history?from={local_day(-30)}"))
        for query in ("", f"?from={local_day(-40)}&to={local_day(0)}", "?includeArchived=true", f"?from={local_day(1)}&to={local_day(0)}", "?from=2020-01-01&to=2026-01-01"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/habits/stats{query}"))
        for body in ({"frequency": "WEEKLY"}, {"frequency": "CUSTOM", "daysOfWeek": [0]}, {"timesPerWeek": None}, {"name": None}, {"targetPerDay": 1, "startDate": local_day(-60)}):
            with self.subTest(body=body):
                self.assertParity(*await self.on_both("PATCH", gym, json=body))
        self.assertParity(*await self.on_both("POST", read, "/archive"))
        self.assertParity(*await self.both("GET", "/habits?archived=true"))
        self.assertParity(*await self.on_both("POST", read, "/unarchive"))
        nest_ids, py_ids = (water[0], gym[0], read[0]), (water[1], gym[1], read[1])
        self.assertParity(
            await self.nest.post("/habits/reorder", json={"items": [{"id": i, "sortOrder": n} for n, i in enumerate(reversed(nest_ids))]}),
            await self.py.post("/api/habits/reorder", json={"items": [{"id": i, "sortOrder": n} for n, i in enumerate(reversed(py_ids))]}),
        )
        self.assertParity(*await self.both("GET", "/habits"))
        self.assertParity(*await self.on_both("POST", water, "/check", {"date": "2999-01-01"}))
        self.assertParity(*await self.on_both("POST", water, "/check", {"count": None}))  # null <= 0: the day's log goes
        self.assertParity(*await self.on_both("POST", gym, "/check", {"date": "0001-01-01"}))  # Date.UTC: 1901
        self.assertParity(*await self.on_both("GET", gym, "/history?from=1901-01-01&to=1901-01-03"))
        self.assertParity(*await self.on_both("DELETE", water))

    async def test_validation(self):
        for body in ({}, {"name": "x", "frequency": "HOURLY"}, {"name": "x", "frequency": "CUSTOM"}, {"name": "x", "daysOfWeek": [1, 1]},
                     {"name": "x", "daysOfWeek": [9]}, {"name": "x", "timesPerWeek": 8}, {"name": "x", "startDate": "2026-1-1"},
                     {"name": "x", "reminderTime": "25:00"}, {"name": "x", "targetPerDay": 0}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/habits", body))


if __name__ == "__main__":
    unittest.main()
