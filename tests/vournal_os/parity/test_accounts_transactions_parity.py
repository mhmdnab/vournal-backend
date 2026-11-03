"""Accounts and transactions answer like NestJS did: balances across currencies,
transfers, filters, totals, repeating series and statement import."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class MoneyParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        # fixed rates on both sides, so conversions don't depend on the live market
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def make(self, path, body):
        nest, py = await self.both("POST", path, body)
        self.assertParity(nest, py)
        return nest.json()["id"], py.json()["id"]

    async def per_side(self, method, template, ids, json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, template.format(ids[0]), **kwargs),
            await self.py.request(method, "/api" + template.format(ids[1]), **kwargs),
        )

    async def ids_by_name(self, path, name):
        found = []
        for client, prefix in ((self.nest, ""), (self.py, "/api")):
            items = (await client.get(f"{prefix}{path}")).json()["items"]
            found.append(next(i["id"] for i in items if i["name"] == name))
        return found[0], found[1]

    async def test_accounts(self):
        bank = await self.make("/accounts", {"name": " Bank ", "type": "BANK", "currency": "USD", "initialBalance": 1000.5, "color": "#112233", "icon": "landmark"})
        await self.make("/accounts", {"name": "Lira wallet", "type": "CASH", "currency": "LBP", "initialBalance": 4475000})
        await self.make("/accounts", {"name": "Card", "type": "CREDIT_CARD", "initialBalance": -250, "includeInTotal": True})
        for query in ("", "?includeArchived=true", "?type=BANK,CASH", "?type=NOPE"):
            self.assertParity(*await self.both("GET", f"/accounts{query}"))
        self.assertParity(*await self.both("GET", "/accounts/summary"))
        self.assertParity(*await self.per_side("PATCH", "/accounts/{}", bank, {"name": "Main bank", "color": None, "initialBalance": None}))
        self.assertParity(*await self.per_side("POST", "/accounts/{}/archive", bank))
        self.assertParity(*await self.both("GET", "/accounts/summary"))
        self.assertParity(*await self.per_side("POST", "/accounts/{}/unarchive", bank))
        cash = await self.ids_by_name("/accounts", "Cash")
        self.assertParity(
            await self.nest.post("/accounts/reorder", json={"items": [{"id": bank[0], "sortOrder": 0}, {"id": cash[0], "sortOrder": 5}]}),
            await self.py.post("/api/accounts/reorder", json={"items": [{"id": bank[1], "sortOrder": 0}, {"id": cash[1], "sortOrder": 5}]}),
        )
        self.assertParity(*await self.both("GET", "/accounts"))
        for body in ({}, {"name": ""}, {"name": "x", "type": "VAULT"}, {"name": "x", "currency": "BTC"}, {"name": "x", "initialBalance": 1.234}, {"name": "x", "sortOrder": -1}):
            self.assertParity(*await self.both("POST", "/accounts", body))

    async def test_transactions_move_balances(self):
        bank = await self.make("/accounts", {"name": "Bank", "currency": "USD", "initialBalance": 1000})
        lira = await self.make("/accounts", {"name": "Lira", "type": "CASH", "currency": "LBP"})
        food = await self.ids_by_name("/categories?scope=EXPENSE", "Food")
        salary = await self.ids_by_name("/categories?scope=INCOME", "Salary")

        async def tx(body_for):
            nest = await self.nest.post("/transactions", json=body_for(0))
            py = await self.py.post("/api/transactions", json=body_for(1))
            self.assertParity(nest, py)
            return nest.json().get("id"), py.json().get("id")

        lunch = await tx(lambda i: {"type": "EXPENSE", "amount": 12.5, "accountId": bank[i], "categoryId": food[i], "title": "Lunch", "date": "2026-11-02"})
        await tx(lambda i: {"type": "EXPENSE", "amount": 450000, "currency": "LBP", "accountId": bank[i], "title": "Taxi", "date": "2026-11-02T08:00:00.000Z", "paymentMethod": "CASH"})
        await tx(lambda i: {"type": "INCOME", "amount": 2000, "accountId": bank[i], "categoryId": salary[i], "source": "ACME", "paymentMethod": "CASH", "date": "2026-11-01"})
        await tx(lambda i: {"type": "TRANSFER", "amount": 100, "accountId": bank[i], "toAccountId": lira[i], "toAmount": 8950000, "date": "2026-11-03"})
        await tx(lambda i: {"type": "TRANSFER", "amount": 50, "accountId": bank[i], "toAccountId": lira[i], "date": "2026-11-03"})
        for body in (
            lambda i: {"type": "TRANSFER", "amount": 5, "accountId": bank[i]},
            lambda i: {"type": "TRANSFER", "amount": 5, "accountId": bank[i], "toAccountId": bank[i]},
            lambda i: {"type": "INCOME", "amount": 5, "categoryId": food[i]},
        ):
            await tx(body)
        self.assertParity(*await self.both("GET", "/accounts/summary"))
        for query in ("", "?type=EXPENSE", "?from=2026-11-02&to=2026-11-02", "?q=lunch", "?minAmount=100", "?sort=amount", "?sort=-amount&pageSize=2&page=2", "?categoryId=none", "?type=GIFT", "?sort=nope"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/transactions{query}"))
        self.assertParity(
            await self.nest.get(f"/transactions?accountId={lira[0]}"), await self.py.get(f"/api/transactions?accountId={lira[1]}")
        )
        self.assertParity(*await self.per_side("PATCH", "/transactions/{}", lunch, {"amount": 15, "currency": "EUR", "date": "2026-11-04"}))
        self.assertParity(*await self.per_side("PATCH", "/transactions/{}", lunch, {"type": "INCOME"}))
        self.assertParity(*await self.both("GET", "/accounts"))
        self.assertParity(*await self.per_side("DELETE", "/transactions/{}", lunch))
        self.assertParity(*await self.per_side("DELETE", "/accounts/{}", bank))
        self.assertParity(*await self.both("GET", "/accounts/summary"))

    async def test_repeating_transactions(self):
        bank = await self.make("/accounts", {"name": "Bank", "currency": "USD"})
        nest = await self.nest.post("/transactions", json={"type": "EXPENSE", "amount": 20, "accountId": bank[0], "title": "Gym", "date": "2026-08-31", "rrule": "FREQ=MONTHLY"})
        py = await self.py.post("/api/transactions", json={"type": "EXPENSE", "amount": 20, "accountId": bank[1], "title": "Gym", "date": "2026-08-31", "rrule": "FREQ=MONTHLY"})
        self.assertParity(nest, py)
        self.assertParity(*await self.both("GET", "/transactions?sort=date"))
        self.assertParity(*await self.both("GET", "/accounts"))

    async def test_import(self):
        bank = await self.make("/accounts", {"name": "Bank", "currency": "USD"})
        food = await self.ids_by_name("/categories?scope=EXPENSE", "Food")
        for client, prefix, i in ((self.nest, "", 0), (self.py, "/api", 1)):
            await client.post(f"{prefix}/transactions", json={"type": "EXPENSE", "amount": 4.5, "accountId": bank[i], "categoryId": food[i], "title": "POS 1234 SPINNEYS", "date": "2026-10-10"})
        rows = lambda i: [  # noqa: E731
            {"type": "EXPENSE", "amount": 4.5, "date": "2026-10-10", "title": "POS 9999 SPINNEYS"},
            {"type": "EXPENSE", "amount": 4.5, "date": "2026-10-10", "title": "POS 9999 SPINNEYS"},
            {"type": "EXPENSE", "amount": 7, "date": "2026-10-12", "title": "spinneys!!"},
            {"type": "INCOME", "amount": 100, "date": "2026-10-15", "title": "Refund"},
        ]
        self.assertParity(
            await self.nest.post("/transactions/import/preview", json={"accountId": bank[0], "rows": rows(0)}),
            await self.py.post("/api/transactions/import/preview", json={"accountId": bank[1], "rows": rows(1)}),
        )
        self.assertParity(
            await self.nest.post("/transactions/import", json={"accountId": bank[0], "rows": [{**r, "categoryId": food[0]} for r in rows(0)]}),
            await self.py.post("/api/transactions/import", json={"accountId": bank[1], "rows": [{**r, "categoryId": food[1]} for r in rows(1)]}),
        )
        self.assertParity(*await self.both("GET", "/transactions"))
        for body in ({"rows": []}, {"rows": [{"type": "TRANSFER", "amount": 1, "date": "2026-10-10"}]}, {"rows": [{"type": "EXPENSE", "amount": 1, "date": "10/10/2026"}]}):
            self.assertParity(*await self.both("POST", "/transactions/import/preview", body))

    async def test_validation_shapes(self):
        for body in ({}, {"type": "EXPENSE"}, {"type": "EXPENSE", "amount": 0}, {"type": "EXPENSE", "amount": 1.005},
                     {"type": "EXPENSE", "amount": 1, "currency": "BTC"}, {"type": "EXPENSE", "amount": 1, "date": "nope"},
                     {"type": "EXPENSE", "amount": 1, "rrule": "FREQ=MINUTELY"}, {"type": "EXPENSE", "amount": 1, "extra": True},
                     {"type": "EXPENSE", "amount": 1, "accountId": "cnosuchaccountidatallxxxx"}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/transactions", body))


if __name__ == "__main__":
    unittest.main()
