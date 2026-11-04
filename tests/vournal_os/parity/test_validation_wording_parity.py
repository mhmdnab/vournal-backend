"""400 messages, unmasked, for the cases whose wording now matches class-validator's
word for word: unknown query parameters, limits, choices and nested paths."""

import unittest

from vournal_os.parity.harness import ParityCase

QUERIES = ("/tasks?bogus=1", "/tasks/focus?bogus=1&date=2026-11-05", "/transactions?bogus=1&page=2", "/debts?x=1&y=2",
           "/finance/forecast?bogus=1", "/categories?bogus=1", "/reminders?entityType=PLANET",
           "/transactions?sort=sideways&page=0", "/transactions?bogus=1&sort=sideways&pageSize=0",
           "/transactions?page=abc", "/transactions?pageSize=abc", "/transactions?page=1.5", "/notifications?page=-3",
           "/recurring-payments/summary?days=0", "/recurring-payments/summary?days=abc", "/finance/forecast?days=abc", "/finance/forecast?days=3")
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

    async def test_wellness_rule_messages(self):
        """Bodies checked rule by rule: a wrong or missing value gets every message."""
        for method, path, body in (
            ("POST", "/wellness/foods", {}),
            ("POST", "/wellness/foods", {"name": 7, "calories": "1", "protein": 1.25, "fat": -1, "isFavorite": "yes"}),
            ("POST", "/wellness/foods", {"name": ["a"], "calories": 1}),
            ("POST", "/wellness/water", {"addMl": "10"}),
            ("POST", "/wellness/water", {"addMl": 1.5, "date": 5}),
            ("POST", "/wellness/exercise", {}),
            ("POST", "/wellness/food-logs", {"servings": 0.123, "meal": "BRUNCH", "source": "x", "catalogId": 1, "name": 5}),
            ("PUT", "/wellness/profile", {"logReminderTime": "8:30", "sex": "OTHER", "heightCm": 99.55, "trackFat": 1, "bogus": True}),
            ("PUT", "/wellness/profile", {"weeklyRateKg": 0.05, "calorieTargetOverride": 799.5}),
            ("PUT", "/wellness/weight", {"weightKg": 80, "note": 5}),
            ("PUT", "/wellness/weight", {}),
            ("GET", "/wellness/foods/search?q=a&q=b", None),
            ("GET", "/wellness/history?days=3", None),
        ):
            with self.subTest(path=path, body=body):
                nest, py = await self.both(method, path, body)
                self.assertEqual((nest.status_code, nest.json()), (py.status_code, py.json()))

    async def test_body_messages(self):
        for path, body in BODIES:
            with self.subTest(path=path, body=body):
                nest, py = await self.both("POST", path, body)
                self.assertEqual((nest.status_code, nest.json()), (py.status_code, py.json()))


if __name__ == "__main__":
    unittest.main()
