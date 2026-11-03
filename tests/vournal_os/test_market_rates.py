"""Live market rates: shared, sanity-checked, refreshed at most every 10 minutes.
Providers are stubbed: these tests never touch the network."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from vournal_os.support import OsTestCase

GOOD = {
    "USD": 1, "LBP": 89600, "EUR": 0.93, "GBP": 0.8, "AED": 3.6725, "SAR": 3.75, "QAR": 3.64,
    "KWD": 0.31, "TRY": 41.5, "EGP": 48.9, "JOD": 0.709, "CAD": 1.38, "AUD": 1.53, "CHF": 0.89,
    "JPY": 151, "CNY": 7.21, "INR": 85.2,
}


class MarketRates(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import market_rates

        self.market = market_rates
        market_rates.reset_for_tests()
        self.addCleanup(market_rates.reset_for_tests)
        await self.db.marketrate.delete_many()
        self.calls: list[str] = []

    def _answers(self, *answers):
        """Stub the provider fetch: each provider URL gets the next answer (an
        exception is raised instead of returned)."""
        queue = list(answers)

        async def fetch(url):
            self.calls.append(url)
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return patch.object(self.market, "_fetch_json", side_effect=fetch)

    def test_sanitize_rates(self):
        from app.os.market_rates import sanitize_rates

        raw = {"USD": 1, "EUR": 0.9, "LBP": -1, "GBP": "x", "XYZ": 5, "JPY": 1000, "CAD": "1.4"}
        self.assertEqual(sanitize_rates(raw, {}), {"EUR": 0.9, "CAD": 1.4})
        self.assertEqual(sanitize_rates({"JPY": 500}, {"JPY": Decimal("150")}), {})  # ×3.3
        self.assertEqual(sanitize_rates({"JPY": 300}, {"JPY": Decimal("150")}), {"JPY": 300.0})

    async def test_refresh_stores_and_serves(self):
        with self._answers({"result": "success", "rates": GOOD}):
            self.assertTrue(await self.market.refresh(force=True))
        rows = {r.currency: r for r in await self.db.marketrate.find_many()}
        self.assertEqual(len(rows), 16)  # every supported currency but USD
        self.assertEqual(rows["EUR"].source, "Exchange Rate API")
        snapshot = await self.market.current()
        self.assertEqual(snapshot.rates["LBP"], Decimal("89600"))
        self.assertEqual(snapshot.source, "Exchange Rate API")

    async def test_refresh_drops_implausible_rates(self):
        usd = {code.lower(): rate for code, rate in GOOD.items()}
        usd["jpy"] = 5000  # a ×33 jump against the default: a provider glitch
        with self._answers({"result": "error"}, {"usd": usd}):
            self.assertTrue(await self.market.refresh(force=True))
        rows = {r.currency for r in await self.db.marketrate.find_many()}
        self.assertNotIn("JPY", rows)
        self.assertIn("EUR", rows)
        self.assertEqual((await self.market.current()).source, "Currency API")

        before = {r.currency: r.ratePerUsd for r in await self.db.marketrate.find_many()}
        with self._answers({"result": "success", "rates": {"EUR": 0.5, "GBP": 0.7}}, RuntimeError("down")):
            self.assertFalse(await self.market.refresh(force=True))
        after = {r.currency: r.ratePerUsd for r in await self.db.marketrate.find_many()}
        self.assertEqual(before, after)

    async def test_refresh_gap(self):
        with self._answers({"result": "success", "rates": GOOD}):
            self.assertTrue(await self.market.refresh())
        with self._answers():
            self.assertFalse(await self.market.refresh())
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(self.market.retry_after_seconds(), 0)

    async def test_stale_triggers_background_refresh(self):
        old = datetime.now(timezone.utc) - timedelta(hours=13)
        await self.db.marketrate.create(
            data={"currency": "EUR", "ratePerUsd": Decimal("0.9"), "source": "Old", "fetchedAt": old}
        )
        with self._answers({"result": "success", "rates": GOOD}):
            snapshot = await self.market.current()
            self.assertEqual(snapshot.source, "Old")  # answers at once with what it has
            await self.market.wait_for_refresh()
        self.assertEqual((await self.market.current()).source, "Exchange Rate API")

    async def test_refresh_route(self):
        client = await self.signed_in()
        with self._answers({"result": "success", "rates": GOOD}):
            response = await client.post("/api/exchange-rates/refresh")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body["refreshed"], True)
        self.assertGreater(body["retryAfterSeconds"], 0)
        self.assertEqual(body["live"]["source"], "Exchange Rate API")
        eur = next(i for i in body["items"] if i["currency"] == "EUR")
        self.assertEqual(eur["ratePerUsd"], 0.93)
        again = (await client.post("/api/exchange-rates/refresh")).json()
        self.assertIs(again["refreshed"], False)


if __name__ == "__main__":
    unittest.main()
