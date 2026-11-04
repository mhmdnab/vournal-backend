"""Trips answer like NestJS did: past, ongoing and upcoming trips, budgets and spending in
two currencies, itinerary items, packing lists and presets in both languages, and the
public share link (opened with no session at all). Beirut-time account."""

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from vournal_os.parity.harness import PYTHON_BASE, ParityCase, copy_reference_market_rates

SHARE = ("shareToken", "path")


def day(offset: int) -> str:
    return (datetime.now(ZoneInfo("Asia/Beirut")).date() + timedelta(days=offset)).isoformat()


class TripsParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def pair(self, method, paths, body=None, *, status=None, ignore=SHARE):
        """Same request to both; `paths` and `body` may be (nest, python) pairs. Returns both bodies."""
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}{p}", json=body[i] if isinstance(body, tuple) else body))
        self.assertParity(*out, ignore_keys=ignore)
        if status is not None:
            self.assertEqual(out[0].status_code, status, out[0].text)
        if out[0].status_code == 400 and isinstance(out[0].json().get("message"), list):
            self.assertEqual(out[0].json()["message"], out[1].json()["message"])  # word for word
        return tuple(r.json() if r.content else None for r in out)

    async def make(self, path, body):
        nest, py = await self.pair("POST", path, body, status=201)
        return nest["id"], py["id"]

    @staticmethod
    def at(ids, suffix=""):
        return tuple(f"/trips/{i}{suffix}" for i in ids)

    async def test_trips_items_and_money(self):
        rome = await self.make("/trips", {"name": " Rome ", "destination": "Italy", "startDate": day(-2), "endDate": day(2), "budget": 1500,
                                          "currency": "EUR", "color": "#f97316", "transportation": "Flight", "accommodation": "Hotel"})
        await self.make("/trips", {"name": "Paris", "startDate": "2026-01-10", "endDate": "2026-01-15", "status": "BOOKED"})
        await self.make("/trips", {"name": "Tokyo", "destination": "Japan", "startDate": day(40), "endDate": day(50), "status": "COMPLETED"})
        await self.make("/trips", {"name": "Cancelled", "startDate": day(10), "status": "CANCELLED"})
        await self.make("/trips", {"name": "Today only"})
        for query in ("", "?when=upcoming", "?when=ongoing", "?when=past", "?when=active", "?when=active&includeCancelled=true",
                      "?status=CANCELLED", "?status=PLANNING,BOOKED&when=upcoming", "?q=ital", "?when=soon", "?status=DONE", "?bogus=1"):
            with self.subTest(query=query):
                await self.pair("GET", f"/trips{query}")
        items = []
        for body in ({"title": "Flight in", "type": "TRANSPORT", "startAt": f"{day(-2)}T06:00:00.000Z", "endAt": f"{day(-2)}T09:30:00.000Z",
                      "cost": 320, "currency": "USD", "confirmationCode": " AB12 ", "location": "FCO"},
                     {"title": "Hotel", "type": "ACCOMMODATION", "cost": 600, "url": "https://hotel.example.com", "isDone": True},
                     {"title": "Colosseum", "startAt": f"{day(0)}T08:00:00.000Z", "cost": 18},
                     {"title": "Gelato", "type": "ACTIVITY", "notes": "pistachio", "sortOrder": 7}):
            nest, py = await self.pair("POST", self.at(rome, "/items"), body, status=201)
            items.append((nest["id"], py["id"]))
        for body in ({}, {"title": ""}, {"title": "x", "type": "HIKE"}, {"title": "x", "cost": -1}, {"title": "x", "cost": 1.005},
                     {"title": "x", "url": "hotel.example.com"}, {"title": "x", "startAt": f"{day(1)}T10:00:00Z", "endAt": f"{day(1)}T09:00:00Z"},
                     {"title": "x", "sortOrder": -1}, {"title": "x", "bogus": 1}):
            with self.subTest(item=body):
                await self.pair("POST", self.at(rome, "/items"), body)
        await self.pair("GET", self.at(rome, "/items"))
        hotel = tuple(f"/trips/{t}/items/{i}" for t, i in zip(rome, items[1]))
        await self.pair("PATCH", hotel, {"cost": 650, "currency": None})
        await self.pair("PATCH", hotel, {"startAt": f"{day(-2)}T14:00:00.000Z", "endAt": f"{day(2)}T10:00:00.000Z", "title": None})
        await self.pair("PATCH", hotel, {"endAt": f"{day(-3)}T10:00:00.000Z"})
        await self.pair("POST", tuple(f"{p}/toggle" for p in hotel))
        await self.pair("POST", self.at(rome, "/items/reorder"), tuple({"items": [{"id": i[side], "sortOrder": n} for n, i in enumerate(reversed(items))]} for side in (0, 1)))
        await self.pair("POST", self.at(rome, "/items/reorder"), {"items": [{"id": "cnosuchitemxxxxxxxxxxxxxx", "sortOrder": 0}]})
        await self.pair("POST", self.at(rome, "/items/reorder"), {"items": []})
        await self.pair("DELETE", tuple(f"/trips/{t}/items/{i}" for t, i in zip(rome, items[3])))
        # money spent on the trip, in two currencies
        # different times: expenses are listed by date only, so a tie has no fixed order on either side
        for body in ({"type": "EXPENSE", "title": "Dinner", "amount": 85, "currency": "EUR", "date": f"{day(-1)}T18:00:00.000Z"},
                     {"type": "EXPENSE", "title": "Taxi", "amount": 40, "date": f"{day(-1)}T20:00:00.000Z"}):
            self.assertParity(*[await c.post(f"{p}/transactions", json={**body, "tripId": t})
                                for c, p, t in ((self.nest, "", rome[0]), (self.py, "/api", rome[1]))])
        self.assertParity(*[await c.post(f"{p}/tasks", json={"title": "Check in online", "tripId": t, "dueAt": day(-3)})
                            for c, p, t in ((self.nest, "", rome[0]), (self.py, "/api", rome[1]))])
        self.assertParity(*[await c.post(f"{p}/notes", json={"title": "Rome tips", "content": "x" * 400, "tripId": t})
                            for c, p, t in ((self.nest, "", rome[0]), (self.py, "/api", rome[1]))])
        await self.pair("GET", self.at(rome))
        await self.pair("GET", self.at(rome, "/summary"))
        await self.pair("GET", "/trips")
        for body in ({"status": "BOOKED"}, {"budget": 100}, {"endDate": day(-5)}, {"startDate": None}, {"destination": None, "budget": None},
                     {"name": None, "currency": "USD"}, {"color": None, "notes": "  "}):
            with self.subTest(update=body):
                await self.pair("PATCH", self.at(rome), body)
        await self.pair("GET", "/trips/cnosuchtripxxxxxxxxxxxxxx")
        await self.pair("DELETE", self.at(rome))
        await self.pair("GET", self.at(rome))

    async def test_packing(self):
        trip = await self.make("/trips", {"name": "Beirut weekend", "startDate": day(5), "endDate": day(7)})
        await self.pair("POST", self.at(trip, "/packing/template"), {"preset": "basic"})
        nest, py = await self.pair("POST", self.at(trip, "/packing"), {"name": " Camera ", "group": " Electronics ", "quantity": 1}, status=201)
        camera = tuple(f"/trips/{t}/packing/{i}" for t, i in zip(trip, (nest["id"], py["id"])))
        await self.pair("POST", self.at(trip, "/packing"), {"name": "Snacks", "group": "", "isPacked": True, "sortOrder": 2})
        for body in ({}, {"name": ""}, {"name": "x", "quantity": 0}, {"name": "x", "quantity": 1000}, {"name": "x", "group": "x" * 61}):
            with self.subTest(packing=body):
                await self.pair("POST", self.at(trip, "/packing"), body)
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        await self.pair("POST", self.at(trip, "/packing/template"), {"preset": "beach"})  # nothing twice, groups reused
        await self.pair("POST", self.at(trip, "/packing/template"), {"preset": "beach"})
        await self.pair("POST", self.at(trip, "/packing/template"), {"preset": "mountain"})
        await self.pair("PATCH", camera, {"group": None, "quantity": 2, "name": None})
        await self.pair("POST", tuple(f"{p}/toggle" for p in camera))
        packing = await self.pair("GET", self.at(trip, "/packing"))
        await self.pair("POST", self.at(trip, "/packing/reorder"),
                        tuple({"items": [{"id": p["items"][0]["id"], "sortOrder": 99, "group": " Misc "}, {"id": p["items"][1]["id"], "sortOrder": 98, "group": None}]}
                              for p in packing))
        await self.pair("POST", self.at(trip, "/packing/pack-all"))
        await self.pair("GET", self.at(trip, "/summary"))
        await self.pair("POST", self.at(trip, "/packing/unpack-all"))
        await self.pair("DELETE", camera)
        await self.pair("DELETE", camera)
        await self.pair("GET", self.at(trip, "/packing"))

    async def test_share_link(self):
        trip = await self.make("/trips", {"name": "Lisbon", "destination": "Portugal", "startDate": day(20), "endDate": day(24), "budget": 900,
                                          "notes": "private", "transportation": "Train"})
        self.assertParity(*[await c.post(f"{p}/trips/{t}/items", json={"title": "Tram 28", "startAt": f"{day(21)}T09:00:00.000Z",
                                                                        "confirmationCode": "SECRET", "cost": 3})
                            for c, p, t in ((self.nest, "", trip[0]), (self.py, "/api", trip[1]))])
        shared = await self.pair("POST", self.at(trip, "/share"))
        again = await self.pair("POST", self.at(trip, "/share"))
        for first, second in zip(shared, again):
            self.assertEqual(first["shareToken"], second["shareToken"])  # the same link
            self.assertEqual(first["path"], f"/share/trip/{first['shareToken']}")
        nest_public = httpx.AsyncClient(base_url=self.nest.base_url, headers={"X-Forwarded-For": self.nest.headers["X-Forwarded-For"]})
        from app.main import app

        py_public = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PYTHON_BASE)
        self.addAsyncCleanup(nest_public.aclose)
        self.addAsyncCleanup(py_public.aclose)
        tokens = tuple(s["shareToken"] for s in shared)
        self.assertParity(await nest_public.get(f"/public/trips/{tokens[0]}"), await py_public.get(f"/api/public/trips/{tokens[1]}"))
        for bad in ("short", "x" * 65, "has spaces in it at all!", "A" * 32):
            self.assertParity(await nest_public.get(f"/public/trips/{bad}"), await py_public.get(f"/api/public/trips/{bad}"))
        await self.pair("GET", self.at(trip))
        await self.pair("DELETE", self.at(trip, "/share"))
        self.assertParity(await nest_public.get(f"/public/trips/{tokens[0]}"), await py_public.get(f"/api/public/trips/{tokens[1]}"))
        await self.pair("GET", "/trips")

    async def test_validation(self):
        for body in ({}, {"name": ""}, {"name": "x" * 121}, {"name": "A", "startDate": "2026-05-10", "endDate": "2026-05-01"},
                     {"name": "A", "budget": -1}, {"name": "A", "budget": 1.234}, {"name": "A", "currency": "XXX"}, {"name": "A", "color": "red"},
                     {"name": "A", "status": "GONE"}, {"name": "A", "startDate": "soon"}, {"name": "A", "destination": 5}, {"name": "A", "bogus": 1}):
            with self.subTest(body=body):
                await self.pair("POST", "/trips", body)


if __name__ == "__main__":
    unittest.main()
