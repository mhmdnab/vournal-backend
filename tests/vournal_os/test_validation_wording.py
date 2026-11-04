"""400s read like NestJS's ValidationPipe: class-validator's wording, parent paths on
nested properties, and unknown query parameters refused on routes with a query DTO."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase

STRICT_ROUTES = (
    "/api/categories", "/api/tasks", "/api/tasks/focus", "/api/events", "/api/reminders", "/api/notifications",
    "/api/calendar/feed?from=2026-11-01&to=2026-11-30", "/api/calendar/summary?from=2026-11-01&to=2026-11-30",
    "/api/accounts", "/api/transactions", "/api/budgets", "/api/recurring-payments", "/api/recurring-payments/summary",
    "/api/debts", "/api/finance/summary", "/api/finance/forecast",
)


class Wording(OsTestCase):
    async def test_body_messages_are_class_validator_words(self):
        client = await self.signed_in()
        messages = (await client.post("/api/debts", json={})).json()["message"]
        self.assertIn("direction must be one of the following values: I_OWE, OWED_TO_ME", messages)
        self.assertIn("title must be a string", messages)
        self.assertIn("principal must be a number conforming to the specified constraints", messages)
        self.assertFalse([m for m in messages if "Value error" in m or "Field required" in m], messages)
        long = (await client.post("/api/debts", json={"direction": "I_OWE", "title": "x" * 201, "principal": 5, "counterparty": "a"})).json()
        self.assertEqual(long["message"], ["title must be shorter than or equal to 200 characters"])

    async def test_nested_messages_carry_the_parent_path(self):
        client = await self.signed_in()
        base = {"title": "Pay", "amount": 10, "alreadyReceived": True}
        day = (await client.post("/api/finance/income", json={**base, "schedule": {"type": "monthly", "day": 99}})).json()
        self.assertEqual(day["message"], ["schedule.day must not be greater than 28"])
        extra = (await client.post("/api/finance/income", json={**base, "schedule": {"type": "monthly", "day": 1, "x": 1}})).json()
        self.assertEqual(extra["message"], ["schedule.property x should not exist"])
        kind = (await client.post("/api/finance/income", json={**base, "schedule": {"type": "yearly"}})).json()
        self.assertEqual(kind["message"], ["schedule.type must be one of the following values: monthly, weekly, daily"])
        absent = (await client.post("/api/finance/income", json=base)).json()
        self.assertEqual(absent["message"], ["nested property schedule must be either object or array"])

    async def test_unknown_query_parameters_are_refused(self):
        client = await self.signed_in()
        for path in STRICT_ROUTES:
            with self.subTest(path=path):
                response = await client.get(path + ("&" if "?" in path else "?") + "bogus=1")
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["message"][0], "property bogus should not exist")
                self.assertEqual((await client.get(path)).status_code, 200, path)

    async def test_transactions_list_checks_everything_before_writing(self):
        client = await self.signed_in()
        past = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT10:00:00.000Z")
        made = await client.post("/api/transactions", json={"type": "EXPENSE", "amount": 5, "date": past, "rrule": "FREQ=DAILY"})
        self.assertEqual(made.status_code, 201, made.text)
        before = await self.db.transaction.count(where={"userId": client.user.id})
        response = await client.get("/api/transactions?type=WRONG&page=0")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(response.json()["message"]), 2, response.json())
        self.assertEqual(await self.db.transaction.count(where={"userId": client.user.id}), before)

    async def test_a_field_can_fail_several_rules_at_once(self):
        client = await self.signed_in()
        made = (await client.post("/api/reminders", json={"title": "Call", "remindAt": "2027-01-20T09:00:00.000Z"})).json()
        response = await client.post(f"/api/reminders/{made['id']}/snooze", json={"minutes": "abc"})
        self.assertEqual(response.json()["message"], [
            "minutes must not be greater than 43200", "minutes must not be less than 1", "minutes must be an integer number",
        ])

    async def test_reminder_entity_type_lists_its_values(self):
        from app.os.common.ownership import ENTITY_TYPE_MODEL
        from app.os.reminders import ENTITY_TYPES

        # Prisma's EntityType in schema order (class-validator's IsEnum lists them so)
        self.assertEqual(ENTITY_TYPES, (
            "TASK", "EVENT", "PROJECT", "TRANSACTION", "RECURRING_PAYMENT", "BUDGET", "GOAL", "HABIT", "MAINTENANCE", "DOCUMENT",
            "SHOPPING_LIST", "SHOPPING_ITEM", "MEAL", "RECIPE", "NOTE", "COURSE", "ASSESSMENT", "TRIP", "PERSON", "IMPORTANT_DATE",
            "REMINDER", "DEBT",
        ))
        self.assertEqual(set(ENTITY_TYPES), set(ENTITY_TYPE_MODEL))
        client = await self.signed_in()
        message = (await client.get("/api/reminders?entityType=PLANET")).json()["message"]
        self.assertEqual(message, [f"entityType must be one of the following values: {', '.join(ENTITY_TYPES)}"])


if __name__ == "__main__":
    unittest.main()
