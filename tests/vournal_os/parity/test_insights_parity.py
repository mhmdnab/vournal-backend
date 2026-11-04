"""Insights answer like NestJS did: a seeded Beirut-time account in both languages, one
with some sections switched off, and an empty one."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates
from vournal_os.parity.seed import SeedMixin


class InsightsParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def test_empty_account(self):
        self.assertParity(*await self.both("GET", "/insights"))

    async def test_seeded_account_in_both_languages(self):
        await self.seed_life()
        nest, py = await self.both("GET", "/insights")
        self.assertParity(nest, py)
        self.assertGreater(len(nest.json()), 5, nest.text)  # the seed really trips rules
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        self.assertParity(*await self.both("GET", "/insights"))
        self.assertParity(*await self.both("PATCH", "/settings", {"enabledSections": ["tasks", "habits", "people"]}))
        self.assertParity(*await self.both("GET", "/insights"))


if __name__ == "__main__":
    unittest.main()
