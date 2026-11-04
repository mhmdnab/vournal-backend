"""Making notifications: a reminder fires once and moves on, a snooze fires once, nothing
is made twice, in-app off stores them read, and new ones go out by push (a summary past 4)."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from vournal_os.support import OsTestCase


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Sync(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = await self.signed_in()
        # no daily digest: it would count the reminders below (the digest has its own test)
        await self.client.patch("/api/settings", json={"timezone": "Asia/Beirut", "notificationPrefs": {"dailyDigestTime": None}})
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    async def reminder(self, **body):
        response = await self.client.post("/api/reminders", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_a_repeating_reminder_fires_once_and_moves_on(self):
        pills = await self.reminder(title="Pills", remindAt=iso(self.now - timedelta(minutes=5)), rrule="FREQ=DAILY")
        first = await self.client.post("/api/notifications/sync")
        self.assertEqual(first.json(), {"ok": True, "created": 1})
        row = await self.db.reminder.find_unique(where={"id": pills["id"]})
        self.assertEqual(row.remindAt, self.now - timedelta(minutes=5) + timedelta(days=1))
        self.assertIsNotNone(row.lastFiredAt)
        self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 0)
        listed = (await self.client.get("/api/notifications?noSync=true")).json()
        self.assertEqual([(n["title"], n["entityType"], n["link"]) for n in listed["items"]], [("Pills", "REMINDER", f"/reminders?id={pills['id']}")])
        self.assertTrue(await self.db.activitylog.count(where={"userId": self.client.user.id, "action": "reminder.fired"}))

    async def test_snooze_and_in_app_off(self):
        later = await self.reminder(title="Call back", remindAt=iso(self.now - timedelta(hours=2)))
        await self.client.post("/api/notifications/sync")
        await self.db.reminder.update(where={"id": later["id"]}, data={"snoozedUntil": self.now - timedelta(minutes=1)})
        await self.client.patch("/api/settings", json={"notificationPrefs": {"inApp": False, "dailyDigestTime": None}})
        self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 1)
        rows = await self.db.notification.find_many(where={"userId": self.client.user.id}, order={"createdAt": "asc"})
        self.assertEqual([r.dedupeKey for r in rows], [f"reminder:{later['id']}:{iso(self.now - timedelta(hours=2))}", f"reminder:{later['id']}:snooze:{iso(self.now - timedelta(minutes=1))}"])
        self.assertIsNone(rows[0].readAt)
        self.assertIsNotNone(rows[1].readAt)  # in-app off: stored read (no badge), still pushed
        self.assertIsNone((await self.db.reminder.find_unique(where={"id": later["id"]})).snoozedUntil)

    async def test_push_sends_the_new_ones_and_a_summary_past_four(self):
        from app.os import push

        for i in range(6):
            await self.reminder(title=f"R{i}", remindAt=iso(self.now - timedelta(minutes=10 + i)))
        sender = AsyncMock(return_value=1)
        with patch.object(push, "is_enabled", return_value=True), patch.object(push, "send_to_user", sender):
            self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 6)
        payloads = sender.await_args.args[1]
        self.assertEqual(len(payloads), 4)
        self.assertEqual((payloads[3]["tag"], payloads[3]["link"], payloads[3]["title"]), ("life-os-summary", "/dashboard", "3 more notifications"))
        self.assertEqual(await self.db.notification.count(where={"userId": self.client.user.id, "pushedAt": {"not": None}}), 6)

    async def test_the_list_syncs_unless_asked_not_to(self):
        from app.os import notification_sync

        notification_sync._last_timed.clear()
        notification_sync._last_full.clear()
        await self.reminder(title="Water", remindAt=iso(self.now - timedelta(minutes=1)))
        self.assertEqual((await self.client.get("/api/notifications?noSync=true")).json()["total"], 0)
        self.assertEqual((await self.client.get("/api/notifications/unread-count")).json(), {"count": 1})

    async def test_the_digest_once_a_day_after_its_time(self):
        await self.client.patch("/api/settings", json={"notificationPrefs": {"dailyDigestTime": "00:00"}})
        self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 0)  # an empty day: none
        await self.client.post("/api/tasks", json={"title": "Overdue", "dueAt": iso(self.now - timedelta(days=3))})
        self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 1)
        self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 0)
        row = await self.db.notification.find_first(where={"userId": self.client.user.id})
        self.assertEqual((row.title, row.body, row.link), ("Your day at a glance", "1 overdue task", "/dashboard"))

    async def test_a_free_account_cannot_sync(self):
        free = await self.signed_in(await self.make_user(plan="free"))
        self.assertEqual((await free.post("/api/notifications/sync")).status_code, 403)

    async def test_fired_reminders_never_crowd_out_a_new_one(self):
        """Fired one-off reminders nobody ticked off stay; the 500-row page only reads due ones."""
        user_id, past = self.client.user.id, self.now - timedelta(days=3)
        await self.db.reminder.create_many(data=[{"userId": user_id, "title": f"Old {i}", "remindAt": past, "lastFiredAt": past + timedelta(minutes=1)}
                                                 for i in range(520)])
        await self.reminder(title="New", remindAt=iso(self.now - timedelta(minutes=1)))
        from app.os import notification_sync

        self.assertIn(user_id, await notification_sync.timed_candidate_users(self.now))
        self.assertEqual((await self.client.post("/api/notifications/sync")).json()["created"], 1)
        self.assertEqual([n.title for n in await self.db.notification.find_many(where={"userId": user_id})], ["New"])


if __name__ == "__main__":
    unittest.main()
