"""Finance answers like NestJS did: the summary over periods, the cash-flow forecast
with recurring payments, income and debts, and the spending plan with its settings,
in a Beirut-time account with LBP and EUR money."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates, normalize


def _ties_by_date(body: dict) -> dict:
    top = sorted(body.get("topExpenses", []), key=lambda t: (-t["amountInDefault"], t["date"]))
    return {**body, "topExpenses": top}


class FinanceParity(ParityCase):
    maxDiff = None  # plan and summary bodies are long; show the whole difference

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def post(self, path, body):
        nest, py = await self.both("POST", path, body)
        self.assertParity(nest, py)
        return nest.json().get("id"), py.json().get("id")

    async def category(self, name):
        found = []
        for client, prefix in ((self.nest, ""), (self.py, "/api")):
            rows = (await client.get(f"{prefix}/categories")).json()
            rows = rows.get("items", rows) if isinstance(rows, dict) else rows
            found.append(next(c["id"] for c in rows if c["name"] == name))
        return found

    async def post_split(self, path, body, ids_by_key):
        """POST a body whose id fields differ per side (ids_by_key: field → (nest id, py id))."""
        nest = await self.nest.post(path, json={**body, **{k: v[0] for k, v in ids_by_key.items()}})
        py = await self.py.post(f"/api{path}", json={**body, **{k: v[1] for k, v in ids_by_key.items()}})
        self.assertParity(nest, py)
        return nest.json().get("id"), py.json().get("id")

    async def seed(self):
        wallet = await self.post("/accounts", {"name": "Wallet", "type": "CASH", "currency": "USD", "initialBalance": 1000})
        lira = await self.post("/accounts", {"name": "Lira", "type": "CASH", "currency": "LBP", "initialBalance": 8950000})
        food = await self.category("Food")
        coffee = await self.post_split("/categories", {"name": "Coffee", "scope": "EXPENSE"}, {"parentId": food})
        await self.post_split("/transactions", {"type": "EXPENSE", "amount": 45.5, "date": "2026-11-02", "title": "Lunch"}, {"categoryId": food, "accountId": wallet})
        await self.post_split("/transactions", {"type": "EXPENSE", "amount": 4.25, "date": "2026-11-03", "title": "Latte"}, {"categoryId": coffee})
        await self.post_split("/transactions", {"type": "EXPENSE", "amount": 895000, "currency": "LBP", "date": "2026-11-01", "title": "Fuel"}, {"accountId": lira})
        await self.post("/transactions", {"type": "EXPENSE", "amount": 120, "date": "2026-10-15", "title": "Shoes"})
        await self.post("/transactions", {"type": "INCOME", "amount": 300, "date": "2026-11-03", "title": "Freelance"})
        await self.post("/transactions", {"type": "EXPENSE", "amount": 30, "date": "2026-10-20", "title": "Groceries", "rrule": "FREQ=WEEKLY"})
        await self.post("/recurring-payments", {"name": "Rent", "amount": 700, "kind": "BILL","startDate": "2026-11-10"})
        await self.post("/recurring-payments", {"name": "Netflix", "amount": 12.99, "currency": "EUR", "kind": "SUBSCRIPTION", "startDate": "2026-11-15"})
        await self.post("/debts", {"direction": "I_OWE", "title": "Omar", "counterparty": "Omar", "principal": 150, "dueDate": "2026-11-20"})
        long = await self.post("/debts", {"direction": "I_OWE", "title": "Family loan", "counterparty": "Dad", "principal": 1200})
        await self.both("POST", "/debts", {"direction": "OWED_TO_ME", "title": "Rania", "counterparty": "Rania", "principal": 40, "dueDate": "2026-11-08"})
        await self.post("/debts", {"direction": "I_OWE", "kind": "LOAN", "title": "Phone", "principal": 300, "installments": {"amount": 100, "firstDate": "2026-11-12"}})
        nest = await self.nest.post(f"/debts/{long[0]}/payments", json={"amount": 100, "date": "2026-11-03"})
        py = await self.py.post(f"/api/debts/{long[1]}/payments", json={"amount": 100, "date": "2026-11-03"})
        self.assertParity(nest, py)

    async def test_summary(self):
        await self.seed()
        for query in ("", "?period=week", "?period=day", "?period=year", "?from=2026-10-01&to=2026-10-31",
                      "?from=2026-10-10&to=2026-11-03", "?from=2026-01-01&to=2026-12-31", "?from=2026-11-01T10:00:00Z",
                      "?from=2026-11-05&to=2026-11-01", "?period=decade", "?from=nope"):
            with self.subTest(query=query):
                nest, py = await self.both("GET", f"/finance/summary{query}")
                self.assertEqual(nest.status_code, py.status_code)
                if nest.status_code == 200:
                    # NestJS ranked top expenses by amount over rows fetched with no ORDER BY, so
                    # equal amounts come in physical row order, which is undefined on both sides
                    self.assertEqual(normalize(_ties_by_date(nest.json())), normalize(_ties_by_date(py.json())))
                else:
                    self.assertParity(nest, py)

    async def test_forecast(self):
        await self.seed()
        for query in ("", "?days=30", "?days=7", "?days=365", "?days=6", "?days=abc", "?days=10.5"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/finance/forecast{query}"))

    async def test_plan_and_settings(self):
        self.assertParity(*await self.both("GET", "/finance/plan"))
        await self.seed()
        self.assertParity(*await self.both("GET", "/finance/plan"))
        for body in ({"savingsMode": "percent", "savingsValue": 10, "periodMode": "auto"},
                     {"savingsMode": "percent", "savingsValue": 150, "periodMode": "auto"},
                     {"savingsMode": "amount", "savingsValue": 200, "periodMode": "month", "debtPayoffMonths": 12, "extraDebtPct": 60, "extraSavingsPct": 50},
                     {"savingsMode": "amount", "savingsValue": 200, "periodMode": "month", "debtPayoffMonths": 12, "extraDebtPct": 60, "extraSavingsPct": 30},
                     {}, {"savingsMode": "none", "savingsValue": 0, "periodMode": "auto", "debtPayoffMonths": 0}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("PUT", "/finance/plan/settings", body))
        self.assertParity(*await self.both("GET", "/finance/forecast"))

    async def test_income_setup(self):
        await self.seed()
        for body in ({"title": " Salary ", "amount": 1500, "schedule": {"type": "monthly", "day": 25}, "alreadyReceived": True},
                     {"title": "Side job", "amount": 80, "currency": "EUR", "schedule": {"type": "weekly", "weekday": 5}, "alreadyReceived": False},
                     {"title": "Wage", "amount": 20, "schedule": {"type": "daily", "weekdays": [1, 2, 3, 4, 5]}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "monthly"}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "monthly", "day": 0}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "weekly"}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "daily", "weekdays": []}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "yearly"}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "monthly", "day": 31}, "alreadyReceived": True},
                     {"title": "x", "amount": 1, "schedule": {"type": "monthly", "day": 1}}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/finance/income", body))
        self.assertParity(*await self.both("GET", "/finance/plan"))
        self.assertParity(*await self.both("GET", "/finance/summary"))


if __name__ == "__main__":
    unittest.main()
