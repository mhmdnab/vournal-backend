"""Events answer like NestJS did, in a Beirut-time account."""

import unittest

from vournal_os.parity.harness import ParityCase


class EventsParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut", "language": "ar"}))

    async def make(self, body):
        nest, py = await self.both("POST", "/events", body)
        self.assertParity(nest, py)
        return nest.json()["id"], py.json()["id"]

    async def on_both(self, method, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, f"/events/{ids[0]}{suffix}", **kwargs),
            await self.py.request(method, f"/api/events/{ids[1]}{suffix}", **kwargs),
        )

    async def test_event_crud(self):
        ids = await self.make({"title": " Dentist ", "startAt": "2026-11-20T08:30:00.000Z", "endAt": "2026-11-20T09:15:00.000Z", "type": "APPOINTMENT", "location": " Hamra ", "reminderMinutes": 30})
        self.assertParity(*await self.on_both("GET", ids))
        self.assertParity(*await self.on_both("PATCH", ids, json={"startAt": "2026-11-21T08:30:00.000Z"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"date": "2026-11-22"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"endAt": None, "color": "#112233", "priority": "HIGH"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"endAt": "2026-11-01T00:00:00.000Z"}))
        self.assertParity(*await self.on_both("DELETE", ids))
        self.assertParity(*await self.on_both("GET", ids))

    async def test_list_range_expands_repeats(self):
        await self.make({"title": "Gym", "startAt": "2026-10-05T16:00:00.000Z", "endAt": "2026-10-05T17:00:00.000Z", "rrule": "FREQ=WEEKLY;BYDAY=MO,TH"})
        await self.make({"title": "Standup", "startAt": "2026-10-20T06:00:00.000Z", "rrule": "FREQ=DAILY;COUNT=10"})
        await self.make({"title": "Trip", "date": "2026-10-30", "endAt": "2026-11-02"})
        await self.make({"title": "Rent", "startAt": "2026-01-31", "rrule": "FREQ=MONTHLY"})
        await self.make({"title": "Old", "startAt": "2025-01-01T10:00:00.000Z"})
        for query in (
            "", "?from=2026-10-25&to=2026-11-03&expand=true", "?from=2026-10-25&to=2026-11-03",
            "?from=2026-11-01T00:00:00.000Z&to=2026-11-30T23:59:59.000Z&expand=true&pageSize=5&page=2",
            "?recurring=true", "?recurring=false", "?q=gym", "?type=GENERAL", "?from=2026-11-10&to=2026-11-01",
            "?expand=maybe", "?type=PARTY",
        ):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/events{query}"))

    async def test_exclude_occurrence(self):
        ids = await self.make({"title": "Class", "startAt": "2026-11-02T15:00:00.000Z", "rrule": "FREQ=WEEKLY"})
        for body in ({"date": "2026-11-09"}, {"date": "2026-11-16T15:00:00.000Z"}, {"date": "2026-11-09"}, {"date": "2026-11-10"}, {}):
            self.assertParity(*await self.on_both("POST", ids, "/exclude", body))
        self.assertParity(*await self.both("GET", "/events?from=2026-11-01&to=2026-11-30&expand=true"))
        once = await self.make({"title": "Once", "startAt": "2026-11-02T15:00:00.000Z"})
        self.assertParity(*await self.on_both("POST", once, "/exclude", {"date": "2026-11-02"}))

    async def test_detach_occurrence(self):
        ids = await self.make({"title": "Lesson", "startAt": "2026-11-03T14:00:00.000Z", "endAt": "2026-11-03T15:00:00.000Z", "rrule": "FREQ=WEEKLY;BYDAY=TU"})
        self.assertParity(*await self.on_both("POST", ids, "/occurrence", {"occurrence": "2026-11-10", "title": "Lesson (moved)", "startAt": "2026-11-11T14:00:00.000Z"}))
        self.assertParity(*await self.on_both("POST", ids, "/occurrence", {"occurrence": "2026-11-17", "allDay": True}))
        self.assertParity(*await self.on_both("POST", ids, "/occurrence", {"occurrence": "2026-11-18"}))
        self.assertParity(*await self.both("GET", "/events?from=2026-11-01&to=2026-11-30&expand=true"))

    async def test_all_day_events(self):
        ids = await self.make({"title": "Holiday", "startAt": "2026-11-22", "endAt": "2026-11-24"})
        self.assertParity(*await self.on_both("PATCH", ids, json={"allDay": False}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"allDay": True}))
        yearly = await self.make({"title": "Birthday", "date": "2026-02-28", "rrule": "FREQ=YEARLY"})
        self.assertParity(*await self.on_both("PATCH", yearly, json={"rrule": None}))
        self.assertParity(*await self.both("GET", "/events?from=2026-11-01&to=2026-11-30&expand=true"))

    async def test_validation_shapes(self):
        for body in (
            {}, {"title": ""}, {"title": "x", "type": "PARTY"}, {"title": "x", "color": "red"},
            {"title": "x", "startAt": "soon"}, {"title": "x", "rrule": "FREQ=SECONDLY"},
            {"title": "x", "reminderMinutes": 40321}, {"title": "x", "exDates": ["nope"]},
            {"title": "x", "shared": True, "startAt": "2026-11-05T10:00:00.000Z"},
            {"title": "x", "taskId": "cnosuchtaskidatallxxxxxx"},
            {"title": "x", "timeAnchor": {"start": {"prayer": "maghrib", "offset": 0}}, "startAt": "2026-11-05T10:00:00.000Z"},
            {"title": "x", "startAt": "2026-11-05T10:00:00.000Z", "endAt": "2026-11-05T09:00:00.000Z"},
            {"title": "x", "bogus": 1},
        ):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/events", body))


if __name__ == "__main__":
    unittest.main()
