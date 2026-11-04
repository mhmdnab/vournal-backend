"""Reports answer like NestJS did: this week, last week, this and last month, this year,
an empty account, Arabic, sections switched off, and bad periods. Beirut-time account."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates
from vournal_os.parity.seed import SeedMixin, at, day, last_month


class ReportsParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def reports(self):
        for path in ("/reports/weekly", f"/reports/weekly?date={day(-7)}", f"/reports/weekly?date={day(14)}", "/reports/monthly",
                     f"/reports/monthly?month={last_month(1)[:7]}", "/reports/yearly", "/reports/yearly?year=2025"):
            with self.subTest(path=path):
                self.assertParity(*await self.both("GET", path))

    async def test_empty_and_bad_periods(self):
        await self.reports()
        for path in ("/reports/weekly?date=2026-1-5", "/reports/weekly?date=2026-02-30", "/reports/monthly?month=2026-13", "/reports/yearly?year=1899",
                     "/reports/yearly?year=26", "/reports/weekly?bogus=1"):
            with self.subTest(path=path):
                nest, py = await self.both("GET", path)
                self.assertParity(nest, py)
                if nest.status_code == 400:
                    self.assertEqual(nest.json()["message"], py.json()["message"])

    async def test_seeded_account(self):
        await self.seed_life()
        # deadlines on one day sort by priority (the seed's Wireframes is MEDIUM on day 2)
        # (distinct times: the pending list sorts by due time only, so a tie has no fixed order)
        for body in ({"title": "Low one", "dueAt": at(day(2), 9), "priority": "LOW"}, {"title": "Urgent one", "dueAt": at(day(2), 10), "priority": "URGENT"}):
            await self.on_each("POST", "/tasks", body)
        await self.reports()
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar", "weekStartsOn": 6}))
        await self.reports()
        self.assertParity(*await self.both("PATCH", "/settings", {"enabledSections": ["tasks", "calendar"]}))
        await self.reports()


if __name__ == "__main__":
    unittest.main()
