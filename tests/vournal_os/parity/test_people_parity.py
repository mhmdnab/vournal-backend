"""People, gift ideas and important dates answer like NestJS did, in a Beirut-time
account. Python's people are the app's (one name rule, so cases never add two people
whose names fold together) and carry `mentionCount`, which NestJS had no journal for."""

import unittest

from vournal_os.parity.harness import ParityCase

MENTIONS = ("mentionCount",)


class PeopleParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def pair(self, method, paths, body=None, *, status=None):
        """Same request to both; `paths` and `body` may be (nest, python) pairs for ids. Returns both bodies."""
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}{p}", json=body[i] if isinstance(body, tuple) else body))
        self.assertParity(*out, ignore_keys=MENTIONS)
        if status is not None:
            self.assertEqual(out[0].status_code, status)
        if out[0].status_code == 400 and isinstance(out[0].json().get("message"), list):
            self.assertEqual(out[0].json()["message"], out[1].json()["message"])  # word for word
        return tuple(r.json() if r.content else None for r in out)

    async def person(self, body):
        nest, py = await self.pair("POST", "/people", body, status=201)
        return nest["id"], py["id"]

    async def test_people_and_their_dates(self):
        sara = await self.person({"name": " Sara ", "relationship": "Sister", "birthday": "1995-12-01", "email": " sara@example.com ",
                                  "phone": "+961 3 123456", "notes": "likes tea", "reminderDaysBefore": 7})
        omar = await self.person({"name": "Omar", "birthday": "2000-11-10", "birthdayHasYear": False})
        rania = await self.person({"name": "Rania", "anniversary": "2015-11-20", "avatarColor": "#123456", "reminderDaysBefore": None})
        await self.person({"name": "Tarek", "birthday": "1990-11-04T15:00:00Z", "relationship": "  "})
        await self.person({"name": "رنا", "birthday": "1988-02-29"})
        for query in ("", "?sort=birthday", "?sort=name", "?q=SA", "?q=example", "?relationship=sister", "?relationship=Friend",
                      "?sort=age", "?q=" + "x" * 101, "?bogus=1"):
            with self.subTest(query=query):
                await self.pair("GET", f"/people{query}")
        for query in ("", "?days=0", "?days=366", "?days=367", "?days=-1", "?days=abc", "?days=10.5"):
            with self.subTest(query=query):
                await self.pair("GET", f"/people/upcoming{query}")
        for i, body in enumerate(({"birthdayHasYear": False}, {"birthday": "1996-01-15"}, {"birthdayHasYear": True, "birthday": "1996-01-15"},
                                  {"email": None, "phone": "", "notes": "  ", "avatarColor": None}, {"reminderDaysBefore": 0},
                                  {"birthday": None}, {"anniversary": "2020-11-05"}, {"name": None}, {"birthday": "2999-01-01"},
                                  {"email": ""}, {"name": "Sara H."})):
            with self.subTest(body=body):
                await self.pair("PATCH", (f"/people/{sara[0]}", f"/people/{sara[1]}"), body)
        await self.pair("GET", (f"/people/{omar[0]}", f"/people/{omar[1]}"))
        await self.pair("PATCH", (f"/people/{omar[0]}", f"/people/{omar[1]}"), {"birthdayHasYear": True, "birthday": "2001-11-10"})
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        await self.pair("GET", "/people/upcoming?days=366")
        await self.pair("GET", "/people/cnosuchpersonxxxxxxxxxxxx")
        await self.pair("DELETE", (f"/people/{rania[0]}", f"/people/{rania[1]}"))
        await self.pair("GET", (f"/people/{rania[0]}", f"/people/{rania[1]}"))
        await self.pair("GET", "/people")

    async def test_create_validation(self):
        for body in ({}, {"name": ""}, {"name": "   "}, {"name": "x" * 101}, {"name": "A", "email": "nope"},
                     {"name": "A", "email": "a@b"}, {"name": "A", "email": 5}, {"name": "A", "birthday": "2999-01-01"},
                     {"name": "A", "birthday": "soon"}, {"name": "A", "avatarColor": "red"}, {"name": "A", "reminderDaysBefore": 366},
                     {"name": "A", "reminderDaysBefore": 1.5}, {"name": "A", "relationship": 5}, {"name": "A", "phone": "x" * 41},
                     {"name": "A", "birthdayHasYear": "no"}, {"name": "A", "bogus": 1}, {"name": 7}):
            with self.subTest(body=body):
                await self.pair("POST", "/people", body)

    async def test_gifts(self):
        sara = await self.person({"name": "Sara", "birthday": "1995-12-01"})
        base = (f"/people/{sara[0]}/gifts", f"/people/{sara[1]}/gifts")
        gifts = []
        for body in ({"title": " Book ", "price": 25.5}, {"title": "Perfume", "price": 80, "currency": "EUR", "url": " https://example.com/p ",
                     "occasion": "Birthday", "notes": "the blue one"}, {"title": "Card", "isPurchased": True}, {"title": "Mug", "occasion": " "}):
            nest, py = await self.pair("POST", base, body, status=201)
            gifts.append((nest["id"], py["id"]))
        for body in ({}, {"title": ""}, {"title": "x", "price": 1.234}, {"title": "x", "price": -1}, {"title": "x", "url": "ftp://x.com"},
                     {"title": "x", "url": "example.com"}, {"title": "x", "currency": "XXX"}, {"title": "x", "isPurchased": 1}, {"title": "x", "bogus": 1}):
            with self.subTest(body=body):
                await self.pair("POST", base, body)
        await self.pair("GET", base)
        card = tuple(f"{b}/{g}" for b, g in zip(base, gifts[2]))
        mug = tuple(f"{b}/{g}" for b, g in zip(base, gifts[3]))
        await self.pair("PATCH", mug, {"price": 12})  # no currency yet: the user's own
        await self.pair("PATCH", mug, {"isPurchased": True, "url": None})
        await self.pair("PATCH", mug, {"title": None})
        await self.pair("PATCH", mug, {"isPurchased": None, "price": None})
        await self.pair("POST", tuple(f"{p}/toggle" for p in card))
        await self.pair("POST", tuple(f"{p}/toggle" for p in card))
        await self.pair("GET", (f"/people/{sara[0]}", f"/people/{sara[1]}"))
        await self.pair("GET", "/people")
        await self.pair("DELETE", card)
        await self.pair("DELETE", card)
        omar = await self.person({"name": "Omar"})
        await self.pair("PATCH", (f"/people/{omar[0]}/gifts/{gifts[0][0]}", f"/people/{omar[1]}/gifts/{gifts[0][1]}"), {"title": "x"})
        await self.pair("GET", "/people/cnosuchpersonxxxxxxxxxxxx/gifts")
        # deleting a person takes their gift ideas and dates; linked notes stay, unlinked
        nest_note = await self.nest.post("/notes", json={"title": "About Sara", "personId": sara[0]})
        py_note = await self.py.post("/api/notes", json={"title": "About Sara", "personId": sara[1]})
        self.assertParity(nest_note, py_note)
        await self.pair("POST", "/important-dates", tuple({"title": "Graduation", "date": "2026-06-20", "personId": i} for i in sara))
        await self.pair("GET", (f"/people/{sara[0]}", f"/people/{sara[1]}"))
        await self.pair("DELETE", (f"/people/{sara[0]}", f"/people/{sara[1]}"))
        await self.pair("GET", (f"/notes/{nest_note.json()['id']}", f"/notes/{py_note.json()['id']}"))
        await self.pair("GET", "/important-dates")

    async def test_important_dates(self):
        rania = await self.person({"name": "Rania", "anniversary": "2015-11-20"})
        dates = []
        for body in ({"title": " Wedding ", "date": "2016-06-12", "personId": rania}, {"title": "Exam", "date": "2026-01-10", "repeatsYearly": False},
                     {"title": "Trip", "date": "2026-12-01", "repeatsYearly": False, "reminderDaysBefore": None, "notes": "Rome"},
                     {"title": "Leap", "date": "2024-02-29"}, {"title": "Today", "date": "2026-11-04T23:00:00Z"}):
            if "personId" in body:
                body = tuple({**body, "personId": i} for i in rania)
            nest, py = await self.pair("POST", "/important-dates", body, status=201)
            dates.append((nest["id"], py["id"]))
        for body in ({}, {"title": "x"}, {"title": "x", "date": "soon"}, {"title": "", "date": "2026-01-01"},
                     {"title": "x", "date": "2026-01-01", "personId": "cnosuchpersonxxxxxxxxxxxx"}, {"title": "x", "date": "2026-01-01", "personId": 5},
                     {"title": "x", "date": "2026-01-01", "reminderDaysBefore": -1}, {"title": "x", "date": "2026-01-01", "bogus": 1}):
            with self.subTest(body=body):
                await self.pair("POST", "/important-dates", body)
        await self.pair("GET", "/important-dates")
        await self.pair("GET", (f"/important-dates?personId={rania[0]}", f"/important-dates?personId={rania[1]}"))
        await self.pair("GET", "/important-dates?personId=nobody")
        await self.pair("GET", "/important-dates?bogus=1")
        await self.pair("GET", "/people/upcoming?days=366")
        await self.pair("GET", (f"/people/{rania[0]}", f"/people/{rania[1]}"))
        wedding = (f"/important-dates/{dates[0][0]}", f"/important-dates/{dates[0][1]}")
        for body in ({"personId": None}, {"date": None}, {"title": None}, {"date": "2026-02-30"}, {"repeatsYearly": None, "notes": None},
                     {"repeatsYearly": False}, {"reminderDaysBefore": 30}):
            with self.subTest(body=body):
                await self.pair("PATCH", wedding, body)
        await self.pair("GET", wedding)
        await self.pair("DELETE", wedding)
        await self.pair("GET", wedding)
        await self.pair("DELETE", wedding)


if __name__ == "__main__":
    unittest.main()
