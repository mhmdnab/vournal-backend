"""Reminders never touch, or link to, another user's rows."""

import unittest

from vournal_os.support import OsTestCase


class Reminders(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.reminder.create(data={"userId": other.id, "title": "Theirs", "remindAt": "2026-11-05T10:00:00Z"})
        for method, suffix, body in (
            ("GET", "", None),
            ("PATCH", "", {"title": "Mine"}),
            ("DELETE", "", None),
            ("POST", "/done", None),
            ("POST", "/snooze", {"minutes": 10}),
        ):
            response = await client.request(method, f"/api/reminders/{theirs.id}{suffix}", json=body)
            self.assertEqual(response.status_code, 404, f"{method} {suffix}")
            self.assertEqual(response.json()["message"], "Reminder not found")
        row = await self.db.reminder.find_unique(where={"id": theirs.id})
        self.assertEqual((row.title, row.isDone, row.snoozedUntil), ("Theirs", False, None))

    async def test_cannot_link_another_users_task(self):
        client = await self.signed_in()
        other = await self.make_user()
        task = await self.db.task.create(data={"userId": other.id, "title": "Their task"})
        response = await client.post("/api/reminders", json={"title": "x", "entityType": "TASK", "entityId": task.id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "Task not found")
        half = await client.post("/api/reminders", json={"title": "x", "entityType": "TASK"})
        self.assertEqual(half.status_code, 400)
        self.assertEqual(half.json()["message"], "entityType and entityId must be provided together")


if __name__ == "__main__":
    unittest.main()
