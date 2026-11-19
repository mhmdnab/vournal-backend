"""The assistant's budget and recurring payment tools (design 2026-11-18). Field changes, pause,
cancel and reactivate can be undone; paying and skipping are final (ruling R4)."""

from vournal_os.assistant_life_base import LifeBase


class MoneyTools(LifeBase):
    async def payment(self, **data):
        return await self.post("/api/recurring-payments", {"name": "Internet", "amount": 30, "rrule": "FREQ=MONTHLY", **data})

    async def test_create_budget_default_currency_and_undo(self):
        a = await self.act("create_budget", {"amount": 400, "name": "Groceries", "alert_percent": 90})
        self.assertEqual((a["kind"], a["entityType"], a["label"], a["link"]), ("create", "budget", "Groceries", f"/finance/budgets?id={a['entityId']}"))
        b = await self.db.budget.find_unique(where={"id": a["entityId"]})
        currency = (await self.client.get("/api/settings")).json()["defaultCurrency"]
        self.assertEqual((float(b.amount), b.currency, b.period, b.alertThreshold), (400.0, currency, "MONTHLY", 90))
        await self.undo(a)
        self.assertIsNone(await self.db.budget.find_unique(where={"id": a["entityId"]}))

    async def test_create_budget_same_category_twice_is_the_routes_error(self):
        await self.act("create_budget", {"amount": 400})
        self.assertIn("already", (await self.act_error("create_budget", {"amount": 500})).lower())

    async def test_update_budget_amount_and_turn_off_undo(self):
        b = await self.post("/api/budgets", {"amount": 300, "name": "Groceries"})
        a = await self.act("update_budget", {"id": b["id"], "amount": 400, "active": False})
        self.assertEqual((a["kind"], a["entityType"]), ("update", "budget"))
        row = await self.db.budget.find_unique(where={"id": b["id"]})
        self.assertEqual((float(row.amount), row.isActive), (400.0, False))
        await self.undo(a)
        row = await self.db.budget.find_unique(where={"id": b["id"]})
        self.assertEqual((float(row.amount), row.isActive), (300.0, True))

    async def test_create_recurring_payment_monthly_default(self):
        a = await self.act("create_recurring_payment", {"name": "Netflix", "amount": 12, "kind": "SUBSCRIPTION"})
        self.assertEqual((a["kind"], a["entityType"], a["link"]), ("create", "recurring_payment", f"/finance/recurring?id={a['entityId']}"))
        p = await self.db.recurringpayment.find_unique(where={"id": a["entityId"]})
        self.assertEqual((p.name, float(p.amount), p.kind, p.rrule, p.status), ("Netflix", 12.0, "SUBSCRIPTION", "FREQ=MONTHLY", "ACTIVE"))
        await self.undo(a)
        self.assertIsNone(await self.db.recurringpayment.find_unique(where={"id": a["entityId"]}))

    async def test_create_recurring_payment_yearly(self):
        a = await self.act("create_recurring_payment", {"name": "Insurance", "amount": 300, "repeat": {"every": "year"}, "first_date": "2026-12-01"})
        p = await self.db.recurringpayment.find_unique(where={"id": a["entityId"]})
        self.assertEqual((p.rrule, p.startDate.date().isoformat(), p.kind), ("FREQ=YEARLY", "2026-12-01", "BILL"))

    async def test_update_recurring_payment_fields_and_undo(self):
        p = await self.payment()
        a = await self.act("update_recurring_payment", {"id": p["id"], "amount": 35, "next_date": "2026-12-05"})
        self.assertEqual((a["kind"], a["entityType"]), ("update", "recurring_payment"))
        row = await self.db.recurringpayment.find_unique(where={"id": p["id"]})
        self.assertEqual((float(row.amount), row.nextDate.date().isoformat()), (35.0, "2026-12-05"))
        await self.undo(a)
        row = await self.db.recurringpayment.find_unique(where={"id": p["id"]})
        self.assertEqual((float(row.amount), row.nextDate.date().isoformat()), (30.0, p["nextDate"][:10]))

    async def test_pay_makes_one_expense_moves_next_date_no_undo(self):
        p = await self.payment()
        a = await self.act("update_recurring_payment", {"id": p["id"], "action": "pay"})
        self.assertEqual((a["kind"], a["entityType"], a["canUndo"]), ("complete", "recurring_payment", False))
        self.assertEqual(await self.db.transaction.count(where={"recurringPaymentId": p["id"]}), 1)
        row = await self.db.recurringpayment.find_unique(where={"id": p["id"]})
        self.assertNotEqual(row.nextDate.date().isoformat(), p["nextDate"][:10])

    async def test_pay_with_another_amount(self):
        p = await self.payment()
        await self.act("update_recurring_payment", {"id": p["id"], "action": "pay", "amount": 32.5})
        tx = await self.db.transaction.find_first(where={"recurringPaymentId": p["id"]})
        self.assertEqual(float(tx.amount), 32.5)

    async def test_pay_cancelled_payment_is_refused(self):
        p = await self.payment(status="CANCELLED")
        text = await self.act_error("update_recurring_payment", {"id": p["id"], "action": "pay"})
        self.assertEqual(text, "Error: This payment is cancelled — reactivate it first")
        self.assertEqual(await self.db.transaction.count(where={"recurringPaymentId": p["id"]}), 0)

    async def test_skip_moves_next_date(self):
        p = await self.payment()
        a = await self.act("update_recurring_payment", {"id": p["id"], "action": "skip"})
        self.assertEqual((a["kind"], a["canUndo"]), ("update", False))
        row = await self.db.recurringpayment.find_unique(where={"id": p["id"]})
        self.assertNotEqual(row.nextDate.date().isoformat(), p["nextDate"][:10])
        self.assertEqual(await self.db.transaction.count(where={"recurringPaymentId": p["id"]}), 0)

    async def test_pause_and_undo_reactivates(self):
        p = await self.payment()
        a = await self.act("update_recurring_payment", {"id": p["id"], "action": "pause"})
        self.assertEqual((await self.db.recurringpayment.find_unique(where={"id": p["id"]})).status, "PAUSED")
        await self.undo(a)
        row = await self.db.recurringpayment.find_unique(where={"id": p["id"]})
        self.assertEqual((row.status, row.nextDate.date().isoformat()), ("ACTIVE", p["nextDate"][:10]))

    async def test_cancel_and_undo(self):
        p = await self.payment()
        a = await self.act("update_recurring_payment", {"id": p["id"], "action": "cancel"})
        self.assertIsNotNone((await self.db.recurringpayment.find_unique(where={"id": p["id"]})).cancelledAt)
        await self.undo(a)
        row = await self.db.recurringpayment.find_unique(where={"id": p["id"]})
        self.assertEqual((row.status, row.cancelledAt), ("ACTIVE", None))

    async def test_action_with_fields_is_an_error(self):
        p = await self.payment()
        self.assertIn("not both", await self.act_error("update_recurring_payment", {"id": p["id"], "action": "pause", "name": "X"}))

    async def test_delete_budget_and_recurring_payment(self):
        b = await self.post("/api/budgets", {"amount": 300})
        a = await self.delete("budget", b["id"], "Budget")
        self.assertEqual((a["status"], a["entityType"]), ("done", "budget"))
        await self.undo(a)
        self.assertIsNotNone(await self.db.budget.find_unique(where={"id": b["id"]}))
        p = await self.payment()
        a = await self.delete("recurring_payment", p["id"], "Internet")
        self.assertEqual(a["status"], "done")
        self.assertIsNone(await self.db.recurringpayment.find_unique(where={"id": p["id"]}))

    async def test_finance_section_off_refuses(self):
        await self.client.patch("/api/settings", json={"enabledSections": ["tasks"]})
        self.assertEqual(await self.act_error("create_budget", {"amount": 5}), "Error: The Finance section is turned off in Settings.")
