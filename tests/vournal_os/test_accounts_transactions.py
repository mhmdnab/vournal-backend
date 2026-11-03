"""Accounts and transactions never touch, or move money into, another user's rows."""

import unittest
from decimal import Decimal

from vournal_os.support import OsTestCase


class Money(OsTestCase):
    async def test_other_users_accounts_and_transactions_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        account = await self.db.account.create(data={"userId": other.id, "name": "Theirs"})
        tx = await self.db.transaction.create(
            data={"userId": other.id, "type": "EXPENSE", "amount": Decimal("5"), "currency": "USD", "date": "2026-11-03T10:00:00Z"}
        )
        for method, path, body in (
            ("GET", f"/api/accounts/{account.id}", None),
            ("PATCH", f"/api/accounts/{account.id}", {"name": "Mine"}),
            ("POST", f"/api/accounts/{account.id}/archive", None),
            ("DELETE", f"/api/accounts/{account.id}", None),
            ("GET", f"/api/transactions/{tx.id}", None),
            ("PATCH", f"/api/transactions/{tx.id}", {"amount": 1}),
            ("DELETE", f"/api/transactions/{tx.id}", None),
        ):
            response = await client.request(method, path, json=body)
            self.assertEqual(response.status_code, 404, f"{method} {path}")
        mine = (await client.post("/api/accounts", json={"name": "Mine"})).json()
        into_theirs = await client.post(
            "/api/transactions", json={"type": "TRANSFER", "amount": 10, "accountId": mine["id"], "toAccountId": account.id}
        )
        self.assertEqual(into_theirs.status_code, 404)
        self.assertEqual(into_theirs.json()["message"], "Account not found")
        self.assertEqual((await self.db.account.find_unique(where={"id": account.id})).name, "Theirs")
        self.assertEqual((await self.db.transaction.find_unique(where={"id": tx.id})).amount, Decimal("5"))

    async def test_import_keeps_only_own_categories(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.category.create(data={"userId": other.id, "scope": "EXPENSE", "name": "Theirs"})
        response = await client.post(
            "/api/transactions/import", json={"rows": [{"type": "EXPENSE", "amount": 3, "date": "2026-11-01", "categoryId": theirs.id}]}
        )
        self.assertEqual(response.status_code, 201, response.text)
        stored = await self.db.transaction.find_first(where={"userId": client.user.id})
        self.assertIsNone(stored.categoryId)


if __name__ == "__main__":
    unittest.main()
