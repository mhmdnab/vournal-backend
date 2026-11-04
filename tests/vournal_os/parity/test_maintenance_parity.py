"""Maintenance answers like NestJS did: items whose next due date follows their repeat
rule, marking them done (with and without an expense), an older completion that doesn't
move the schedule, a series that ends, history, and templates in both languages."""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


def day(offset: int) -> str:
    return (datetime.now(ZoneInfo("Asia/Beirut")).date() + timedelta(days=offset)).isoformat()


class MaintenanceParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def pair(self, method, paths, body=None, *, status=None):
        """Same request to both; `paths` and `body` may be (nest, python) pairs. Returns both bodies."""
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}{p}", json=body[i] if isinstance(body, tuple) else body))
        self.assertParity(*out)
        if status is not None:
            self.assertEqual(out[0].status_code, status, out[0].text)
        if out[0].status_code == 400 and isinstance(out[0].json().get("message"), list):
            self.assertEqual(out[0].json()["message"], out[1].json()["message"])  # word for word
        return tuple(r.json() if r.content else None for r in out)

    async def make(self, body):
        nest, py = await self.pair("POST", "/maintenance", body, status=201)
        return tuple(f"/maintenance/{i}" for i in (nest["id"], py["id"]))

    async def test_items_done_and_history(self):
        oil = await self.make({"title": " Oil change ", "area": "CAR", "rrule": "FREQ=MONTHLY;INTERVAL=6", "lastDoneAt": "2026-06-01",
                               "estimatedCost": 45, "description": "5W-30"})
        await self.make({"title": "Clean AC", "rrule": "freq=monthly;interval=6", "reminderDaysBefore": None})
        await self.make({"title": "Filters", "rrule": "FREQ=MONTHLY;INTERVAL=3", "nextDueAt": "2026-10-01", "notes": "kitchen"})
        doctor = await self.make({"title": "Doctor", "area": "HEALTH", "rrule": "FREQ=YEARLY", "nextDueAt": day(5), "estimatedCost": 30, "currency": "EUR"})
        once = await self.make({"title": "Paint fence", "rrule": "FREQ=DAILY;COUNT=1", "nextDueAt": day(0), "isActive": True})
        for query in ("", "?area=HOME,CAR", "?area=CAR&area=HEALTH", "?status=overdue", "?status=dueSoon,upcoming", "?active=false",
                      "?active=true", "?q=OIL", "?q=kitchen", "?area=BOAT", "?status=late", "?active=maybe", "?bogus=1"):
            with self.subTest(query=query):
                await self.pair("GET", f"/maintenance{query}")
        await self.pair("GET", oil)
        await self.pair("POST", tuple(f"{p}/done" for p in oil))  # today: the schedule moves on
        await self.pair("POST", tuple(f"{p}/done" for p in oil), {"date": "2026-05-01", "cost": 40})  # older: history only
        await self.pair("POST", tuple(f"{p}/done" for p in oil), {"cost": 50.5, "currency": "EUR", "logExpense": True, "notes": "garage"})
        await self.pair("POST", tuple(f"{p}/done" for p in doctor), {"date": day(-3), "cost": 25, "logExpense": True})
        await self.pair("POST", tuple(f"{p}/done" for p in doctor), {"cost": 0, "logExpense": True})
        await self.pair("POST", tuple(f"{p}/done" for p in once))  # the series ends: the item is switched off
        for body in ({"date": "soon"}, {"cost": -1}, {"cost": 1.234}, {"currency": "XXX"}, {"accountId": "cnosuchaccountxxxxxxxxxxx"},
                     {"logExpense": "yes"}, {"bogus": 1}):
            with self.subTest(done=body):
                await self.pair("POST", tuple(f"{p}/done" for p in oil), body)
        logs = await self.pair("GET", tuple(f"{p}/logs" for p in oil))
        await self.pair("GET", "/transactions?type=EXPENSE")
        await self.pair("DELETE", tuple(f"{p}/logs/{l['items'][0]['id']}" for p, l in zip(oil, logs)))  # lastDoneAt follows what's left
        await self.pair("DELETE", tuple(f"{p}/logs/cnosuchlogxxxxxxxxxxxxxxx" for p in oil))
        await self.pair("GET", oil)
        for n, body in enumerate(({"nextDueAt": None}, {"rrule": None}, {"rrule": "FREQ=WEEKLY;byday=mo"}, {"estimatedCost": 20}, {"isActive": False},
                     {"lastDoneAt": None, "description": None}, {"nextDueAt": "2027-01-15", "area": "OTHER"}, {"rrule": "FREQ=SECONDLY"})):
            with self.subTest(update=body):
                await self.pair("PATCH", tuple(p for p in (await self.make({"title": f"Update {n}", "rrule": "FREQ=MONTHLY"}))), body)
        await self.pair("DELETE", doctor)
        await self.pair("GET", doctor)
        await self.pair("GET", "/maintenance")

    async def test_categories(self):
        nest_cat = (await self.nest.post("/categories", json={"name": "Garden", "scope": "MAINTENANCE"})).json()
        py_cat = (await self.py.post("/api/categories", json={"name": "Garden", "scope": "MAINTENANCE"})).json()
        nest_exp = (await self.nest.post("/categories", json={"name": "Odd", "scope": "EXPENSE"})).json()
        py_exp = (await self.py.post("/api/categories", json={"name": "Odd", "scope": "EXPENSE"})).json()
        item = await self.make(({"title": "Mow", "rrule": "FREQ=WEEKLY", "categoryId": nest_cat["id"]},
                                {"title": "Mow", "rrule": "FREQ=WEEKLY", "categoryId": py_cat["id"]}))
        await self.pair("POST", "/maintenance", ({"title": "x", "rrule": "FREQ=WEEKLY", "categoryId": nest_exp["id"]},
                                                 {"title": "x", "rrule": "FREQ=WEEKLY", "categoryId": py_exp["id"]}))
        await self.pair("POST", "/maintenance", {"title": "x", "rrule": "FREQ=WEEKLY", "categoryId": "cnosuchcategoryxxxxxxxxxx"})
        await self.pair("GET", (f"/maintenance?categoryId={nest_cat['id']}", f"/maintenance?categoryId={py_cat['id']}"))
        await self.pair("PATCH", item, {"categoryId": None})

    async def test_templates(self):
        await self.pair("GET", "/maintenance/templates")
        await self.pair("POST", "/maintenance/templates", {"keys": ["oil-change", "nope", "also-nope"]})
        await self.pair("POST", "/maintenance/templates", {"keys": ["oil-change", "passport-renewal", "clean-room", "oil-change"], "startDate": day(3)})
        await self.pair("GET", "/maintenance/templates")
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        await self.pair("GET", "/maintenance/templates")
        await self.pair("POST", "/maintenance/templates", {"keys": ["oil-change", "dentist", "haircut"]})
        await self.pair("GET", "/maintenance")
        for body in ({}, {"keys": []}, {"keys": "oil-change"}, {"keys": [1]}, {"keys": ["x"] * 51}, {"keys": ["haircut"], "startDate": "soon"}):
            with self.subTest(body=body):
                await self.pair("POST", "/maintenance/templates", body)

    async def test_validation(self):
        for body in ({}, {"title": "x"}, {"title": "", "rrule": "FREQ=WEEKLY"}, {"title": "x", "rrule": "nope"}, {"title": "x", "rrule": ""},
                     {"title": "x", "rrule": "FREQ=WEEKLY;DTSTART=20260101"}, {"title": "x", "rrule": "FREQ=WEEKLY", "area": "BOAT"},
                     {"title": "x", "rrule": "FREQ=WEEKLY", "estimatedCost": -5}, {"title": "x", "rrule": "FREQ=WEEKLY", "nextDueAt": "soon"},
                     {"title": "x", "rrule": "FREQ=WEEKLY", "reminderDaysBefore": 400}, {"title": "x", "rrule": "FREQ=WEEKLY", "bogus": 1}):
            with self.subTest(body=body):
                await self.pair("POST", "/maintenance", body)


if __name__ == "__main__":
    unittest.main()
