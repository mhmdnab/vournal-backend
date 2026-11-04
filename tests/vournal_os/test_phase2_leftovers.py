"""Phase 2 review leftovers: batched writes, the activity-log size rule, an empty
body read as {}, single-flight market-rate loading, tag counts without N+1, and two
missing cases (a name shared across levels, rates with nothing stored and no network)."""

import asyncio
import contextlib
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from vournal_os.support import OsTestCase


@contextlib.contextmanager
def counted_queries():
    """Counts database round trips (one per engine request, a batch counts once)."""
    from app.db import db

    engine = type(db._engine)
    real = engine.query
    calls = []

    async def query(self, *args, **kwargs):
        calls.append(1)
        return await real(self, *args, **kwargs)

    with patch.object(engine, "query", query):
        yield calls


class Leftovers(OsTestCase):
    async def test_reorder_and_rate_saving_are_one_batch(self):
        client = await self.signed_in()
        made = [await self.db.category.create(data={"userId": client.user.id, "scope": "EXPENSE", "name": f"C{i}"}) for i in range(30)]
        with counted_queries() as calls:
            response = await client.post("/api/categories/reorder", json={"items": [{"id": c.id, "sortOrder": 30 - i} for i, c in enumerate(made)]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(len(calls), 8, "one write per category")
        self.assertEqual((await self.db.category.find_unique(where={"id": made[0].id})).sortOrder, 30)
        rates = {"LBP": 89500, "EUR": 0.9, "GBP": 0.8, "AED": 3.67, "SAR": 3.75, "QAR": 3.64, "KWD": 0.31, "TRY": 41}
        with counted_queries() as calls:
            saved = await client.put("/api/exchange-rates", json={"rates": rates})
        self.assertEqual(saved.status_code, 200, saved.text)
        writes_before_view = len(calls)
        self.assertLess(writes_before_view, 15)

    def test_activity_metadata_measures_compact_json_in_utf16_units(self):
        from app.os.common.activity import _metadata

        fits = {f"k{i}": "x" * 791 for i in range(10)}  # JSON.stringify: 7,991 characters; with spaces 8,010
        self.assertEqual(_metadata(fits), fits)
        astral = {"note": "😀" * 3996}  # 4,007 code points but 8,003 UTF-16 units, as JS counts .length
        self.assertEqual(_metadata(astral), {"truncated": True})

    async def test_empty_body_is_an_empty_object(self):
        client = await self.signed_in()
        response = await client.post("/api/exchange-rates/use-live")
        self.assertEqual(response.status_code, 200, response.text)
        bare = await client.request("POST", "/api/exchange-rates/use-live", content=b"", headers={"content-type": "application/json"})
        self.assertEqual(bare.status_code, 200, bare.text)

    async def test_a_body_with_a_length_is_never_buffered(self):
        """The 30 MB cap must see a big upload first: the wrapper may only look at
        bodies that are empty or arrive without a length, and never past one chunk."""
        from app.os.common.http import EmptyJsonBody

        seen = {}

        async def inner(scope, receive, send):
            seen["called"] = True

        async def receive():
            raise AssertionError("read the body of a request that declared its length")

        scope = {"type": "http", "method": "POST", "path": "/api/tags", "headers": [(b"content-length", b"400000000"), (b"content-type", b"application/json")]}
        await EmptyJsonBody(inner)(scope, receive, None)
        self.assertTrue(seen.get("called"))
        chunks = [{"type": "http.request", "body": b'{"a"', "more_body": True}, {"type": "http.request", "body": b":1}", "more_body": False}]
        reads = []

        async def chunked():
            reads.append(1)
            return chunks.pop(0)

        async def reader(scope, receive, send):
            seen["body"] = b"".join([(await receive())["body"], (await receive())["body"]])

        await EmptyJsonBody(reader)({**scope, "headers": [(b"content-type", b"application/json")]}, chunked, None)
        self.assertEqual(seen["body"], b'{"a":1}')

    async def test_market_rates_load_once_and_never_overwrite_a_refresh(self):
        from app.os import market_rates

        market_rates.reset_for_tests()
        self.addCleanup(market_rates.reset_for_tests)
        await self.db.marketrate.delete_many()
        old = datetime.now(timezone.utc) - timedelta(days=3)
        await self.db.marketrate.create(data={"currency": "LBP", "ratePerUsd": Decimal("89000"), "source": "old", "fetchedAt": old})
        real_find = type(self.db.marketrate).find_many
        loads = []

        async def slow_find(self_, *args, **kwargs):
            loads.append(1)
            rows = await real_find(self_, *args, **kwargs)
            # a refresh lands while the slow first load is still reading
            market_rates._snapshot = market_rates.Snapshot({"LBP": Decimal("89600")}, "fresh", datetime.now(timezone.utc))
            return rows

        with patch.object(type(self.db.marketrate), "find_many", slow_find), patch.object(market_rates, "refresh", return_value=False):
            snapshots = await asyncio.gather(*(market_rates.current() for _ in range(5)))
        self.assertEqual(len(loads), 1)
        self.assertTrue(all(s.source == "fresh" for s in snapshots), [s.source for s in snapshots])

    async def test_tag_list_counts_without_a_query_per_tag(self):
        client = await self.signed_in()
        for i in range(12):
            await client.post("/api/tags", json={"name": f"t{i}"})
        tag = (await client.get("/api/tags")).json()["items"][0]
        await client.post("/api/tasks", json={"title": "x", "tagIds": [tag["id"]]})
        with counted_queries() as calls:
            listed = (await client.get("/api/tags")).json()
        self.assertLess(len(calls), 8, "two counts per tag")
        counts = {t["id"]: (t["taskCount"], t["noteCount"]) for t in listed["items"]}
        self.assertEqual(counts[tag["id"]], (1, 0))

    async def test_a_top_level_and_a_sub_category_may_share_a_name(self):
        client = await self.signed_in()
        car = (await client.post("/api/categories", json={"scope": "EXPENSE", "name": "Car"})).json()
        sub = await client.post("/api/categories", json={"scope": "EXPENSE", "name": "Fuel", "parentId": car["id"]})
        top = await client.post("/api/categories", json={"scope": "EXPENSE", "name": "fuel"})
        self.assertEqual((sub.status_code, top.status_code), (201, 201), (sub.text, top.text))

    async def test_rates_with_nothing_stored_and_no_network(self):
        from app.os import market_rates

        market_rates.reset_for_tests()
        self.addCleanup(market_rates.reset_for_tests)
        await self.db.marketrate.delete_many()
        client = await self.signed_in()
        with patch.object(market_rates, "_fetch_json", side_effect=OSError("offline")):
            response = await client.get("/api/exchange-rates")
            await market_rates.wait_for_refresh()
        self.assertEqual(response.status_code, 200, response.text)
        lbp = next(r for r in response.json()["items"] if r["currency"] == "LBP")
        self.assertGreater(lbp["ratePerUsd"], 0)


if __name__ == "__main__":
    unittest.main()
