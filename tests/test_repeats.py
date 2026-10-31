"""Repeating to-dos: the next day, the first day, and what ticking one does.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import os
import unittest
import uuid
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx

from app.task_intelligence import first_occurrence_day, next_occurrence_day

MON = date(2026, 11, 2)


def later(anchor, freq, interval=1, weekdays=(), after=None, until=None):
    return next_occurrence_day(anchor, freq, interval, list(weekdays), after or anchor, until)


class NextOccurrence(unittest.TestCase):
    def test_daily(self):
        self.assertEqual(later(MON, "daily"), date(2026, 11, 3))
        self.assertEqual(later(MON, "daily", 2), date(2026, 11, 4))

    def test_weekly_without_days_keeps_its_weekday(self):
        self.assertEqual(later(MON, "weekly"), date(2026, 11, 9))
        self.assertEqual(later(MON, "weekly", 2), date(2026, 11, 16))

    def test_weekly_with_days_and_interval(self):
        days = ["MO", "TH"]
        seen, day = [], MON
        for _ in range(3):
            day = later(day, "weekly", 2, days)
            seen.append(day)
        self.assertEqual(seen, [date(2026, 11, 5), date(2026, 11, 16), date(2026, 11, 19)])

    def test_overdue_repeat_lands_in_the_future(self):
        self.assertEqual(later(date(2026, 10, 5), "weekly", after=MON), date(2026, 11, 9))
        self.assertEqual(later(date(2026, 10, 5), "daily", after=MON), date(2026, 11, 3))

    def test_ticked_early_still_moves_past_its_own_day(self):
        self.assertEqual(later(date(2026, 11, 9), "weekly", after=MON), date(2026, 11, 16))

    def test_monthly_31st_skips_short_months(self):
        self.assertEqual(later(date(2027, 1, 31), "monthly"), date(2027, 3, 31))
        self.assertEqual(later(date(2028, 2, 29), "yearly"), date(2032, 2, 29))

    def test_monthly_overdue_lands_in_the_future(self):
        self.assertEqual(later(date(2026, 1, 15), "monthly", after=MON), date(2026, 11, 15))

    def test_past_until_ends_the_series(self):
        self.assertIsNone(later(MON, "weekly", until=date(2026, 11, 8)))
        self.assertEqual(later(MON, "weekly", until=date(2026, 11, 9)), date(2026, 11, 9))

    def test_no_repeat_has_no_next_day(self):
        self.assertIsNone(later(MON, None))


class FirstOccurrence(unittest.TestCase):
    SATURDAY = date(2026, 10, 31)

    def test_first_occurrence(self):
        self.assertEqual(first_occurrence_day("daily", [], self.SATURDAY), self.SATURDAY)
        self.assertEqual(first_occurrence_day("weekly", ["MO", "TH"], self.SATURDAY), MON)
        self.assertEqual(first_occurrence_day("weekly", ["MO", "TH"], MON), MON)

    def test_repeats_that_dont_say_when_have_no_first_day(self):
        self.assertIsNone(first_occurrence_day("weekly", [], self.SATURDAY))
        self.assertIsNone(first_occurrence_day("monthly", [], self.SATURDAY))
        self.assertIsNone(first_occurrence_day(None, [], self.SATURDAY))


class Endpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db
        from app.main import app

        self.db = db
        await db.connect()
        self.address = f"repeats-{uuid.uuid4().hex}@example.test"
        from app.rate_limit import limiter

        limiter.reset()  # the suite registers more than 3 accounts per hour
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://repeats"
        )
        registered = await self.client.post(
            "/auth/register", json={"email": self.address, "password": "local-passphrase"}
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        self.client.headers["Authorization"] = f"Bearer {registered.json()['accessToken']}"
        self.user = await db.user.find_unique(where={"email": self.address})
        await db.user.update(where={"id": self.user.id}, data={"timezone": "Asia/Beirut"})
        self.entry = await db.entry.create(
            data={"userId": self.user.id, "rawTranscript": "x", "status": "DONE"}
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.db.user.delete_many(where={"email": self.address})
        await self.db.disconnect()

    async def _todo(self, **fields):
        return await self.db.todo.create(
            data={"entryId": self.entry.id, "userId": self.user.id, "text": "Water plants", **fields}
        )

    async def _tick(self, todo, **body):
        response = await self.client.patch(f"/todos/{todo.id}", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_advance_moves_to_the_next_day(self):
        todo = await self._todo(recurrence="weekly", dueDate=datetime(2030, 11, 4, tzinfo=timezone.utc))
        saved = await self._tick(todo, done=True, advance=True)
        self.assertFalse(saved["done"])
        self.assertTrue(saved["dueDate"].startswith("2030-11-11"))
        self.assertIsNone(saved["startAt"])

    async def test_clock_time_survives_daylight_saving(self):
        # Clocks in Beirut go forward on Sunday 31 March 2030: 08:00 is 06:00Z, then 05:00Z.
        todo = await self._todo(
            recurrence="daily",
            dueDate=datetime(2030, 3, 30, tzinfo=timezone.utc),
            startAt=datetime(2030, 3, 30, 6, tzinfo=timezone.utc),
        )
        saved = await self._tick(todo, done=True, advance=True)
        self.assertTrue(saved["dueDate"].startswith("2030-03-31"))
        self.assertTrue(saved["startAt"].startswith("2030-03-31T05:00"), saved["startAt"])

    async def test_advance_past_until_is_done(self):
        due = datetime(2030, 11, 4, tzinfo=timezone.utc)
        todo = await self._todo(
            recurrence="weekly", dueDate=due, recurrenceUntil=datetime(2030, 11, 8, tzinfo=timezone.utc)
        )
        saved = await self._tick(todo, done=True, advance=True)
        self.assertTrue(saved["done"])
        self.assertTrue(saved["dueDate"].startswith("2030-11-04"))

    async def test_until_is_read_in_the_users_zone(self):
        # 23:59 on 11 Nov in Beirut is 21:59Z: the 11th itself is still in the series.
        todo = await self._todo(
            recurrence="weekly",
            dueDate=datetime(2030, 11, 4, tzinfo=timezone.utc),
            recurrenceUntil=datetime(2030, 11, 11, 21, 59, tzinfo=timezone.utc),
        )
        saved = await self._tick(todo, done=True, advance=True)
        self.assertFalse(saved["done"])
        self.assertTrue(saved["dueDate"].startswith("2030-11-11"))

    async def test_advance_on_a_one_time_todo_just_marks_done(self):
        saved = await self._tick(await self._todo(), done=True, advance=True)
        self.assertTrue(saved["done"])

    async def test_done_without_advance_ends_as_before(self):
        todo = await self._todo(recurrence="weekly", dueDate=datetime(2030, 11, 4, tzinfo=timezone.utc))
        saved = await self._tick(todo, done=True)
        self.assertTrue(saved["done"])
        self.assertTrue(saved["dueDate"].startswith("2030-11-04"))

    async def test_undone_with_advance_ignores_advance(self):
        todo = await self._todo(recurrence="weekly", dueDate=datetime(2030, 11, 4, tzinfo=timezone.utc), done=True)
        saved = await self._tick(todo, done=False, advance=True)
        self.assertFalse(saved["done"])
        self.assertTrue(saved["dueDate"].startswith("2030-11-04"))

    async def test_daily_without_a_day_gets_today_when_details_saved(self):
        from zoneinfo import ZoneInfo

        todo = await self._todo()
        response = await self.client.patch(f"/todos/{todo.id}/details", json={"recurrence": "daily"})
        self.assertEqual(response.status_code, 200, response.text)
        today = datetime.now(ZoneInfo("Asia/Beirut")).date().isoformat()
        self.assertTrue(response.json()["dueDate"].startswith(today))

    async def test_weekly_days_without_a_day_get_the_next_listed_day(self):
        todo = await self._todo()
        response = await self.client.patch(
            f"/todos/{todo.id}/details",
            json={"recurrence": "weekly", "recurrenceWeekdays": ["MO"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        due = date.fromisoformat(response.json()["dueDate"][:10])
        self.assertEqual(due.weekday(), 0)

    async def test_a_chosen_day_is_kept(self):
        todo = await self._todo(dueDate=datetime(2030, 11, 6, tzinfo=timezone.utc))
        response = await self.client.patch(f"/todos/{todo.id}/details", json={"recurrence": "daily"})
        self.assertTrue(response.json()["dueDate"].startswith("2030-11-06"))


if __name__ == "__main__":
    unittest.main()
