"""The calendar shows only the user's own items, plus events their household shares."""

import unittest

from vournal_os.support import OsTestCase


class Calendar(OsTestCase):
    async def test_only_own_and_household_items(self):
        client = await self.signed_in()
        other = await self.make_user()
        await self.db.task.create(data={"userId": other.id, "title": "Their task", "dueAt": "2026-11-10T10:00:00Z", "hasDueTime": True})
        await self.db.event.create(data={"userId": other.id, "title": "Their event", "startAt": "2026-11-10T10:00:00Z"})
        feed = (await client.get("/api/calendar/feed?from=2026-11-01&to=2026-11-30")).json()
        self.assertEqual(feed["total"], 0)

    async def test_range_limits(self):
        client = await self.signed_in()
        too_long = await client.get("/api/calendar/feed?from=2026-01-01&to=2027-03-01")
        self.assertEqual(too_long.status_code, 400)
        self.assertEqual(too_long.json()["message"], "Range too large (max 400 days)")
        backwards = await client.get("/api/calendar/feed?from=2026-11-10&to=2026-11-01")
        self.assertEqual(backwards.json()["message"], '"to" must be after "from"')


if __name__ == "__main__":
    unittest.main()
