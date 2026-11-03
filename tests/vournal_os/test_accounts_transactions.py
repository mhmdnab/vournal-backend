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

    async def test_balances_and_list_totals_are_summed_in_the_database(self):
        """The iOS app shares this server: balances and list totals must not load every
        transaction row (NestJS used groupBy), and still come out exact."""
        client = await self.signed_in()
        await client.put("/api/exchange-rates", json={"rates": {"LBP": 89500}})
        usd = (await client.post("/api/accounts", json={"name": "Wallet", "initialBalance": 100})).json()
        lbp = (await client.post("/api/accounts", json={"name": "Lira", "currency": "LBP"})).json()
        for body in (
            {"type": "INCOME", "amount": 50.25, "accountId": usd["id"], "date": "2026-11-01"},
            {"type": "EXPENSE", "amount": 895000, "currency": "LBP", "accountId": usd["id"], "date": "2026-11-02"},
            {"type": "TRANSFER", "amount": 20, "accountId": usd["id"], "toAccountId": lbp["id"], "toAmount": 1790000, "date": "2026-11-03"},
            {"type": "TRANSFER", "amount": 5, "accountId": usd["id"], "toAccountId": lbp["id"], "date": "2026-11-04"},
        ):
            self.assertEqual((await client.post("/api/transactions", json=body)).status_code, 201)

        from unittest.mock import patch

        from app.db import db

        actions = type(db.transaction)
        real = actions.find_many
        unpaged: list[dict] = []

        async def paged_only(self, *args, **kwargs):
            where = kwargs.get("where") or {}
            if kwargs.get("take") is None and "rrule" not in where:  # repeating-series heads are few
                unpaged.append(where)
            return await real(self, *args, **kwargs)

        with patch.object(actions, "find_many", paged_only):
            accounts = (await client.get("/api/accounts")).json()
            listed = await client.get("/api/transactions?pageSize=2")
        self.assertEqual(unpaged, [], "loaded every transaction row")
        by_name = {a["name"]: a for a in accounts["items"]}
        # 100 + 50.25 − 10 (895000 LBP) − 20 − 5
        self.assertEqual((by_name["Wallet"]["balance"], by_name["Wallet"]["transactionCount"]), (115.25, 4))
        self.assertEqual((by_name["Lira"]["balance"], by_name["Lira"]["transactionCount"]), (1790000 + 447500, 2))
        self.assertEqual(by_name["Wallet"]["lastTransactionAt"][:10], "2026-11-04")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["totals"], {"currency": "USD", "income": 50.25, "expenses": 10, "transfers": 25, "net": 40.25})

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
