"""Public trip links (spec): they show one trip with no session, stop working while the
owner has no Plus (and work again on renewal), and go away with the account."""

import unittest

import httpx

from vournal_os.support import OsTestCase


class ShareLinks(OsTestCase):
    async def test_link_follows_the_owners_plus(self):
        from app.main import app

        owner = await self.signed_in()
        trip = (await owner.post("/api/trips", json={"name": "Lisbon", "budget": 900, "notes": "private"})).json()
        token = (await owner.post(f"/api/trips/{trip['id']}/share")).json()["shareToken"]
        stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://life.test")  # no session, no CSRF header
        self.addAsyncCleanup(stranger.aclose)
        seen = await stranger.get(f"/api/public/trips/{token}")
        self.assertEqual(seen.status_code, 200)
        self.assertTrue({"budget", "notes", "shareToken", "spent"}.isdisjoint(seen.json()))
        await self.db.user.update(where={"id": owner.user.id}, data={"plan": "free"})
        lapsed = await stranger.get(f"/api/public/trips/{token}")
        self.assertEqual((lapsed.status_code, lapsed.json()["message"]), (404, "Trip not found"))
        await self.db.user.update(where={"id": owner.user.id}, data={"plan": "plus"})
        self.assertEqual((await stranger.get(f"/api/public/trips/{token}")).status_code, 200)
        await self.db.user.delete(where={"id": owner.user.id})
        self.assertEqual((await stranger.get(f"/api/public/trips/{token}")).status_code, 404)


if __name__ == "__main__":
    unittest.main()


class TripDetail(OsTestCase):
    async def test_trip_detail_opens(self):
        """Regression (found 2026-11-18): trip expenses were filtered by a workspace that transactions don't have."""
        web = await self.signed_in()
        trip = (await web.post("/api/trips", json={"name": "Rome", "destination": "Rome"})).json()
        r = await web.get(f"/api/trips/{trip['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["expensesCount"], 0)
