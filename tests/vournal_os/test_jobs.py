"""The scheduled jobs: NestJS's schedules (UTC), each run once across processes (a Postgres
advisory lock, released when a run crashes), accounts without Plus skipped, and one
failing account never stopping a run."""

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from vournal_os.support import OsTestCase


def utc(hh: int, mm: int) -> datetime:
    return datetime(2026, 11, 4, hh, mm, tzinfo=timezone.utc)


class Schedule(unittest.TestCase):
    def test_the_minutes_each_job_runs(self):
        from app.os import jobs

        tick, sweep, cleanup = "notifications-tick", "notifications-sweep", "notifications-cleanup"
        rates, repeat, autolog = "market-rates", "finance-recurring-transactions", "finance-recurring-autolog"
        self.assertEqual(jobs.due_jobs(utc(3, 0)), [tick, sweep, cleanup, repeat, autolog])
        self.assertEqual(jobs.due_jobs(utc(4, 0)), [tick, sweep, repeat, autolog])
        self.assertEqual(jobs.due_jobs(utc(10, 45)), [tick, sweep])
        self.assertEqual(jobs.due_jobs(utc(10, 7)), [tick])
        self.assertEqual(jobs.due_jobs(utc(6, 17)), [tick, rates])
        self.assertEqual(jobs.due_jobs(utc(0, 17)), [tick, rates])
        self.assertEqual(jobs.due_jobs(utc(5, 17)), [tick])
        self.assertEqual(jobs.due_jobs(utc(3, 0) + timedelta(seconds=42)), jobs.due_jobs(utc(3, 0)))

    def test_switched_off_by_the_environment(self):
        from app.os import jobs

        with patch.dict(os.environ, {"VOURNAL_OS_JOBS": "0"}):
            self.assertIsNone(jobs.start())


class Jobs(OsTestCase):
    async def test_two_runners_run_a_job_once(self):
        from app.os import jobs

        runs = []

        async def work():
            runs.append(1)
            await asyncio.sleep(0.3)

        results = await asyncio.gather(jobs.run_locked("test-job", work), jobs.run_locked("test-job", work))
        self.assertEqual((sorted(results), len(runs)), ([False, True], 1))
        self.assertTrue(await jobs.run_locked("test-job", work))  # the lock went with the run
        self.assertEqual(len(runs), 2)

    async def test_a_crashed_run_releases_the_lock(self):
        from app.os import jobs

        async def crash():
            raise RuntimeError("boom")

        with self.assertLogs("vournal.os", "ERROR"):
            self.assertTrue(await jobs.run_locked("test-crash", crash))
        self.assertTrue(await jobs.run_locked("test-crash", AsyncMock()))

    async def due_reminder(self, user, title):
        return await self.db.reminder.create(data={"userId": user.id, "title": title, "remindAt": datetime.now(timezone.utc) - timedelta(minutes=3)})

    async def test_the_tick_skips_accounts_without_plus(self):
        from app.os import jobs

        plus = await self.make_user()
        free = await self.make_user(plan="free")
        lapsed = await self.make_user(planExpiresAt=datetime.now(timezone.utc) - timedelta(days=1))
        reminders = {u.id: await self.due_reminder(u, "Pills") for u in (plus, free, lapsed)}
        await jobs.notifications_tick()
        made = {u.id: await self.db.notification.count(where={"userId": u.id, "entityType": "REMINDER"}) for u in (plus, free, lapsed)}
        self.assertEqual(made, {plus.id: 1, free.id: 0, lapsed.id: 0})
        for user in (free, lapsed):  # their data is untouched
            self.assertIsNone((await self.db.reminder.find_unique(where={"id": reminders[user.id].id})).lastFiredAt)

    async def test_a_failing_account_does_not_stop_the_run(self):
        from app.os import jobs, notification_sync

        first, second = await self.make_user(), await self.make_user()
        for user in (first, second):
            await self.due_reminder(user, "Water")
        real = notification_sync.sync_for_user

        async def flaky(user, full=True):
            if user.id == first.id:
                raise RuntimeError("broken account")
            return await real(user, full)

        with patch.object(notification_sync, "sync_for_user", flaky), self.assertLogs("vournal.os", "ERROR"):
            await jobs.notifications_tick()
        self.assertEqual(await self.db.notification.count(where={"userId": second.id, "entityType": "REMINDER"}), 1)

    async def test_the_finance_jobs_skip_accounts_without_plus(self):
        from app.os import jobs, recurring_payments, transactions

        plus, free = await self.make_user(), await self.make_user(plan="free")
        for user in (plus, free):
            await self.db.recurringpayment.create(data={"userId": user.id, "name": "Rent", "amount": 700, "currency": "USD", "status": "ACTIVE",
                                                        "autoLogExpense": True, "rrule": "FREQ=MONTHLY", "startDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
                                                        "nextDate": datetime(2026, 1, 1, tzinfo=timezone.utc)})
            await self.db.transaction.create(data={"userId": user.id, "type": "EXPENSE", "title": "Gym", "amount": 30, "currency": "USD",
                                                   "date": datetime(2026, 1, 1, tzinfo=timezone.utc), "rrule": "FREQ=MONTHLY"})
        autolog, repeat = AsyncMock(return_value=0), AsyncMock(return_value=0)
        with patch.object(recurring_payments, "auto_log_due", autolog), patch.object(transactions, "materialize_due", repeat):
            await jobs.finance_autolog()
            await jobs.finance_repeat()
        for mock in (autolog, repeat):
            called = {c.args[0] for c in mock.await_args_list}
            self.assertIn(plus.id, called)
            self.assertNotIn(free.id, called)


if __name__ == "__main__":
    unittest.main()
