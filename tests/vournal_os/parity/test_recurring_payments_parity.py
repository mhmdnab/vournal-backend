"""Recurring payments answer like NestJS did: cycles, paying, skipping, pausing,
lazy auto-logging and the summary, in a Beirut-time account."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class RecurringParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def make(self, body):
        nest, py = await self.both("POST", "/recurring-payments", body)
        self.assertParity(nest, py)
        return nest.json()["id"], py.json()["id"]

    async def on_both(self, method, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, f"/recurring-payments/{ids[0]}{suffix}", **kwargs),
            await self.py.request(method, f"/api/recurring-payments/{ids[1]}{suffix}", **kwargs),
        )

    async def test_lifecycle(self):
        rent = await self.make({"name": " Rent ", "amount": 700, "startDate": "2026-01-31", "kind": "BILL", "reminderDaysBefore": 5, "url": "landlord.example.com/pay"})
        await self.make({"name": "Netflix", "amount": 12.99, "currency": "EUR", "kind": "SUBSCRIPTION", "rrule": "FREQ=MONTHLY;BYMONTHDAY=15", "startDate": "2026-03-15"})
        await self.make({"name": "Insurance", "amount": 1200, "kind": "INSURANCE", "rrule": "FREQ=YEARLY", "startDate": "2026-12-20"})
        await self.make({"name": "Gym", "amount": 895000, "currency": "LBP", "rrule": "FREQ=WEEKLY;BYDAY=MO", "startDate": "2026-11-02", "nextDate": "2026-11-09"})
        for query in ("", "?kind=SUBSCRIPTION", "?excludeKind=SUBSCRIPTION", "?status=ACTIVE", "?q=net", "?kind=RENT"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/recurring-payments{query}"))
        for query in ("", "?days=90", "?kind=SUBSCRIPTION", "?days=0"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/recurring-payments/summary{query}"))
        self.assertParity(*await self.on_both("POST", rent, "/pay", {"date": "2026-11-01", "amount": 650.5, "notes": "partial"}))
        self.assertParity(*await self.on_both("POST", rent, "/skip"))
        self.assertParity(*await self.on_both("GET", rent))
        self.assertParity(*await self.on_both("POST", rent, "/pause"))
        self.assertParity(*await self.on_both("POST", rent, "/reactivate"))
        self.assertParity(*await self.on_both("POST", rent, "/cancel"))
        self.assertParity(*await self.on_both("POST", rent, "/pay", {}))
        self.assertParity(*await self.on_both("PATCH", rent, json={"status": "ACTIVE", "amount": 750, "notes": None, "rrule": "FREQ=MONTHLY;INTERVAL=2"}))
        self.assertParity(*await self.both("GET", "/transactions"))
        self.assertParity(*await self.both("GET", "/recurring-payments/summary"))
        self.assertParity(*await self.on_both("DELETE", rent))

    async def test_same_day_names_in_locale_order(self):
        """Same-day upcoming payments sort by String#localeCompare: Äpfel, netflix, Netflix, Zebra."""
        day = (datetime.now(timezone.utc) + timedelta(days=20)).date().isoformat()
        for name in ("Netflix", "netflix", "Zebra", "Äpfel", "[x]", "10x", "2x"):
            await self.make({"name": name, "amount": 5, "startDate": day})
        self.assertParity(*await self.both("GET", "/recurring-payments/summary?days=30"))

    async def test_lazy_auto_log(self):
        await self.make({"name": "Phone", "amount": 20, "autoLogExpense": True, "startDate": "2026-08-31", "nextDate": "2026-08-31"})
        self.assertParity(*await self.both("GET", "/recurring-payments"))
        self.assertParity(*await self.both("GET", "/transactions"))

    async def test_validation_shapes(self):
        for body in ({}, {"name": "x"}, {"name": "x", "amount": -1}, {"name": "x", "amount": 1, "kind": "RENT"},
                     {"name": "x", "amount": 1, "rrule": "FREQ=MINUTELY"}, {"name": "x", "amount": 1, "reminderDaysBefore": 61},
                     {"name": "x", "amount": 1, "url": "not a url"}, {"name": "x", "amount": 1, "extra": 1}):
            self.assertParity(*await self.both("POST", "/recurring-payments", body))


if __name__ == "__main__":
    unittest.main()
