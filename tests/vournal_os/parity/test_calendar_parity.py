"""The calendar feed and summary answer like NestJS did, from tasks, events and
reminders made through the API on both sides, in a Beirut-time account."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates

SEED = [
    ("/tasks", {"title": "Pay rent", "dueAt": "2026-11-05", "priority": "HIGH"}),
    ("/tasks", {"title": "Call bank", "dueAt": "2026-11-05T07:30:00.000Z", "estimatedMinutes": 20}),
    ("/tasks", {"title": "Late thing", "dueAt": "2026-10-31T21:30:00.000Z"}),
    ("/events", {"title": "Gym", "startAt": "2026-10-26T16:00:00.000Z", "endAt": "2026-10-26T17:00:00.000Z", "rrule": "FREQ=WEEKLY;BYDAY=MO,TH"}),
    ("/events", {"title": "Holiday", "startAt": "2026-10-30", "endAt": "2026-11-02"}),
    ("/events", {"title": "Coffee", "startAt": "2026-11-12T08:00:00.000Z"}),
    ("/reminders", {"title": "Pills", "remindAt": "2026-11-01T06:00:00.000Z", "rrule": "FREQ=DAILY;INTERVAL=3"}),
    ("/reminders", {"title": "Renew card", "date": "2026-11-20", "isDeadline": True}),
]


class CalendarParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()  # the feed's subscription costs convert currencies
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        for path, body in SEED:
            self.assertParity(*await self.both("POST", path, body))

    async def test_feed_month(self):
        self.assertParity(*await self.both("GET", "/calendar/feed?from=2026-11-01&to=2026-11-30"))

    async def test_feed_across_month_boundary(self):
        for query in (
            "?from=2026-10-25&to=2026-11-08",
            "?from=2026-10-31T22:00:00.000Z&to=2026-11-02T21:59:59.999Z",
            "?from=2026-10-25&to=2026-11-08&types=event,reminder",
            "?from=2026-10-25&to=2026-11-08&includeCompleted=false",
        ):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/calendar/feed{query}"))

    async def test_summary(self):
        self.assertParity(*await self.both("GET", "/calendar/summary?from=2026-10-01&to=2026-12-31"))

    async def test_feed_with_excluded_and_detached_occurrences(self):
        ids = []
        for client, prefix in ((self.nest, ""), (self.py, "/api")):
            items = (await client.get(f"{prefix}/events?q=gym")).json()["items"]
            ids.append(items[0]["id"])
        for (client, prefix), event_id in zip(((self.nest, ""), (self.py, "/api")), ids):
            await client.post(f"{prefix}/events/{event_id}/exclude", json={"date": "2026-11-02"})
            await client.post(f"{prefix}/events/{event_id}/occurrence", json={"occurrence": "2026-11-05", "title": "Gym (late)", "startAt": "2026-11-05T18:00:00.000Z"})
        self.assertParity(*await self.both("GET", "/calendar/feed?from=2026-11-01&to=2026-11-08"))

    async def test_hidden_section_is_left_out(self):
        self.assertParity(*await self.both("PATCH", "/settings", {"enabledSections": ["calendar", "finance"]}))
        self.assertParity(*await self.both("GET", "/calendar/feed?from=2026-11-01&to=2026-11-30"))

    async def test_feed_with_bills_subscriptions_and_debts(self):
        """The money sources phase 3 could not check: recurring payments and dated debts."""
        for path, body in (
            ("/recurring-payments", {"name": "Rent", "amount": 700, "startDate": "2026-10-31", "nextDate": "2026-10-31"}),
            ("/recurring-payments", {"name": "Netflix", "amount": 12.99, "currency": "EUR", "kind": "SUBSCRIPTION", "rrule": "FREQ=MONTHLY;BYMONTHDAY=15", "startDate": "2026-11-15"}),
            ("/recurring-payments", {"name": "Gym", "amount": 25, "rrule": "FREQ=WEEKLY;BYDAY=MO", "startDate": "2026-11-02", "autoLogExpense": False}),
            ("/debts", {"direction": "I_OWE", "title": "Omar", "counterparty": "Omar", "principal": 150, "dueDate": "2026-11-20"}),
            ("/debts", {"direction": "OWED_TO_ME", "title": "Dinner", "counterparty": "Rania", "principal": 45.5, "dueDate": "2026-11-01"}),
            ("/debts", {"direction": "I_OWE", "kind": "LOAN", "title": "Car", "principal": 1000, "dueDate": "2027-01-10"}),
        ):
            self.assertParity(*await self.both("POST", path, body))
        for query in ("?from=2026-11-01&to=2026-11-30", "?from=2026-10-25&to=2026-12-20&types=bill,subscription,debt",
                      "?from=2026-11-01&to=2026-11-30&types=subscription"):
            with self.subTest(query=query):
                nest, py = await self.both("GET", f"/calendar/feed{query}")
                self.assertParity(nest, py)
        # the last month-wide feed really carries the money sources
        nest, _ = await self.both("GET", "/calendar/feed?from=2026-11-01&to=2026-11-30")
        self.assertEqual({i["sourceType"] for i in nest.json()["items"]} & {"bill", "subscription", "debt"}, {"bill", "subscription", "debt"})
        self.assertParity(*await self.both("GET", "/calendar/summary?from=2026-10-01&to=2027-01-31"))

    async def test_feed_with_document_expiries(self):
        """Document expiry items, which phase 3 could only check as empty."""
        for body in ({"name": "Passport", "type": "PASSPORT", "expiryDate": "2026-11-18", "documentNumber": "P1"},
                     {"name": "Lease", "type": "CONTRACT", "expiryDate": "2026-12-01"}, {"name": "Will"}):
            self.assertParity(*await self.both("POST", "/documents", body))
        for query in ("?from=2026-11-01&to=2026-12-31", "?from=2026-11-01&to=2026-12-31&types=document"):
            with self.subTest(query=query):
                nest, py = await self.both("GET", f"/calendar/feed{query}")
                self.assertParity(nest, py)
        self.assertEqual(sum(i["sourceType"] == "document" for i in nest.json()["items"]), 2)

    async def test_validation_shapes(self):
        for query in ("", "?from=2026-11-01", "?from=x&to=y", "?from=2026-11-01&to=2026-11-30&types=party",
                      "?from=2026-11-01&to=2026-11-30&includeCompleted=perhaps", "?from=2026-01-01&to=2027-03-01",
                      "?from=2026-11-10&to=2026-11-01"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/calendar/feed{query}"))


if __name__ == "__main__":
    unittest.main()
