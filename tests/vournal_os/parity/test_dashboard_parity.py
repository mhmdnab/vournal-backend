"""The home screen answers like NestJS did: an empty account, a seeded one with today's
tasks, focus picks, reminders and bills, Arabic, and sections switched off."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates
from vournal_os.parity.seed import SeedMixin, at, day


class DashboardParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def test_empty_account(self):
        self.assertParity(*await self.both("GET", "/dashboard"))

    async def test_seeded_home(self):
        await self.seed_life()
        focus = await self.on_each("POST", "/tasks", {"title": "Deep work", "dueAt": day(0), "priority": "URGENT"})
        pinned = [await c.put(f"{p}/tasks/focus", json={"date": day(0), "taskIds": [i]}) for c, p, i in ((self.nest, "", focus[0]), (self.py, "/api", focus[1]))]
        self.assertParity(*pinned)
        self.assertEqual(pinned[0].status_code, 200, pinned[0].text)
        await self.on_each("POST", "/tasks", {"title": "Urgent call", "dueAt": at(day(0), 23, 30), "priority": "URGENT"})
        for body in ({"title": "Pills", "remindAt": at(day(0), 0, 5)}, {"title": "Old", "remindAt": at(day(-3), 9)},
                     {"title": "Later", "remindAt": at(day(2), 9)}):
            await self.on_each("POST", "/reminders", body)
        # one that's over already today, and an all-day one: only the second is still "upcoming"
        await self.on_each("POST", "/events", {"title": "Early swim", "startAt": at(day(0), 0, 1), "endAt": at(day(0), 0, 2)})
        await self.on_each("POST", "/events", {"title": "Holiday", "startAt": day(0), "allDay": True})
        await self.on_each("POST", "/trips", {"name": "Rome", "destination": "Italy", "startDate": day(12), "endDate": day(15)})
        course = await self.on_each("POST", "/study/courses", {"name": "Calculus"})
        await self.on_each("POST", "/study/assessments", lambda s: {"courseId": course[s], "title": "Midterm", "type": "EXAM", "dueAt": at(day(4), 10)})
        nest, py = await self.both("GET", "/dashboard")
        self.assertParity(nest, py)
        self.assertGreater(len(nest.json()["deadlines"]["items"]), 3, nest.text)
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        self.assertParity(*await self.both("GET", "/dashboard"))
        self.assertParity(*await self.both("PATCH", "/settings", {"enabledSections": ["tasks", "habits"]}))
        self.assertParity(*await self.both("GET", "/dashboard"))


if __name__ == "__main__":
    unittest.main()
