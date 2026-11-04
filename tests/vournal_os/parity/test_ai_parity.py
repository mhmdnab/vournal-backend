"""Smart capture and the food estimate answer like NestJS did, and send the model exactly
what NestJS sent (prompt, output format, context, betas), with the model played by
fake_model on both sides. The allowance numbers differ by design (NestJS: 100 a day;
here: a monthly allowance), so they're left out of the comparison."""

import unittest
from unittest.mock import patch

from vournal_os.parity import fake_model as fm
from vournal_os.parity.harness import ParityCase

ALLOWANCE = ("remainingToday", "dailyLimit", "remainingThisMonth", "monthlyLimit")  # NestJS: 100 a day; here: monthly, renamed
BLANK = {"kind": "unknown", "title": "", "amount": None, "currency": None, "date": None, "time": None, "durationMinutes": None, "category": None,
         "account": None, "list": None, "quantity": None, "priority": None, "note": None, "debtDirection": None, "person": None}


class AiParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import ai

        fm.start()
        patcher = patch.object(ai, "_client", fm.python_client())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut", "aiEnabled": True}))
        for path, body in (("/people", {"name": "Ahmad Khalil"}), ("/shopping/lists", {"name": "Home"})):
            self.assertParity(*await self.both("POST", path, body), ignore_keys=("mentionCount",))  # Python's people count journal mentions

    async def check(self, method, path, body=None):
        nest, py = await self.both(method, path, body)
        self.assertParity(nest, py, ignore_keys=ALLOWANCE)
        if nest.status_code == 400:
            self.assertEqual(nest.json()["message"], py.json()["message"])
        return nest.json()

    def assert_same_requests(self, count):
        nest, py = fm.requests("nest"), fm.requests("python")
        self.assertEqual(len(nest), count)
        self.assertEqual(nest, py)

    async def test_status_and_ai_off(self):
        await self.check("GET", "/ai/status")
        self.assertParity(*await self.both("PATCH", "/settings", {"aiEnabled": False}))
        fm.script()
        self.assertEqual((await self.check("POST", "/ai/capture", {"text": "lunch 12$"}))["status"], "disabled")
        self.assertEqual((await self.check("POST", "/wellness/estimate", {"text": "manoushe"}))["status"], "disabled")
        self.assert_same_requests(0)

    async def test_every_kind_of_draft(self):
        drafts = [
            {"kind": "expense", "title": "lunch", "amount": 12.5, "category": "food", "account": "CASH", "date": "2026-11-03"},
            {"kind": "income", "title": "Salary", "amount": 2000, "currency": "eur", "category": "Salary"},
            {"kind": "event", "title": "Dentist", "date": "2026-11-05", "time": "9:30", "durationMinutes": 45},
            {"kind": "reminder", "title": "اتصل بأمي", "date": "2026-11-05", "time": "18:00"},
            {"kind": "task", "title": "Pay rent", "priority": "URGENT", "note": "before Friday"},
            {"kind": "shopping", "title": "Milk", "quantity": 2, "list": "home"},
            {"kind": "habit", "title": "Run every morning"},
            {"kind": "note", "title": "Gift idea: a book"},
            {"kind": "debt", "title": "", "amount": 50, "debtDirection": "OWED_TO_ME", "person": "ahmad"},
            {"kind": "debt", "title": "dinner", "amount": 20, "debtDirection": "I_OWE", "person": "Rami"},
            {"kind": "unknown", "title": "hello"},
        ]
        fm.script(*[fm.structured({**BLANK, **d}) for d in drafts])
        for d in drafts:
            with self.subTest(kind=d["kind"]):
                await self.check("POST", "/ai/capture", {"text": f"  {d['title'] or 'ahmad owes me 50'}  "})
        self.assert_same_requests(len(drafts))

    async def test_arabic_and_failures(self):
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar", "defaultCurrency": "LBP"}))
        fm.script(fm.structured({**BLANK, "kind": "expense", "title": "غدا", "amount": 300000}),
                  fm.message(fm.text(""), stop="refusal"),
                  fm.message(fm.text('{"kind": "unknown"}'), stop="refusal"),
                  fm.structured(BLANK) | {"body": {**fm.structured(BLANK)["body"], "stop_reason": "refusal"}},
                  fm.error(400),
                  fm.message(fm.text("not json")),
                  fm.message(fm.text('{"kind": "task"}')))
        statuses = [(await self.check("POST", "/ai/capture", {"text": "ghada 300000 mbere7"}))["status"] for _ in range(7)]
        # an empty or partial refusal fails parsing first (NestJS's SDK parsed before the refusal check)
        self.assertEqual(statuses, ["ok", "unavailable", "unavailable", "not_understood", "unavailable", "unavailable", "unavailable"])
        self.assert_same_requests(7)

    async def test_the_food_estimate(self):
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        fm.script(fm.structured({"items": [{"name": " منقوشة ", "servings": 2, "serving": "1 piece", "calories": 560.4, "protein": 14.26, "carbs": 70, "fat": None},
                                           {"name": "", "servings": 1, "serving": "x", "calories": 5, "protein": None, "carbs": None, "fat": None}]}),
                  fm.structured({"items": []}),
                  fm.message(fm.text(""), stop="refusal"),
                  fm.message(fm.text('{"items": []}'), stop="refusal"))
        for _ in range(4):
            await self.check("POST", "/wellness/estimate", {"text": " 2 man2oushe zaatar "})
        self.assert_same_requests(4)

    async def test_validation(self):
        fm.script()
        for body in ({}, {"text": "x"}, {"text": "x" * 501}, {"text": 5}, {"text": None}, {"text": "ok ok", "extra": 1}, {"text": ["a", "b"]}):
            with self.subTest(body=body):
                await self.check("POST", "/ai/capture", body)
                await self.check("POST", "/wellness/estimate", body)
        self.assert_same_requests(0)


if __name__ == "__main__":
    unittest.main()
