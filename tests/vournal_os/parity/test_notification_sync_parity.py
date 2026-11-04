"""Making notifications answers like NestJS did: the same seeded life in Beirut makes the
same notifications (titles, bodies, links, entities) in English and in Arabic, a second
sync makes none, and with in-app off they are stored read."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates, normalize
from vournal_os.parity.seed import SeedMixin, at, day


def ago(minutes: int) -> str:
    moment = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=minutes)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class NotificationSyncParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def seed(self):
        await self.seed_life()
        await self.on_each("POST", "/reminders", {"title": "Pills", "remindAt": ago(5), "rrule": "FREQ=DAILY"})
        await self.on_each("POST", "/reminders", {"title": "Call back", "remindAt": ago(90)})
        await self.on_each("POST", "/reminders", {"title": "Later", "remindAt": at(day(2), 9)})
        await self.on_each("POST", "/tasks", {"title": "Send invoice", "dueAt": at(day(1), 12), "reminderAt": ago(2)})

    async def listed(self):
        """Both lists, ordered by title and body: rows made in the same millisecond have no order."""
        nest, py = await self.both("GET", "/notifications?noSync=true&pageSize=100")
        self.assertEqual((nest.status_code, py.status_code), (200, 200))
        rows = [sorted(r.json()["items"], key=lambda n: (n["title"], n["body"] or "")) for r in (nest, py)]
        return rows

    async def sync(self):
        nest, py = await self.both("POST", "/notifications/sync")
        self.assertParity(nest, py)
        return nest.json()["created"]

    async def check_lists(self):
        nest, py = await self.listed()
        self.assertEqual(normalize(nest), normalize(py))
        return nest

    async def test_english_then_nothing_new(self):
        await self.seed()
        self.assertGreater(await self.sync(), 5)
        rows = await self.check_lists()
        self.assertIn("Pills", [n["title"] for n in rows])
        self.assertEqual(await self.sync(), 0)
        self.assertParity(*await self.both("GET", "/notifications/unread-count"))

    async def test_arabic_with_in_app_off(self):
        await self.seed()  # by the English category names, so before switching language
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar", "notificationPrefs": {"inApp": False}}))
        self.assertGreater(await self.sync(), 5)
        rows = await self.check_lists()
        self.assertTrue(all(n["readAt"] for n in rows))
        self.assertParity(*await self.both("GET", "/notifications/unread-count"))


if __name__ == "__main__":
    unittest.main()
