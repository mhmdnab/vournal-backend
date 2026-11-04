"""400 messages, unmasked, for the cases whose wording now matches class-validator's
word for word: unknown query parameters, limits, choices and nested paths."""

import unittest

from vournal_os.parity.harness import ParityCase

QUERIES = ("/tasks?bogus=1", "/tasks/focus?bogus=1&date=2026-11-05", "/transactions?bogus=1&page=2", "/debts?x=1&y=2",
           "/finance/forecast?bogus=1", "/categories?bogus=1", "/reminders?entityType=PLANET")
BODIES = (
    ("/debts", {"direction": "I_OWE", "title": "x" * 201, "principal": 5, "counterparty": "a"}),
    ("/debts", {"direction": "SIDEWAYS", "title": "x", "principal": 5, "counterparty": "a"}),
    ("/debts", {"direction": "I_OWE", "title": "x", "principal": 5, "counterparty": "a", "reminderDaysBefore": 400}),
    ("/finance/income", {"title": "Pay", "amount": 10, "alreadyReceived": True, "schedule": {"type": "monthly", "day": 99}}),
    ("/finance/income", {"title": "Pay", "amount": 10, "alreadyReceived": True, "schedule": {"type": "monthly", "day": 1, "x": 1}}),
    ("/finance/income", {"title": "Pay", "amount": 10, "alreadyReceived": True, "schedule": {"type": "yearly"}}),
)


class WordingParity(ParityCase):
    maxDiff = None

    async def test_query_messages(self):
        for path in QUERIES:
            with self.subTest(path=path):
                nest, py = await self.both("GET", path)
                self.assertEqual((nest.status_code, nest.json()), (py.status_code, py.json()))

    async def test_trailing_slash_matches(self):
        """Express's non-strict routing: "/tasks/" is "/tasks"."""
        for method, path, body in (("GET", "/tasks/", None), ("POST", "/tags/", {"name": "slashed"}), ("GET", "/debts/summary/", None)):
            with self.subTest(path=path):
                self.assertParity(*await self.both(method, path, body))

    async def test_body_messages(self):
        for path, body in BODIES:
            with self.subTest(path=path, body=body):
                nest, py = await self.both("POST", path, body)
                self.assertEqual((nest.status_code, nest.json()), (py.status_code, py.json()))


if __name__ == "__main__":
    unittest.main()
