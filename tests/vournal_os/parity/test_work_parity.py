"""GET /api/work/overview answers like NestJS did: work tasks grouped (overdue, today,
upcoming, no date), meetings over the next 14 days with repeats expanded, work projects with
progress, deadlines over the next 30 days, and the stats, in English and Arabic."""

import unittest

from vournal_os.parity.harness import ParityCase
from vournal_os.parity.seed import SeedMixin, at, day


class WorkParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def test_empty(self):
        self.assertParity(*await self.both("GET", "/work/overview"))

    async def test_a_working_week(self):
        launch = await self.on_each("POST", "/projects", {"name": "Launch", "lifeArea": "WORK", "deadline": day(12), "color": "#6366f1"})
        late = await self.on_each("POST", "/projects", {"name": "Audit", "lifeArea": "WORK", "deadline": day(-3)})
        shipped = await self.on_each("POST", "/projects", {"name": "Shipped", "lifeArea": "WORK"})
        await self.on_each("PATCH", "/projects/{}", {"status": "COMPLETED"}, ids=[shipped])
        await self.on_each("POST", "/projects", {"name": "Garden", "lifeArea": "HOME"})
        for body in (
            lambda s: {"title": "Report", "lifeArea": "WORK", "dueAt": day(-2), "priority": "HIGH"},
            lambda s: {"title": "Standup notes", "lifeArea": "WORK", "dueAt": at(day(0), 23, 50), "priority": "LOW"},
            lambda s: {"title": "Review PR", "lifeArea": "WORK", "dueAt": day(0)},
            lambda s: {"title": "Slides", "projectId": launch[s], "dueAt": day(5), "priority": "URGENT"},
            lambda s: {"title": "Copy", "projectId": launch[s], "dueAt": day(40)},
            lambda s: {"title": "Findings", "projectId": late[s], "dueAt": day(-1)},
            lambda s: {"title": "Inbox zero", "lifeArea": "WORK", "priority": "URGENT"},
            lambda s: {"title": "Read docs", "lifeArea": "WORK"},
            lambda s: {"title": "Groceries", "lifeArea": "HOME", "dueAt": day(0)},
        ):
            await self.on_each("POST", "/tasks", body)
        done = await self.on_each("POST", "/tasks", {"title": "Ship it", "lifeArea": "WORK", "projectId": None})
        await self.on_each("POST", "/tasks/{}/complete", ids=[done])
        sub_parent = await self.on_each("POST", "/tasks", lambda s: {"title": "Plan", "projectId": launch[s], "dueAt": day(3)})
        await self.on_each("POST", "/tasks", lambda s: {"title": "Plan part", "parentId": sub_parent[s], "lifeArea": "WORK", "dueAt": day(2)})
        await self.on_each("POST", "/events", {"title": "Sync", "type": "MEETING", "startAt": at(day(0), 0, 5), "endAt": at(day(0), 0, 35)})
        await self.on_each("POST", "/events", {"title": "1:1", "type": "MEETING", "startAt": at(day(-6), 15), "endAt": at(day(-6), 15, 45), "rrule": "FREQ=WEEKLY"})
        await self.on_each("POST", "/events", lambda s: {"title": "Kickoff", "type": "MEETING", "startAt": at(day(3), 10), "projectId": launch[s]})
        await self.on_each("POST", "/events", {"title": "Offsite", "type": "MEETING", "date": day(8), "allDay": True})
        await self.on_each("POST", "/events", {"title": "Dinner", "startAt": at(day(1), 20), "endAt": at(day(1), 22)})
        self.assertParity(*await self.both("GET", "/work/overview"))
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar", "weekStartsOn": 0}))
        self.assertParity(*await self.both("GET", "/work/overview"))

    async def test_plus_and_query(self):
        self.assertParity(*await self.both("GET", "/work/overview?x=1"))


if __name__ == "__main__":
    unittest.main()
