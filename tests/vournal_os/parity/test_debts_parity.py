"""Debts answer like NestJS did: both directions, part payments, settling,
installments and the summary, in a Beirut-time account."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class DebtsParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def make(self, body):
        nest, py = await self.both("POST", "/debts", body)
        self.assertParity(nest, py)
        return nest.json().get("id"), py.json().get("id")

    async def on_both(self, method, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, f"/debts/{ids[0]}{suffix}", **kwargs),
            await self.py.request(method, f"/api/debts/{ids[1]}{suffix}", **kwargs),
        )

    async def test_debts_lifecycle(self):
        friend = await self.make({"direction": "OWED_TO_ME", "title": " Dinner split ", "counterparty": " Rania ", "principal": 45.5, "dueDate": "2026-11-10", "recordTransaction": True})
        loan = await self.make({"direction": "I_OWE", "kind": "LOAN", "title": "Car loan", "principal": 8950000, "currency": "LBP", "interestRate": 5.125, "startDate": "2026-01-01", "dueDate": "2027-01-01"})
        await self.make({"direction": "I_OWE", "title": "Old", "counterparty": "Omar", "principal": 20, "dueDate": "2026-10-01"})
        for query in ("", "?status=all", "?status=closed", "?direction=I_OWE", "?q=rania", "?status=paid"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/debts{query}"))
        self.assertParity(*await self.both("GET", "/debts/summary"))
        self.assertParity(*await self.on_both("POST", friend, "/payments", {"amount": 20, "date": "2026-11-02", "note": " half "}))
        self.assertParity(*await self.on_both("POST", friend, "/payments", {"amount": 30}))
        self.assertParity(*await self.on_both("POST", friend, "/settle", {}))
        self.assertParity(*await self.on_both("POST", friend, "/settle", {}))
        self.assertParity(*await self.on_both("POST", friend, "/payments", {"amount": 1}))
        self.assertParity(*await self.on_both("POST", friend, "/status", {"status": "OPEN"}))
        self.assertParity(*await self.on_both("POST", loan, "/payments", {"amount": 895000, "date": "2026-02-01"}))
        self.assertParity(*await self.on_both("PATCH", loan, json={"principal": 895000}))
        self.assertParity(*await self.on_both("PATCH", loan, json={"principal": 9000000, "notes": "refinanced"}))
        self.assertParity(*await self.on_both("POST", loan, "/status", {"status": "CANCELLED"}))
        self.assertParity(*await self.both("GET", "/debts?status=all"))
        self.assertParity(*await self.both("GET", "/transactions"))
        self.assertParity(*await self.on_both("GET", loan))
        self.assertParity(*await self.on_both("DELETE", loan))

    async def test_installments(self):
        ids = await self.make({"direction": "I_OWE", "kind": "LOAN", "title": "Phone", "principal": 600, "installments": {"amount": 100, "firstDate": "2026-12-05"}})
        self.assertParity(*await self.on_both("GET", ids))
        self.assertParity(*await self.both("GET", "/recurring-payments"))
        await self.make({"direction": "OWED_TO_ME", "title": "x", "counterparty": "y", "principal": 5, "installments": {"amount": 1, "firstDate": "2026-12-05"}})

    async def test_validation_shapes(self):
        for body in ({}, {"direction": "I_OWE", "title": "x", "principal": 5}, {"direction": "UP", "title": "x", "principal": 5, "counterparty": "a"},
                     {"direction": "I_OWE", "title": "x", "principal": 0, "counterparty": "a"}, {"direction": "I_OWE", "title": "x", "principal": 5, "counterparty": "a", "interestRate": 1.2345},
                     {"direction": "I_OWE", "title": "x", "principal": 5, "counterparty": "a", "dueDate": "2026-01-01", "startDate": "2026-02-01"},
                     {"direction": "I_OWE", "title": "x", "principal": 5, "counterparty": "a", "extra": 1}):
            self.assertParity(*await self.both("POST", "/debts", body))


if __name__ == "__main__":
    unittest.main()
