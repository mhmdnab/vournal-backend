"""Recurring payments never touch another user's rows or accounts."""

import unittest

from vournal_os.support import OsTestCase


class RecurringPayments(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.recurringpayment.create(
            data={"userId": other.id, "name": "Theirs", "amount": "10", "currency": "USD", "rrule": "FREQ=MONTHLY", "startDate": "2026-11-01T00:00:00Z", "nextDate": "2026-12-01T00:00:00Z"}
        )
        for method, suffix, body in (
            ("GET", "", None), ("PATCH", "", {"name": "Mine"}), ("DELETE", "", None), ("POST", "/pay", {}),
            ("POST", "/skip", None), ("POST", "/cancel", None), ("POST", "/pause", None), ("POST", "/reactivate", None),
        ):
            response = await client.request(method, f"/api/recurring-payments/{theirs.id}{suffix}", json=body)
            self.assertEqual(response.status_code, 404, f"{method} {suffix}")
            self.assertEqual(response.json()["message"], "Recurring payment not found")
        their_account = await self.db.account.create(data={"userId": other.id, "name": "Theirs"})
        response = await client.post("/api/recurring-payments", json={"name": "x", "amount": 5, "accountId": their_account.id})
        self.assertEqual(response.json()["message"], "Account not found")
        row = await self.db.recurringpayment.find_unique(where={"id": theirs.id})
        self.assertEqual((row.name, row.status), ("Theirs", "ACTIVE"))


if __name__ == "__main__":
    unittest.main()
