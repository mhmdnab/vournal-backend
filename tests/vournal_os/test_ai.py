"""Smart capture and the food estimate: NestJS's own normalizers (goldens), the statuses
(no key, AI off, allowance used, a refusal, a provider error), one monthly allowance shared
by every AI feature and counted in the user's own month, and a burst that never runs past it."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from vournal_os.support import OsTestCase

GOLDEN = json.loads((Path(__file__).parent / "golden" / "ai.json").read_text())
BLANK = {"kind": "unknown", "title": "", "amount": None, "currency": None, "date": None, "time": None, "durationMinutes": None, "category": None,
         "account": None, "list": None, "quantity": None, "priority": None, "note": None, "debtDirection": None, "person": None}


class FakeModel:
    """A stand-in for AsyncAnthropic: records each request, answers from a script."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **request):
        self.requests.append(request)
        await asyncio.sleep(0.05)
        reply = self.replies.pop(0) if self.replies else {"text": json.dumps(BLANK)}
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(stop_reason=reply.get("stop", "end_turn"), content=[SimpleNamespace(type="text", text=reply["text"])])


def drafted(**fields) -> dict:
    return {"text": json.dumps({**BLANK, **fields})}


class Goldens(unittest.TestCase):
    def test_normalize_like_nestjs(self):
        from app.os import ai

        for case in GOLDEN["capture"]:
            with self.subTest(raw=case["raw"]):
                self.assertEqual(ai.normalize_capture(case["raw"], GOLDEN["lookups"]), case["out"])
        for case in GOLDEN["people"]:
            found = ai.match_person(GOLDEN["lookups"]["people"], case["name"])
            self.assertEqual(found["id"] if found else None, case["out"], case["name"])
        for case in GOLDEN["estimate"]:
            self.assertEqual(ai.normalize_estimate(case["raw"]), case["out"])
        for case in GOLDEN["userMessage"]:
            self.assertEqual(ai.capture_user_message(case["text"], case["ctx"]), case["out"])


