"""Calendar and reminder fixes from the phase 3 review: cleared prayer anchors are a
database NULL (NestJS's Prisma.DbNull), `??` keeps empty strings, monthly reminders on
the 31st stay on the month's last day."""

import unittest

from prisma import Json

from vournal_os.support import OsTestCase

ANCHOR = Json({"start": {"prayer": "fajr", "offset": 0}})


class CalendarFixes(OsTestCase):
    async def _is_db_null(self, table: str, id: str) -> bool:
        rows = await self.db.query_raw(f'SELECT "timeAnchor" IS NULL AS n FROM "{table}" WHERE id = $1', id)
        return rows[0]["n"]

    async def test_cleared_prayer_anchors_are_database_null(self):
        client = await self.signed_in()
        uid = client.user.id
        task = await self.db.task.create(data={"userId": uid, "title": "Pray", "dueAt": "2026-11-20T04:00:00Z", "hasDueTime": True, "timeAnchor": ANCHOR})
        event = await self.db.event.create(data={"userId": uid, "title": "Pray", "startAt": "2026-11-20T04:00:00Z", "endAt": "2026-11-20T05:00:00Z", "timeAnchor": ANCHOR})
        reminder = await self.db.reminder.create(data={"userId": uid, "title": "Pray", "remindAt": "2026-11-20T04:00:00Z", "timeAnchor": ANCHOR})
        for path, table, id in (("/api/tasks", "Task", task.id), ("/api/events", "Event", event.id), ("/api/reminders", "Reminder", reminder.id)):
            with self.subTest(table=table):
                response = await client.patch(f"{path}/{id}", json={"timeAnchor": None})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(await self._is_db_null(table, id), f"{table}.timeAnchor holds JSON null")

    async def test_monthly_reminder_on_the_31st_keeps_the_month_end(self):
        client = await self.signed_in()
        made = await client.post("/api/reminders", json={"title": "Rent", "remindAt": "2027-01-31T09:00:00.000Z", "rrule": "FREQ=MONTHLY"})
        self.assertEqual(made.status_code, 201, made.text)
        seen = []
        for _ in range(3):
            done = (await client.post(f"/api/reminders/{made.json()['id']}/done", json={})).json()
            seen.append(done["remindAt"][:10])
        # NestJS anchored on the current remindAt: 31 Jan → 28 Feb → 28 Mar → 28 Apr
        self.assertEqual(seen, ["2027-02-28", "2027-03-31", "2027-04-30"])

    async def test_moving_a_monthly_reminder_moves_its_day(self):
        client = await self.signed_in()
        made = (await client.post("/api/reminders", json={"title": "Bill", "remindAt": "2027-01-31T09:00:00.000Z", "rrule": "FREQ=MONTHLY"})).json()
        moved = await client.patch(f"/api/reminders/{made['id']}", json={"remindAt": "2027-01-15T09:00:00.000Z"})
        self.assertEqual(moved.status_code, 200, moved.text)
        done = (await client.post(f"/api/reminders/{made['id']}/done", json={})).json()
        self.assertEqual(done["remindAt"][:10], "2027-02-15")
        own = (await client.post("/api/reminders", json={"title": "Own", "remindAt": "2027-01-10T09:00:00.000Z", "rrule": "FREQ=MONTHLY;BYMONTHDAY=20"})).json()
        await client.patch(f"/api/reminders/{own['id']}", json={"remindAt": "2027-01-12T09:00:00.000Z"})
        self.assertEqual((await self.db.reminder.find_unique(where={"id": own["id"]})).rrule, "FREQ=MONTHLY;BYMONTHDAY=20")

    async def test_empty_strings_count_as_values(self):
        client = await self.signed_in()
        uid = client.user.id
        await self.db.debt.create(data={"userId": uid, "direction": "OWED_TO_ME", "title": "Lunch", "counterparty": "", "principal": "5", "currency": "USD", "startDate": "2026-11-01T00:00:00Z", "dueDate": "2026-11-20T00:00:00Z"})
        items = (await client.get("/api/calendar/feed?from=2026-11-20&to=2026-11-20&types=debt")).json()["items"]
        self.assertEqual(items[0]["meta"]["who"], "")  # d.person?.name ?? d.counterparty ?? d.title


if __name__ == "__main__":
    unittest.main()
