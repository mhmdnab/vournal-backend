"""Events never touch another user's rows."""

import unittest

from vournal_os.support import OsTestCase


class Events(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.event.create(
            data={"userId": other.id, "title": "Theirs", "startAt": "2026-11-05T10:00:00Z", "rrule": "FREQ=DAILY"}
        )
        for method, suffix, body in (
            ("GET", "", None),
            ("PATCH", "", {"title": "Mine"}),
            ("DELETE", "", None),
            ("POST", "/exclude", {"date": "2026-11-06"}),
            ("POST", "/occurrence", {"occurrence": "2026-11-06"}),
        ):
            response = await client.request(method, f"/api/events/{theirs.id}{suffix}", json=body)
            self.assertEqual(response.status_code, 404, f"{method} {suffix}")
            self.assertEqual(response.json()["message"], "Event not found")
        row = await self.db.event.find_unique(where={"id": theirs.id})
        self.assertEqual((row.title, row.exDates), ("Theirs", []))
        listed = (await client.get("/api/events?from=2026-11-01&to=2026-11-30&expand=true")).json()
        self.assertEqual(listed["total"], 0)


if __name__ == "__main__":
    unittest.main()
