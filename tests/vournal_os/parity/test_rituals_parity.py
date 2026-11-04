"""Rituals answer like NestJS did: the morning plan and evening review, the weekly review
with its stored score, last week's priorities, and the life score, in Beirut time."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates
from vournal_os.parity.seed import SeedMixin, day


class RitualsParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def check(self, method, path, body=None):
        nest, py = await self.both(method, path, body)
        self.assertParity(nest, py)
        if nest.status_code == 400:
            self.assertEqual(nest.json()["message"], py.json()["message"])

    async def test_rituals(self):
        await self.check("GET", "/rituals")
        await self.seed_life()
        await self.check("GET", "/rituals")
        await self.check("PUT", "/rituals/day", {"plan": True, "intention": "  Ship phase 8  ", "mood": 4})
        await self.check("PUT", "/rituals/day", {"review": True, "win": "  ", "note": "good day"})
        for offset in (-1, -2, -4):
            await self.check("PUT", "/rituals/day", {"date": day(offset), "plan": True})
        await self.check("PUT", "/rituals/day", {"date": day(-2), "plan": False, "mood": None})
        await self.check("GET", "/rituals")
        await self.check("GET", "/rituals/week")
        await self.check("GET", f"/rituals/week?date={day(-7)}")
        await self.check("PUT", "/rituals/week", {"date": day(-7), "priorities": [" Sleep ", "", "Run", "Read", "Call mum"], "wentWell": "x", "done": True})
        await self.check("PUT", "/rituals/week", {"priorities": ["Focus"], "improve": "  less phone  "})
        await self.check("GET", "/rituals/week")
        await self.check("PUT", "/rituals/week", {"done": True})
        await self.check("PUT", "/rituals/week", {"done": False})
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar", "weekStartsOn": 0}))
        await self.check("GET", "/rituals/week")
        await self.check("GET", "/rituals")

    async def test_validation(self):
        for body in ({"date": "2026-1-1"}, {"plan": "yes"}, {"mood": 6}, {"mood": 2.5}, {"intention": "x" * 301}, {"intention": 5},
                     {"bogus": 1}):
            with self.subTest(day=body):
                await self.check("PUT", "/rituals/day", body)
        for body in ({"priorities": "Sleep"}, {"priorities": ["a", "b", "c", "d"]}, {"priorities": [1]}, {"priorities": ["x" * 161]},
                     {"wentWell": "x" * 1001}, {"done": 1}, {"date": "soon"}):
            with self.subTest(week=body):
                await self.check("PUT", "/rituals/week", body)
        await self.check("GET", "/rituals/week?date=2026-02-30")
        await self.check("GET", "/rituals/week?date=x")
        await self.check("GET", "/rituals/week?bogus=1")


if __name__ == "__main__":
    unittest.main()
