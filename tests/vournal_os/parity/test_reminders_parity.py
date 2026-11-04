"""Reminders answer like NestJS did, in a Beirut-time account."""

import unittest

from vournal_os.parity.harness import ParityCase


class RemindersParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def make(self, body):
        nest, py = await self.both("POST", "/reminders", body)
        self.assertParity(nest, py)
        return nest.json()["id"], py.json()["id"]

    async def on_both(self, method, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, f"/reminders/{ids[0]}{suffix}", **kwargs),
            await self.py.request(method, f"/api/reminders/{ids[1]}{suffix}", **kwargs),
        )

    async def test_reminder_crud(self):
        await self.make({"title": "Call bank", "remindAt": "2026-12-01T07:00:00.000Z", "notes": "about the card"})
        await self.make({"title": "Pay fees", "date": "2026-12-03"})
        await self.make({"title": "Pick up", "date": "2026-12-04", "time": "17:30"})
        await self.make({"title": "Old", "remindAt": "2020-01-01T07:00:00.000Z", "isDeadline": True})
        ids = await self.make({"title": "Allday flag", "remindAt": "2026-12-05T10:00:00.000Z", "allDay": True})
        for query in ("", "?status=all", "?status=overdue", "?status=upcoming", "?status=today", "?status=done",
                      "?deadline=true", "?q=bank", "?from=2026-12-01&to=2026-12-03", "?status=nope", "?recurring=false"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/reminders{query}"))
        self.assertParity(*await self.on_both("PATCH", ids, json={"time": "08:15"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"allDay": True}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"title": "Renamed"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"isDone": True}))
        self.assertParity(*await self.both("GET", "/reminders?status=done"))
        self.assertParity(*await self.on_both("DELETE", ids))
        self.assertParity(*await self.on_both("GET", ids))

    async def test_done_repeating_moves_on(self):
        ids = await self.make({"title": "Pills", "remindAt": "2026-11-01T06:00:00.000Z", "rrule": "FREQ=DAILY"})
        self.assertParity(*await self.on_both("POST", ids, "/done"))
        self.assertParity(*await self.on_both("POST", ids, "/done"))
        once = await self.make({"title": "Once", "remindAt": "2026-12-01T06:00:00.000Z"})
        self.assertParity(*await self.on_both("POST", once, "/done"))
        ends = await self.make({"title": "Ends", "remindAt": "2026-11-01T06:00:00.000Z", "rrule": "FREQ=DAILY;COUNT=2"})
        self.assertParity(*await self.on_both("POST", ends, "/done"))

    async def test_monthly_on_the_31st(self):
        """A bare monthly repeat follows the current remindAt, as NestJS did (31 Jan → 28 Feb → 28 Mar)."""
        ids = await self.make({"title": "Rent", "remindAt": "2027-01-31T09:00:00.000Z", "rrule": "FREQ=MONTHLY"})
        for _ in range(3):
            self.assertParity(*await self.on_both("POST", ids, "/done", {}))

    async def test_snooze(self):
        ids = await self.make({"title": "Stretch", "remindAt": "2026-11-01T06:00:00.000Z"})
        self.assertParity(*await self.on_both("POST", ids, "/snooze", {"until": "2026-12-24T06:00:00.000Z"}))
        for body in ({}, {"until": "2020-01-01T00:00:00.000Z"}, {"until": "2030-01-01T00:00:00.000Z"}, {"minutes": 0}, {"minutes": 50000}, {"minutes": "x"}):
            self.assertParity(*await self.on_both("POST", ids, "/snooze", body))
        self.assertParity(*await self.on_both("POST", ids, "/done"))
        self.assertParity(*await self.on_both("POST", ids, "/snooze", {"minutes": 10}))

    async def test_linked_entity(self):
        nest_task = (await self.nest.post("/tasks", json={"title": "Renew passport"})).json()["id"]
        py_task = (await self.py.post("/api/tasks", json={"title": "Renew passport"})).json()["id"]
        nest, py = (
            await self.nest.post("/reminders", json={"title": "Passport", "date": "2026-12-10", "entityType": "TASK", "entityId": nest_task}),
            await self.py.post("/api/reminders", json={"title": "Passport", "date": "2026-12-10", "entityType": "TASK", "entityId": py_task}),
        )
        self.assertParity(nest, py)
        self.assertParity(
            await self.nest.get(f"/reminders?entityType=TASK&entityId={nest_task}"),
            await self.py.get(f"/api/reminders?entityType=TASK&entityId={py_task}"),
        )
        ids = (nest.json()["id"], py.json()["id"])
        self.assertParity(*await self.on_both("PATCH", ids, json={"entityType": None}))
        self.assertParity(*await self.both("POST", "/reminders", {"title": "x", "entityType": "TASK"}))
        self.assertParity(*await self.both("POST", "/reminders", {"title": "x", "entityType": "TASK", "entityId": "cnosuchtaskidatallxxxxxx"}))

    async def test_validation_shapes(self):
        for body in (
            {}, {"title": ""}, {"title": "x", "time": "25:00"}, {"title": "x", "remindAt": "later"},
            {"title": "x", "rrule": "FREQ=MINUTELY"}, {"title": "x", "entityType": "PLANET", "entityId": "a"},
            {"title": "x", "timeAnchor": {"start": {"prayer": "fajr", "offset": 0}}}, {"title": "x", "bogus": 1},
        ):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/reminders", body))


if __name__ == "__main__":
    unittest.main()
