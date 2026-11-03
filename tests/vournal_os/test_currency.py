"""Currency conversion and the finance helpers, ported from vournalOS's
currency.service.spec.ts and finance.utils (rounding matches JavaScript's)."""

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from vournal_os.support import OsTestCase


class Rates(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import market_rates

        self.market = market_rates
        market_rates.reset_for_tests()
        self.addCleanup(market_rates.reset_for_tests)
        # live rates in memory only: nothing to load, nothing stale, no network
        market_rates._snapshot = market_rates.Snapshot({}, None, datetime.now(timezone.utc))
        market_rates._loaded = True

    async def _user_with(self, rows, currency="USD"):
        user = await self.make_user()
        await self.db.usersettings.create(data={"userId": user.id, "defaultCurrency": currency})
        for code, rate, manual in rows:
            await self.db.exchangerate.create(data={"userId": user.id, "currency": code, "ratePerUsd": Decimal(rate), "manual": manual})
        return user

    async def test_rates_layer_defaults_live_and_manual(self):
        from app.os.common import currency

        self.market._snapshot = self.market.Snapshot({"EUR": Decimal("0.95"), "LBP": Decimal("90100")}, "test", datetime.now(timezone.utc))
        user = await self._user_with([("EUR", "0.9", True), ("GBP", "0.5", False), ("USD", "2", True)])
        rates = await currency.get_rates(user.id)
        self.assertEqual(rates["EUR"], 0.9)  # manual wins
        self.assertEqual(rates["LBP"], 90100)  # live beats the default
        self.assertEqual(rates["GBP"], 0.79)  # a stored row that isn't manual never overrides
        self.assertEqual(rates["USD"], 1)

    async def test_invalid_stored_rates_are_ignored(self):
        from app.os.common import currency

        user = await self._user_with([("EUR", "0", True)])
        self.assertEqual((await currency.get_rates(user.id))["EUR"], 0.92)

    async def test_convert(self):
        from app.os.common import currency

        user = await self._user_with([("EUR", "0.9", True), ("LBP", "90000", True)])
        self.assertEqual(await currency.convert(100, "USD", "EUR", user.id), 90)
        self.assertEqual(await currency.convert(90, "EUR", "USD", user.id), 100)
        self.assertEqual(await currency.convert(9, "EUR", "LBP", user.id), 900000)
        self.assertEqual(await currency.convert(Decimal("12.345"), "USD", "USD", user.id), 12.35)
        self.assertEqual(await currency.convert("10.5", "EUR", "EUR", user.id), 10.5)
        self.assertEqual(await currency.convert(50, "XYZ", "USD", user.id), 50)

    async def test_converter_and_defaults(self):
        from app.os.common import currency

        user = await self._user_with([("EUR", "0.9", True), ("LBP", "90000", True)], currency="LBP")
        fx = await currency.get_converter(user.id)
        self.assertEqual(fx.default_currency, "LBP")
        self.assertEqual(fx.to_default(10, "USD"), 900000)
        self.assertEqual(fx.to_default(10, None), 10)
        self.assertEqual(fx.convert(1, "LBP", "LBP"), 1)
        bare = await self.make_user()  # no settings row: USD
        self.assertEqual((await currency.get_converter(bare.id)).default_currency, "USD")


class Helpers(unittest.TestCase):
    def test_round_and_to_number(self):
        from app.os.common.currency import round_money, to_number

        self.assertEqual(to_number(None), 0)
        self.assertEqual(to_number("abc"), 0)
        self.assertEqual(to_number(Decimal("1.5")), 1.5)
        self.assertEqual(round_money(1.005), 1.01)
        self.assertEqual(round_money(-1.005), -1.01)
        self.assertEqual(round_money(2.344), 2.34)
        self.assertEqual(round_money(1234.5678, 0), 1235)

    def test_finance_utils(self):
        from app.os import finance_utils as fu

        self.assertEqual(fu.parse_instant("2026-11-03", "Asia/Beirut"), datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(fu.parse_boundary("2026-11-03", "Asia/Beirut", "end"), datetime(2026, 11, 3, 21, 59, 59, 999000, tzinfo=timezone.utc))
        self.assertIsNone(fu.parse_boundary("", "UTC", "start"))
        self.assertEqual(fu.pct_change(150, 100), 50.0)
        self.assertIsNone(fu.pct_change(5, 0))
        self.assertEqual(fu.pct(1, 3), 33.3)
        self.assertEqual(fu.pct(1, 0), 0)
        self.assertEqual(fu.enumerate_days("2026-02-27", "2026-03-02"), ["2026-02-27", "2026-02-28", "2026-03-01", "2026-03-02"])
        self.assertEqual(fu.last_months("2026-02", 3), ["2025-12", "2026-01", "2026-02"])
        self.assertEqual(fu.days_in_month(2028, 2), 29)
        self.assertEqual(fu.date_only_to_noon(datetime(2026, 11, 3, tzinfo=timezone.utc), "Asia/Beirut"), datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
        self.assertEqual(fu.day_key("2026-11-03T22:30:00Z", "Asia/Beirut"), "2026-11-04")


if __name__ == "__main__":
    unittest.main()
