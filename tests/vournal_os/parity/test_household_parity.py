"""Households answer like NestJS did: making one, invite codes, joining, leaving,
removing members, the owner leaving, and what shared lists and events do then."""

import re
import unittest

from vournal_os.parity.harness import ParityCase

CODE = re.compile(r"^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")


class HouseholdParity(ParityCase):
    maxDiff = None

    async def invite(self, nest, py):
        """A fresh code on each side (they differ): returns both."""
        responses = (await nest.post("/household/invite"), await py.post("/api/household/invite"))
        self.assertParity(*responses, ignore_keys=("code",))
        codes = tuple(r.json()["code"] for r in responses)
        for code in codes:
            self.assertRegex(code, CODE)
        return codes

    async def join(self, clients, codes, *, alter=lambda c: c):
        nest, py = clients
        responses = (await nest.post("/household/join", json={"code": alter(codes[0])}),
                     await py.post("/api/household/join", json={"code": alter(codes[1])}))
        self.assertParity(*responses)
        return responses

    async def test_create_invite_join_leave(self):
        self.assertParity(*await self.both("GET", "/household"))
        self.assertParity(*await self.both("POST", "/household/invite"))  # not in one yet
        self.assertParity(*await self.both("POST", "/household/leave"))
        self.assertParity(*await self.both("POST", "/household", {"name": "  The Haddads "}))
        self.assertParity(*await self.both("POST", "/household", {"name": "Again"}))
        self.assertParity(*await self.both("PATCH", "/household", {"name": "Home"}))
        sam = await self.other_user("Sam")
        lina = await self.other_user("Lina")
        codes = await self.invite(self.nest, self.py)
        # dashes, spaces and case don't matter; a used code doesn't work twice
        await self.join(sam, codes, alter=lambda c: f" {c.lower()} ")
        await self.join(lina, codes)
        self.assertParity(*await self.both("GET", "/household"))
        lina_codes = await self.invite(self.nest, self.py)  # the owner's new code replaces the old one
        await self.join(lina, lina_codes, alter=lambda c: c.replace("-", ""))
        for nest, py in (sam, lina):
            self.assertParity(await nest.get("/household"), await py.get("/api/household"))
        # only the owner renames and removes; nobody removes themselves
        self.assertParity(await sam[0].patch("/household", json={"name": "Mine"}), await sam[1].patch("/api/household", json={"name": "Mine"}))
        me = (await self.nest.get("/household")).json(), (await self.py.get("/api/household")).json()
        sam_ids = [m["userId"] for m in me[0]["household"]["members"] if m["name"] == "Sam"], [m["userId"] for m in me[1]["household"]["members"] if m["name"] == "Sam"]
        owner_ids = me[0]["household"]["members"][0]["userId"], me[1]["household"]["members"][0]["userId"]
        self.assertParity(await sam[0].delete(f"/household/members/{owner_ids[0]}"), await sam[1].delete(f"/api/household/members/{owner_ids[1]}"))
        self.assertParity(await self.nest.delete(f"/household/members/{owner_ids[0]}"), await self.py.delete(f"/api/household/members/{owner_ids[1]}"))
        self.assertParity(*await self.both("DELETE", "/household/members/cnosuchmemberxxxxxxxxxxx"))
        self.assertParity(await self.nest.delete(f"/household/members/{sam_ids[0][0]}"), await self.py.delete(f"/api/household/members/{sam_ids[1][0]}"))
        self.assertParity(await sam[0].get("/household"), await sam[1].get("/api/household"))
        # the owner leaves: the longest-standing member takes over; the last one out deletes it
        self.assertParity(*await self.both("POST", "/household/leave"))
        self.assertParity(await lina[0].get("/household"), await lina[1].get("/api/household"))
        self.assertParity(await lina[0].post("/household/leave"), await lina[1].post("/api/household/leave"))
        self.assertParity(await lina[0].post("/household/leave"), await lina[1].post("/api/household/leave"))

    async def test_shared_rows_go_back_with_their_owner(self):
        self.assertParity(*await self.both("POST", "/household", {"name": "Flat"}))
        sam = await self.other_user("Sam")
        await self.join(sam, await self.invite(self.nest, self.py))
        for nest, py in ((self.nest, self.py), sam):
            self.assertParity(await nest.post("/shopping/lists", json={"name": "Shared", "shared": True}),
                              await py.post("/api/shopping/lists", json={"name": "Shared", "shared": True}))
            event = {"title": "Dinner", "startAt": "2026-11-20T17:00:00.000Z", "shared": True}
            self.assertParity(await nest.post("/events", json=event), await py.post("/api/events", json=event))
        self.assertParity(*await self.both("GET", "/household"))
        self.assertParity(await sam[0].post("/household/leave"), await sam[1].post("/api/household/leave"))
        self.assertParity(*await self.both("GET", "/household"))
        self.assertParity(*await self.both("GET", "/shopping/lists"))
        self.assertParity(await sam[0].get("/shopping/lists"), await sam[1].get("/api/shopping/lists"))

    async def test_validation(self):
        for body in ({}, {"name": ""}, {"name": "   "}, {"name": "x" * 61}, {"name": 5}, {"name": "ok", "bogus": 1}):
            with self.subTest(body=body):
                nest, py = await self.both("POST", "/household", body)
                self.assertParity(nest, py)
                self.assertEqual(nest.json()["message"], py.json()["message"])
        for body in ({}, {"code": "ABC"}, {"code": 12345678}, {"code": "ABCD-EFGH-J"}, {"code": "ABCD-EFGH"}, {"code": "abcd efgh"}):
            with self.subTest(body=body):
                nest, py = await self.both("POST", "/household/join", body)
                self.assertParity(nest, py)
                self.assertEqual(nest.json()["message"], py.json()["message"])

    async def test_full_household(self):
        self.assertParity(*await self.both("POST", "/household", {"name": "Big"}))
        for i in range(7):
            await self.join(await self.other_user(f"M{i}"), await self.invite(self.nest, self.py))
        self.assertParity(*await self.both("POST", "/household/invite"))
        self.assertParity(*await self.both("GET", "/household"))


if __name__ == "__main__":
    unittest.main()
