"""Notifications never touch another user's rows; sync waits for the jobs phase."""

import unittest

from vournal_os.support import OsTestCase


class Notifications(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.notification.create(data={"userId": other.id, "title": "Theirs", "dedupeKey": "x"})
        for method, suffix in (("POST", "/read"), ("POST", "/unread"), ("DELETE", "")):
            response = await client.request(method, f"/api/notifications/{theirs.id}{suffix}")
            self.assertEqual(response.status_code, 404, f"{method} {suffix}")
            self.assertEqual(response.json()["message"], "Notification not found")
        await client.post("/api/notifications/read-all")
        await client.delete("/api/notifications")
        row = await self.db.notification.find_unique(where={"id": theirs.id})
        self.assertIsNone(row.readAt)

    async def test_sync_is_not_ported_yet(self):
        client = await self.signed_in()
        self.assertEqual((await client.post("/api/notifications/sync")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