class Capture(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import ai

        self.ai = ai
        self.client = await self.signed_in()
        await self.client.patch("/api/settings", json={"timezone": "Asia/Beirut", "aiEnabled": True})

    def model(self, *replies):
        fake = FakeModel(*replies)
        patcher = patch.object(self.ai, "_client", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    async def capture(self, text="lunch 12$"):
        response = await self.client.post("/api/ai/capture", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_without_a_key_or_with_ai_off(self):
        with patch.object(self.ai, "_client", None):
            self.assertEqual((await self.client.get("/api/ai/status")).json(),
                             {"configured": False, "enabled": True, "monthlyLimit": 3000, "remainingThisMonth": 3000})
            self.assertEqual(await self.capture(), {"status": "not_configured", "draft": None, "remainingThisMonth": 3000})
        fake = self.model()
        await self.client.patch("/api/settings", json={"aiEnabled": False})
        self.assertEqual((await self.capture())["status"], "disabled")
        self.assertEqual(fake.requests, [])

    async def test_a_draft_from_the_users_own_names(self):
        fake = self.model(drafted(kind="expense", title="lunch", amount=12, category="food", account="cash"))
        cash = next(a for a in (await self.client.get("/api/accounts")).json()["items"] if a["name"] == "Cash")
        food = next(c for c in (await self.client.get("/api/categories?scope=EXPENSE")).json()["items"] if c["name"] == "Food")
        await self.client.post("/api/people", json={"name": "Sara Khoury"})
        result = await self.capture(" lunch 12$ ")
        self.assertEqual((result["status"], result["remainingThisMonth"]), ("ok", 2999))
        self.assertEqual((result["draft"]["categoryId"], result["draft"]["accountId"], result["draft"]["currency"]), (food["id"], cash["id"], "USD"))
        request = fake.requests[0]
        self.assertEqual((request["model"], request["max_tokens"], request["betas"], request["fallbacks"]),
                         ("claude-sonnet-5-5", 2048, ["server-side-fallback-2026-07-01", "structured-outputs-2025-12-15"], "default"))
        self.assertEqual(request["output_config"], {"effort": "low", "format": self.ai.PROMPTS["captureFormat"]})
        self.assertEqual(request["system"], self.ai.PROMPTS["captureSystem"])
        self.assertIn("<text>\nlunch 12$\n</text>", request["messages"][0]["content"])
        self.assertIn("timezone Asia/Beirut", request["messages"][0]["content"])
        self.assertNotIn("Sara", json.dumps(request))  # people never leave the server

    async def test_refusals_errors_and_nonsense_still_count(self):
        import anthropic

        self.model({"stop": "refusal", "text": json.dumps(BLANK)}, anthropic.APIConnectionError(request=None), {"text": "not json"}, drafted(kind="unknown"))
        statuses = [(await self.capture())["status"] for _ in range(4)]
        self.assertEqual(statuses, ["not_understood", "unavailable", "unavailable", "not_understood"])
        self.assertEqual((await self.client.get("/api/ai/status")).json()["remainingThisMonth"], 2996)

    async def test_values_outside_the_enums_are_unavailable(self):
        """zod's enums (kind, priority, debtDirection) aren't in the wire schema; they're still enforced."""
        self.model(drafted(kind="todo", title="call mom"), drafted(kind="task", title="x", priority="high"),
                   drafted(kind="debt", title="x", amount=5, debtDirection="OWES_ME", person="Rami"), drafted(kind="task", title="ok", priority="HIGH"))
        self.assertEqual([(await self.capture())["status"] for _ in range(4)], ["unavailable", "unavailable", "unavailable", "ok"])

    async def test_the_allowance_is_monthly_in_the_users_time_zone_and_shared(self):
        self.model()
        tz = ZoneInfo("Asia/Beirut")
        local = datetime.now(tz)
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        for action, at in (("ai.chat", month_start - timedelta(minutes=1)), ("ai.chat", month_start + timedelta(minutes=1)), ("ai.food", month_start + timedelta(hours=1))):
            await self.db.activitylog.create(data={"userId": self.client.user.id, "action": action, "createdAt": at})
        with patch.object(self.ai, "MONTHLY_LIMIT", 3):
            self.assertEqual((await self.client.get("/api/ai/status")).json()["remainingThisMonth"], 1)
            self.assertEqual((await self.capture())["remainingThisMonth"], 0)
            self.assertEqual(await self.capture(), {"status": "limit", "draft": None, "remainingThisMonth": 0})
            estimate = await self.client.post("/api/wellness/estimate", json={"text": "manoushe"})
            self.assertEqual(estimate.json(), {"status": "limit", "items": [], "remainingThisMonth": 0})

    async def test_a_burst_never_runs_past_the_allowance(self):
        fake = self.model()
        with patch.object(self.ai, "MONTHLY_LIMIT", 3):
            results = await asyncio.gather(*[self.client.post("/api/ai/capture", json={"text": f"note {i}"}) for i in range(6)])
        statuses = sorted(r.json()["status"] for r in results)
        self.assertEqual(statuses, ["limit"] * 3 + ["not_understood"] * 3)
        self.assertEqual(len(fake.requests), 3)

    async def test_the_food_estimate(self):
        fake = self.model({"text": json.dumps({"items": [{"name": "Manoushe", "servings": 2, "serving": "1 piece", "calories": 560.4,
                                                          "protein": 14, "carbs": 70, "fat": None}]})})
        await self.client.patch("/api/settings", json={"language": "ar"})
        response = await self.client.post("/api/wellness/estimate", json={"text": " 2 man2oushe "})
        self.assertEqual(response.json(), {"status": "ok", "remainingThisMonth": 2999, "items": [
            {"name": "Manoushe", "servings": 2, "serving": "1 piece", "calories": 560, "protein": 14, "carbs": 70, "fat": None}]})
        self.assertEqual(fake.requests[0]["messages"][0]["content"], "<language>ar</language>\n<text>\n2 man2oushe\n</text>")
        self.assertEqual(fake.requests[0]["output_config"], {"effort": "low", "format": self.ai.PROMPTS["foodFormat"]})
        self.assertEqual(await self.db.foodlog.count(where={"userId": self.client.user.id}), 0)  # saves nothing

    async def test_validation_and_plus(self):
        for body in ({}, {"text": "x"}, {"text": "x" * 501}, {"text": 5}, {"text": "ok ok", "extra": 1}):
            with self.subTest(body=body):
                self.assertEqual((await self.client.post("/api/ai/capture", json=body)).status_code, 400)
                self.assertEqual((await self.client.post("/api/wellness/estimate", json=body)).status_code, 400)
        free = await self.signed_in(await self.make_user(plan="free"))
        for method, path in (("GET", "/api/ai/status"), ("POST", "/api/ai/capture"), ("POST", "/api/wellness/estimate")):
            self.assertEqual((await free.request(method, path, json={"text": "lunch 12"})).status_code, 403)


if __name__ == "__main__":
    unittest.main()
