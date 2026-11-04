"""Search answers like NestJS did across every section: ranking (exact, prefix, word,
contains, another field), grouping, limits, Arabic text, short queries and filters."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates
from vournal_os.parity.seed import SeedMixin, at, day


class SearchParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def test_search(self):
        await self.seed_life()
        await self.on_each("POST", "/notes", {"title": "Rent contract", "content": "## Rent\n\nThe **landlord** wants the rent by the 5th of each month, " + "x" * 120})
        await self.on_each("POST", "/notes", {"title": "Ideas", "content": "مكتبة: كتب عن السفر 📚 rent"})
        await self.on_each("POST", "/shopping/items", {"name": "Rent-a-car voucher", "store": "Avis"})
        await self.on_each("POST", "/trips", {"name": "Rome", "destination": "Italy", "startDate": day(12)})
        await self.on_each("POST", "/recipes", {"name": "Lentil soup", "prepMinutes": 10, "cookMinutes": 30, "ingredients": [{"name": "Red lentils"}]})
        await self.on_each("POST", "/debts", {"direction": "I_OWE", "title": "Rent share", "counterparty": "Omar", "principal": 150})
        await self.on_each("POST", "/people", {"name": "رنا", "relationship": "صديقة"})
        await self.on_each("POST", "/events", {"title": "Rent viewing", "startAt": at(day(2), 17), "location": "Hamra"})
        for q in ("rent", "RENT", "Rent", "re", "r", "  ", "", "lentil", "رنا", "سفر", "visa", "x" * 101, "omar", "car", "📚"):
            for extra in ("", "&limit=3", "&types=note,task", "&types=expense"):
                with self.subTest(q=q, extra=extra):
                    nest, py = await self.both("GET", f"/search?q={q}{extra}")
                    self.assertParity(nest, py)
                    if nest.status_code == 400:
                        self.assertEqual(nest.json()["message"], py.json()["message"])
        nest, _ = await self.both("GET", "/search?q=rent")
        self.assertGreater(len(nest.json()["results"]), 4, nest.text)
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        for q in ("rent", "passport", "licence"):
            self.assertParity(*await self.both("GET", f"/search?q={q}"))
        self.assertParity(*await self.both("GET", "/search?q=rent&types=planet"))
        self.assertParity(*await self.both("GET", "/search?q=rent&limit=0"))
        self.assertParity(*await self.both("GET", "/search?q=rent&bogus=1"))


if __name__ == "__main__":
    unittest.main()
