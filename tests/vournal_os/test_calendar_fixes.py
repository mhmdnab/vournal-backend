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
                before = (await self.db.query_raw(f'SELECT "updatedAt" AS u FROM "{table}" WHERE id = $1', id))[0]["u"]
                response = await client.patch(f"{path}/{id}", json={"timeAnchor": None})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(response.json()["timeAnchor"])
                self.assertTrue(await self._is_db_null(table, id), f"{table}.timeAnchor holds JSON null")
                after = (await self.db.query_raw(f'SELECT "updatedAt" AS u FROM "{table}" WHERE id = $1', id))[0]["u"]
                self.assertNotEqual(after, before, "updatedAt moves, as Prisma's @updatedAt did")

    async def test_reminder_rules_are_stored_as_sent(self):
        """No rewriting of a person's rule: the dashboard's "Every month" preset already
        sends BYMONTHDAY, and a drag must not change it (NestJS kept rules as sent)."""
        client = await self.signed_in()
        made = (await client.post("/api/reminders", json={"title": "Bill", "remindAt": "2027-01-20T09:00:00.000Z", "rrule": "FREQ=MONTHLY;BYMONTHDAY=20"})).json()
        await client.patch(f"/api/reminders/{made['id']}", json={"remindAt": "2027-01-22T09:00:00.000Z"})
        self.assertEqual((await self.db.reminder.find_unique(where={"id": made["id"]})).rrule, "FREQ=MONTHLY;BYMONTHDAY=20")
        bare = (await client.post("/api/reminders", json={"title": "Rent", "remindAt": "2027-01-31T09:00:00.000Z", "rrule": "FREQ=MONTHLY"})).json()
        self.assertEqual(bare["rrule"], "FREQ=MONTHLY")

    async def test_empty_strings_count_as_values(self):
        client = await self.signed_in()
        uid = client.user.id
        await self.db.debt.create(data={"userId": uid, "direction": "OWED_TO_ME", "title": "Lunch", "counterparty": "", "principal": "5", "currency": "USD", "startDate": "2026-11-01T00:00:00Z", "dueDate": "2026-11-20T00:00:00Z"})
        items = (await client.get("/api/calendar/feed?from=2026-11-20&to=2026-11-20&types=debt")).json()["items"]
        self.assertEqual(items[0]["meta"]["who"], "")  # d.person?.name ?? d.counterparty ?? d.title


if __name__ == "__main__":
    unittest.main()
