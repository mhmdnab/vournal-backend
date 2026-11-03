"""Request values read as vournalOS's JavaScript read them (golden/jsparse.json, printed
by golden/jsparse_golden.mjs from the NestJS repo's own validator.js and Node)."""

import json
import math
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase

GOLDEN = json.loads((pathlib.Path(__file__).parent / "golden" / "jsparse.json").read_text())


class Golden(unittest.TestCase):
    def test_iso8601_validation(self):
        from app.os.common.jsparse import is_iso8601

        for case in GOLDEN["dates"]:
            with self.subTest(value=case["value"]):
                self.assertEqual(is_iso8601(case["value"]), case["iso"])

    def test_new_date_for_valid_values(self):
        from app.os.common.jsparse import js_date
        from app.os.common.serialize import to_json

        for case in GOLDEN["dates"]:
            if case["iso"]:
                with self.subTest(value=case["value"]):
                    parsed = js_date(case["value"])
                    self.assertEqual(None if parsed is None else to_json(parsed), case["date"])

    def test_number(self):
        from app.os.common.jsparse import js_number

        for case in GOLDEN["numbers"]:
            with self.subTest(value=case["value"]):
                got = js_number(case["value"])
                expected = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(case["number"], case["number"])
                if isinstance(expected, float) and math.isnan(expected):
                    self.assertTrue(math.isnan(got))
                else:
                    self.assertEqual(got, expected)

    def test_to_string_and_decimal_places(self):
        from app.os.common import validators
        from app.os.common.jsparse import js_to_string

        for case in GOLDEN["strings"]:
            with self.subTest(value=case["value"]):
                self.assertEqual(js_to_string(case["value"]), case["string"])
                try:
                    validators.money(case["value"], "amount")
                    accepted = True
                except ValueError:
                    accepted = False
                # where class-validator threw (a 500 in NestJS), Python refuses with a 400
                self.assertEqual(accepted, case["decimals2"] is True)


    def test_titles_sort_like_locale_compare(self):
        from app.os.common.collation import locale_key

        golden = GOLDEN["titles"]
        self.assertEqual(sorted(reversed(golden), key=locale_key), golden)

    def test_js_sum_adds_like_reduce(self):
        from app.os.common.jsparse import js_sum

        self.assertEqual(js_sum([0.1] * 10), 0.9999999999999999)  # [0.1 × 10].reduce: Python 3.12's sum() says 1.0
        self.assertEqual(js_sum([]), 0)


class Requests(OsTestCase):
    async def test_impossible_and_foreign_dates(self):
        client = await self.signed_in()
        rolled = await client.post("/api/transactions", json={"type": "EXPENSE", "amount": 5, "date": "2026-02-30T10:00:00Z"})
        self.assertEqual(rolled.status_code, 201, rolled.text)
        self.assertEqual(rolled.json()["date"], "2026-03-02T10:00:00.000Z")
        noon = await client.post("/api/transactions", json={"type": "EXPENSE", "amount": 5, "date": "2026-02-30"})
        self.assertEqual(noon.status_code, 201, noon.text)
        self.assertEqual(noon.json()["date"][:10], "2026-03-02")
        for bad in ("2026-13-01", "٢٠٢٦-01-01", "2026-11-05T25:00:00Z"):
            response = await client.post("/api/transactions", json={"type": "EXPENSE", "amount": 5, "date": bad})
            self.assertEqual(response.status_code, 400, bad)
        summary = await client.get("/api/finance/summary?from=2026-02-30&to=2026-03-31")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["fromDate"], "2026-03-02")

    async def test_odd_numbers_in_query_strings_are_400(self):
        client = await self.signed_in()
        for path in ("/api/transactions?page=inf", "/api/transactions?page=nan", "/api/transactions?pageSize=1e400",
                     "/api/transactions?minAmount=nan", "/api/transactions?minAmount=1_000", "/api/recurring-payments/summary?days=inf",
                     "/api/finance/forecast?days=Infinity"):
            with self.subTest(path=path):
                self.assertEqual((await client.get(path)).status_code, 400)
        self.assertEqual((await client.get("/api/transactions?page=0x2")).json()["page"], 2)

    async def test_same_day_names_sort_like_locale_compare(self):
        client = await self.signed_in()
        day = (datetime.now(timezone.utc) + timedelta(days=20)).date().isoformat()
        for name in ("Netflix", "netflix", "Zebra", "Äpfel"):
            made = await client.post("/api/recurring-payments", json={"name": name, "amount": 5, "startDate": day})
            self.assertEqual(made.status_code, 201, made.text)
        summary = (await client.get("/api/recurring-payments/summary?days=30")).json()
        on_day = [u["name"] for u in summary["upcoming"] if u["date"].startswith(day)]
        self.assertEqual(on_day, ["Äpfel", "netflix", "Netflix", "Zebra"])
        for title in ("Zebra", "Äpfel", "Netflix", "netflix"):
            await client.post("/api/tasks", json={"title": title, "dueAt": "2026-11-20"})
        feed = (await client.get("/api/calendar/feed?from=2026-11-20&to=2026-11-20&types=task")).json()["items"]
        self.assertEqual([i["title"] for i in feed], ["Äpfel", "netflix", "Netflix", "Zebra"])


if __name__ == "__main__":
    unittest.main()
