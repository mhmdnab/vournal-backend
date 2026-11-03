"""Budgets answer like NestJS did: spending per period across currencies and
sub-categories, in a Beirut-time account."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class BudgetsParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def ids_by_name(self, path, name):
        found = []
        for client, prefix in ((self.nest, ""), (self.py, "/api")):
            items = (await client.get(f"{prefix}{path}")).json()["items"]
            found.append(next(i["id"] for i in items if i["name"] == name))
        return found[0], found[1]

    async def test_budgets_and_spending(self):
        food = await self.ids_by_name("/categories?scope=EXPENSE", "Food")
        salary = await self.ids_by_name("/categories?scope=INCOME", "Salary")
        coffee = []
        for client, prefix, i in ((self.nest, "", 0), (self.py, "/api", 1)):
            coffee.append((await client.post(f"{prefix}/categories", json={"scope": "EXPENSE", "name": "Coffee", "parentId": food[i]})).json()["id"])
            for body in (
                {"type": "EXPENSE", "amount": 30, "categoryId": food[i], "date": "2026-11-02"},
                {"type": "EXPENSE", "amount": 9, "categoryId": coffee[i], "date": "2026-11-03"},
                {"type": "EXPENSE", "amount": 895000, "currency": "LBP", "categoryId": food[i], "date": "2026-11-05"},
                {"type": "EXPENSE", "amount": 50, "date": "2026-10-20"},
                {"type": "EXPENSE", "amount": 70, "currency": "EUR", "date": "2026-11-30T21:30:00.000Z"},
                {"type": "INCOME", "amount": 500, "categoryId": salary[i], "date": "2026-11-01"},
            ):
                await client.post(f"{prefix}/transactions", json=body)
        nest, py = (
            await self.nest.post("/budgets", json={"amount": 120, "categoryId": food[0], "alertThreshold": 50}),
            await self.py.post("/api/budgets", json={"amount": 120, "categoryId": food[1], "alertThreshold": 50}),
        )
        self.assertParity(nest, py)
        ids = (nest.json()["id"], py.json()["id"])
        self.assertParity(*await self.both("POST", "/budgets", {"amount": 400, "name": " Everything ", "currency": "EUR"}))
        self.assertParity(*await self.both("POST", "/budgets", {"amount": 3000, "period": "YEARLY"}))
        self.assertParity(*await self.both("POST", "/budgets", {"amount": 10, "period": "YEARLY"}))
        self.assertParity(
            await self.nest.post("/budgets", json={"amount": 10, "categoryId": salary[0]}),
            await self.py.post("/api/budgets", json={"amount": 10, "categoryId": salary[1]}),
        )
        for query in ("", "?period=2026-11", "?period=2026-10", "?period=2026", "?period=2025", "?period=2027", "?periodType=YEARLY", "?includeInactive=true", "?period=2026-13"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/budgets{query}"))
        self.assertParity(
            await self.nest.patch(f"/budgets/{ids[0]}", json={"amount": 20, "name": None, "isActive": False}),
            await self.py.patch(f"/api/budgets/{ids[1]}", json={"amount": 20, "name": None, "isActive": False}),
        )
        self.assertParity(await self.nest.get(f"/budgets/{ids[0]}?period=2026-10"), await self.py.get(f"/api/budgets/{ids[1]}?period=2026-10"))
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        self.assertParity(*await self.both("GET", "/budgets?includeInactive=true"))
        self.assertParity(await self.nest.delete(f"/budgets/{ids[0]}"), await self.py.delete(f"/api/budgets/{ids[1]}"))

    async def test_validation_shapes(self):
        for body in ({}, {"amount": 0}, {"amount": 5, "period": "WEEKLY"}, {"amount": 5, "alertThreshold": 101}, {"amount": 5, "name": ""}, {"amount": 5, "extra": 1}):
            self.assertParity(*await self.both("POST", "/budgets", body))


if __name__ == "__main__":
    unittest.main()
