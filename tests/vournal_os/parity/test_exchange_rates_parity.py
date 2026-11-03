"""Exchange rates answer like NestJS did, on the same live market rates."""

import unittest

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class ExchangeRatesParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()

    async def test_rates_list(self):
        self.assertParity(*await self.both("GET", "/exchange-rates"))

    async def test_rates_put_then_use_live(self):
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"EUR": 0.95, "LBP": "90000", "USD": 2}}))
        self.assertParity(*await self.both("GET", "/exchange-rates"))
        self.assertParity(*await self.both("POST", "/exchange-rates/use-live", {}))
        self.assertParity(*await self.both("GET", "/exchange-rates"))

    async def test_use_live_for_some(self):
        self.assertParity(*await self.both("PUT", "/exchange-rates", {"rates": {"EUR": 0.95, "GBP": 0.81}}))
        self.assertParity(*await self.both("POST", "/exchange-rates/use-live", {"currencies": ["EUR"]}))

    async def test_rates_validation_shapes(self):
        for body in (
            {},
            {"rates": []},
            {"rates": "EUR"},
            {"rates": {"XYZ": 1}},
            {"rates": {"EUR": 0}},
            {"rates": {"EUR": -1, "GBP": "abc"}},
            {"rates": {"EUR": 1e13}},
            {"rates": {"EUR": True}},
            {"rates": {"EUR": 1}, "extra": 1},
        ):
            self.assertParity(*await self.both("PUT", "/exchange-rates", body))
        for body in ({"currencies": ["XYZ"]}, {"currencies": "EUR"}, {"bogus": 1}):
            self.assertParity(*await self.both("POST", "/exchange-rates/use-live", body))


if __name__ == "__main__":
    unittest.main()
