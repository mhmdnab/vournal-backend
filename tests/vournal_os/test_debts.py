"""Debts never touch another user's rows, and follow their linked transactions."""

import unittest
from decimal import Decimal

from vournal_os.support import OsTestCase


class Debts(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.debt.create(
            data={"userId": other.id, "direction": "I_OWE", "title": "Theirs", "principal": "50", "currency": "USD", "startDate": "2026-11-01T00:00:00Z", "counterparty": "Bank"}
        )
        for method, suffix, body in (
            ("GET", "", None), ("PATCH", "", {"title": "Mine"}), ("DELETE", "", None),
            ("POST", "/payments", {"amount": 5}), ("POST", "/settle", {}), ("POST", "/status", {"status": "CANCELLED"}),
        ):
            response = await client.request(method, f"/api/debts/{theirs.id}{suffix}", json=body)
            self.assertEqual(response.status_code, 404, f"{method} {suffix}")
            self.assertEqual(response.json()["message"], "Debt not found")
        row = await self.db.debt.find_unique(where={"id": theirs.id})
        self.assertEqual((row.title, row.status), ("Theirs", "OPEN"))

    async def test_deleting_the_payment_transaction_reopens_a_settled_debt(self):
        client = await self.signed_in()
        debt = (await client.post("/api/debts", json={"direction": "I_OWE", "kind": "LOAN", "title": "Car", "principal": 100})).json()
        settled = (await client.post(f"/api/debts/{debt['id']}/settle", json={})).json()
        self.assertEqual(settled["status"], "SETTLED")
        payment = await self.db.debtpayment.find_first(where={"debtId": debt["id"]})
        self.assertIsNotNone(payment.transactionId)  # a LOAN records its transactions
        response = await client.delete(f"/api/transactions/{payment.transactionId}")
        self.assertEqual(response.status_code, 200)
        reopened = (await client.get(f"/api/debts/{debt['id']}")).json()
        self.assertEqual((reopened["status"], reopened["remaining"]), ("OPEN", 100))

    async def test_paying_a_linked_installment_records_a_debt_payment(self):
        client = await self.signed_in()
        debt = (
            await client.post(
                "/api/debts",
                json={"direction": "I_OWE", "kind": "LOAN", "title": "Laptop", "principal": 300, "installments": {"amount": 100, "firstDate": "2026-12-01"}},
            )
        ).json()
        plan = debt["recurringPaymentId"]
        paid = await client.post(f"/api/recurring-payments/{plan}/pay", json={})
        self.assertEqual(paid.status_code, 200, paid.text)
        after = (await client.get(f"/api/debts/{debt['id']}")).json()
        self.assertEqual((after["paid"], after["remaining"], after["paymentCount"]), (100, 200, 1))

    async def test_installments_take_the_debts_own_limits(self):
        """NestJS built the plan without the recurring-payment form's rules (name ≤ 100,
        reminder ≤ 60): a long debt title or a far reminder still makes the plan."""
        client = await self.signed_in()
        title = "Car loan from the bank, " * 6  # 144 characters
        response = await client.post(
            "/api/debts",
            json={"direction": "I_OWE", "kind": "LOAN", "title": title, "principal": 1000, "reminderDaysBefore": 90,
                  "installments": {"amount": 100, "firstDate": "2026-12-01"}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        plan = await self.db.recurringpayment.find_unique(where={"id": response.json()["recurringPaymentId"]})
        self.assertEqual((plan.name, plan.reminderDaysBefore), (title.strip(), 90))

    async def test_null_on_a_required_field_is_nest_invalid_data(self):
        client = await self.signed_in()
        debt = (await client.post("/api/debts", json={"direction": "I_OWE", "title": "Omar", "counterparty": "Omar", "principal": 20})).json()
        for field in ("principal", "title", "direction", "kind", "currency"):
            response = await client.patch(f"/api/debts/{debt['id']}", json={field: None})
            self.assertEqual(response.status_code, 400, f"{field}: {response.text}")
            self.assertEqual(response.json(), {"statusCode": 400, "message": "Invalid data", "error": "BAD_REQUEST"})
        row = await self.db.debt.find_unique(where={"id": debt["id"]})
        self.assertEqual((row.title, row.principal), ("Omar", Decimal("20")))


if __name__ == "__main__":
    unittest.main()
