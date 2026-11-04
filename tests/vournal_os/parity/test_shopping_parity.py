"""Shopping answers like NestJS did: lists with totals across currencies, items,
ticking, clearing, logging the expense (English and Arabic), reorder, validation."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class ShoppingParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500, "EUR": 0.9}}))

    async def pair(self, method, paths, body=None, ids=None):
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            payload = None if body is None and not ids else {**(body or {}), **{k: v[i] for k, v in (ids or {}).items()}}
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}{p}", json=payload))
        self.assertParity(*out)
        return tuple(r.json().get("id") if isinstance(r.json(), dict) else None for r in out)

    def both_paths(self, ids, suffix):
        return (f"/shopping/{suffix[0]}/{ids[0]}{suffix[1]}", f"/shopping/{suffix[0]}/{ids[1]}{suffix[1]}")

    async def test_lists_and_items(self):
        self.assertParity(*await self.both("GET", "/shopping/lists"))
        weekly = await self.pair("POST", "/shopping/lists", {"name": " Weekly ", "currency": "EUR", "icon": "cart", "color": "#ff8800"})
        party = await self.pair("POST", "/shopping/lists", {"name": "Party"})
        milk = await self.pair("POST", "/shopping/items", {"name": " Milk ", "quantity": 2, "unit": "L", "estimatedPrice": 3.5}, {"listId": weekly})
        await self.pair("POST", "/shopping/items", {"name": "Bread", "estimatedPrice": 89500, "currency": "LBP", "store": "Spinneys"}, {"listId": weekly})
        await self.pair("POST", "/shopping/items", {"name": "Cake", "priority": "HIGH", "isPurchased": True, "actualPrice": 20}, {"listId": party})
        await self.pair("POST", "/shopping/items", {"name": "Loose item", "unit": ""})
        self.assertParity(*await self.both("GET", "/shopping/lists"))
        for query in ("", "?purchased=true", "?store=spinneys", "?q=mil", "?limit=1", "?limit=0", "?bogus=1"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/shopping/items{query}"))
        await self.pair("POST", self.both_paths(milk, ("items", "/toggle")))
        await self.pair("PATCH", self.both_paths(milk, ("items", "")), {"actualPrice": 3.2, "quantity": None, "store": ""})
        await self.pair("PATCH", self.both_paths(milk, ("items", "")), {"isPurchased": None})
        await self.pair("PATCH", self.both_paths(milk, ("items", "")), {"isPurchased": True}, {"listId": party})
        await self.pair("GET", self.both_paths(weekly, ("lists", "")))
        await self.pair("POST", self.both_paths(party, ("lists", "/log-expense")), {})
        await self.both("PATCH", "/settings", {"language": "ar"})
        await self.pair("POST", self.both_paths(party, ("lists", "/log-expense")), {"date": "2026-11-01"})
        self.assertParity(*await self.both("GET", "/transactions"))
        await self.pair("POST", self.both_paths(weekly, ("lists", "/log-expense")), {})
        await self.pair("PATCH", self.both_paths(party, ("lists", "")), {"isArchived": True})
        await self.pair("PATCH", self.both_paths(party, ("lists", "")), {"shared": True})
        for query in ("?includeArchived=true", "?archived=true"):
            self.assertParity(*await self.both("GET", f"/shopping/lists{query}"))
        await self.pair("POST", self.both_paths(party, ("lists", "/clear-purchased")))
        await self.pair("DELETE", self.both_paths(weekly, ("lists", "")))
        self.assertParity(*await self.both("GET", "/shopping/items"))

    async def test_validation(self):
        for body in ({}, {"name": "x" * 61}, {"name": "x", "currency": "BTC"}, {"name": "x", "sortOrder": -1}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/shopping/lists", body))
        for body in ({}, {"name": "x", "quantity": 0}, {"name": "x", "priority": "MEGA"}, {"name": "x", "listId": "cnosuchlistxxxxxxxxxxxxxxx"}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/shopping/items", body))


if __name__ == "__main__":
    unittest.main()
