"""The free months of Plus: 60 days from email confirmation, once per person.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.entitlements import (
    confirm_email,
    effective_plan,
    grant_plus,
    normalise_email,
    start_trial,
)


def _in(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _near(test, actual: datetime, expected: datetime) -> None:
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    test.assertLess(abs((actual - expected).total_seconds()), 60, f"{actual} vs {expected}")


class TrialBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db

        self.db = db
        await db.connect()
        self.started = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.ids: list[str] = []
        self.env = patch.dict(os.environ, {"PLUS_TRIAL_FROM": _iso(_in(-1 / 24))})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.db.user.delete_many(where={"id": {"in": self.ids}})
        await self.db.trialclaim.delete_many(where={"createdAt": {"gte": self.started}})
        await self.db.disconnect()

    async def _user(self, email: str | None = None, **data):
        user = await self.db.user.create(
            data={"email": email or f"trial-{uuid.uuid4().hex}@example.test", **data}
        )
        self.ids.append(user.id)
        return user

    async def _reload(self, user):
        return await self.db.user.find_unique(where={"id": user.id})


class Trial(TrialBase):
    def test_normalise_email(self):
        self.assertEqual(normalise_email("A.B+x@GoogleMail.com"), "ab@googlemail.com")
        self.assertEqual(normalise_email(" A.b+x@Gmail.com "), "ab@gmail.com")
        self.assertEqual(normalise_email("a.b+x@example.com"), "a.b+x@example.com")

    async def test_confirm_starts_a_60_day_trial(self):
        user = await confirm_email(self.db, await self._user())
        self.assertEqual(effective_plan(user), "plus")
        self.assertEqual(user.planSource, "trial")
        _near(self, user.trialEndsAt, _in(60))
        self.assertEqual(user.planExpiresAt, user.trialEndsAt)

    async def test_no_trial_while_switched_off(self):
        with patch.dict(os.environ, {"PLUS_TRIAL_FROM": ""}):
            user = await confirm_email(self.db, await self._user())
        self.assertEqual(effective_plan(user), "free")
        self.assertIsNone(user.trialEndsAt)
        self.assertEqual(await self.db.trialclaim.count(where={"createdAt": {"gte": self.started}}), 0)

    async def test_no_trial_for_accounts_from_before(self):
        user = await self._user(createdAt=_in(-2))
        user = await confirm_email(self.db, user)
        self.assertEqual(effective_plan(user), "free")
        self.assertIsNone(user.trialEndsAt)

    async def test_only_one_trial(self):
        user = await self._user()
        self.assertTrue(await start_trial(self.db, user))
        first = (await self._reload(user)).trialEndsAt
        self.assertFalse(await start_trial(self.db, await self._reload(user)))
        self.assertEqual((await self._reload(user)).trialEndsAt, first)

    async def test_deleted_and_back_gets_no_second_trial(self):
        tag = uuid.uuid4().hex[:10]
        first = await self._user(f"x.y{tag}@gmail.com")
        self.assertTrue(await start_trial(self.db, first))
        await self.db.user.delete(where={"id": first.id})
        again = await self._user(f"xy{tag}+new@gmail.com")
        self.assertFalse(await start_trial(self.db, again))
        again = await confirm_email(self.db, again)
        self.assertEqual(effective_plan(again), "free")

    async def test_provider_identity_blocks_a_second_trial(self):
        subject = f"sub-{uuid.uuid4().hex}"
        first = await self._user()
        await self.db.externalidentity.create(
            data={"userId": first.id, "issuer": "https://accounts.google.com", "subject": subject}
        )
        self.assertTrue(await start_trial(self.db, first))
        await self.db.user.delete(where={"id": first.id})
        again = await self._user()
        await self.db.externalidentity.create(
            data={"userId": again.id, "issuer": "https://accounts.google.com", "subject": subject}
        )
        self.assertFalse(await start_trial(self.db, again))

    async def test_trial_skipped_while_already_on_plus(self):
        lifetime = await self._user(plan="plus", planSource="manual")
        self.assertFalse(await start_trial(self.db, lifetime))
        lifetime = await self._reload(lifetime)
        self.assertIsNone(lifetime.planExpiresAt)
        self.assertIsNone(lifetime.trialEndsAt)
        apple = await self._user(plan="plus", planSource="apple", planExpiresAt=_in(90))
        self.assertFalse(await start_trial(self.db, apple))
        apple = await self._reload(apple)
        self.assertEqual(apple.planSource, "apple")
        _near(self, apple.planExpiresAt, _in(90))
        self.assertEqual(await self.db.trialclaim.count(where={"createdAt": {"gte": self.started}}), 0)

    async def test_trial_never_shortens_lifetime(self):
        user = await self._user(plan="plus", planSource="manual")
        await grant_plus(self.db, user.id, _in(60), "trial", None)
        self.assertIsNone((await self._reload(user)).planExpiresAt)

    async def test_paddle_during_trial_takes_over(self):
        user = await self._user()
        await start_trial(self.db, user)
        await grant_plus(self.db, user.id, _in(30), "paddle", "sub_1")
        user = await self._reload(user)
        self.assertEqual(user.planSource, "paddle")
        _near(self, user.planExpiresAt, _in(30))
        self.assertEqual(user.paddleSubscriptionId, "sub_1")

    async def test_an_expired_purchase_never_ends_the_trial(self):
        # The app replays old transactions on launch: one that already ended changes nothing.
        user = await self._user()
        await start_trial(self.db, user)
        await grant_plus(self.db, user.id, _in(-3), "apple", f"orig-{uuid.uuid4().hex}")
        user = await self._reload(user)
        self.assertEqual((user.planSource, effective_plan(user)), ("trial", "plus"))
        _near(self, user.planExpiresAt, _in(60))

    async def test_apple_during_trial_takes_over(self):
        user = await self._user()
        await start_trial(self.db, user)
        await grant_plus(self.db, user.id, _in(30), "apple", f"orig-{uuid.uuid4().hex}")
        user = await self._reload(user)
        self.assertEqual(user.planSource, "apple")
        _near(self, user.planExpiresAt, _in(30))


class TrialReminder(TrialBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent: list[tuple[str, datetime]] = []

        async def capture(to, ends_at):
            self.sent.append((to, ends_at))
            return True

        self.mail = patch("app.email.send_trial_ending", capture)
        self.mail.start()

    async def asyncTearDown(self):
        self.mail.stop()
        await super().asyncTearDown()

    async def _on_trial(self, days_left: float):
        user = await self._user()
        await start_trial(self.db, user)
        ends = _in(days_left)
        await self.db.user.update(
            where={"id": user.id}, data={"trialEndsAt": ends, "planExpiresAt": ends}
        )
        return await self._reload(user)

    def _mine(self, user) -> list:
        return [s for s in self.sent if s[0] == user.email]

    async def test_reminder_sent_once_in_the_last_week(self):
        from app.trial_reminders import send_trial_reminders

        user = await self._on_trial(5)
        await send_trial_reminders(datetime.now(timezone.utc))
        self.assertEqual(len(self._mine(user)), 1)
        self.assertIsNotNone((await self._reload(user)).trialReminderSentAt)
        await send_trial_reminders(datetime.now(timezone.utc))
        self.assertEqual(len(self._mine(user)), 1)

    async def test_reminder_not_sent_early(self):
        from app.trial_reminders import send_trial_reminders

        user = await self._on_trial(20)
        await send_trial_reminders(datetime.now(timezone.utc))
        self.assertEqual(self._mine(user), [])

    async def test_reminder_skips_subscribers(self):
        from app.trial_reminders import send_trial_reminders

        user = await self._on_trial(5)
        await grant_plus(self.db, user.id, _in(30), "paddle", f"sub_{uuid.uuid4().hex[:8]}")
        await send_trial_reminders(datetime.now(timezone.utc))
        self.assertEqual(self._mine(user), [])

    def test_reminder_job_is_scheduled(self):
        from app.os import jobs

        self.assertIn("plus-trial-reminders", jobs.due_jobs(datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)))
        self.assertNotIn("plus-trial-reminders", jobs.due_jobs(datetime(2026, 11, 10, 9, 1, tzinfo=timezone.utc)))
