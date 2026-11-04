"""People on the web are the app's people (spec "Shared people"): one table, the app's
name rule, everyone from recordings listed, and deleting never touches an entry."""

import unittest

from vournal_os.support import OsTestCase


class People(OsTestCase):
    async def recorded_person(self, user, name="Bilal", mentions=2):
        """A person the app picked up from recordings: no OS fields at all."""
        from app.entities import normalize_name

        person = await self.db.person.create(data={"userId": user.id, "name": name, "normalizedName": normalize_name(name)})
        for i in range(mentions):
            entry = await self.db.entry.create(data={"userId": user.id, "rawTranscript": f"Saw {name} {i}", "emotions": []})
            await self.db.entryperson.create(data={"entryId": entry.id, "personId": person.id})
        return person

    async def test_people_from_recordings_are_listed_and_editable(self):
        client = await self.signed_in()
        person = await self.recorded_person(client.user)
        listed = (await client.get("/api/people")).json()
        self.assertEqual([(p["name"], p["mentionCount"], p["relationship"], p["avatarColor"]) for p in listed["items"]],
                         [("Bilal", 2, None, None)])
        self.assertEqual((await client.get("/api/people/upcoming")).json(), {"items": [], "total": 0})
        edited = await client.patch(f"/api/people/{person.id}", json={"relationship": "Friend", "birthday": "1990-05-04"})
        self.assertEqual((edited.status_code, edited.json()["relationship"], edited.json()["mentionCount"]), (200, "Friend", 2))
        detail = (await client.get(f"/api/people/{person.id}")).json()
        self.assertEqual((detail["birthday"], detail["gifts"], detail["mentionCount"]), ("1990-05-04T00:00:00.000Z", [], 2))

    async def test_adding_someone_who_exists_opens_them(self):
        client = await self.signed_in()
        first = await client.post("/api/people", json={"name": "Sara", "relationship": "Sister"})
        self.assertEqual(first.status_code, 201)
        for name in ("sara", "  SARA ", "Sara"):
            again = await client.post("/api/people", json={"name": name, "relationship": "Colleague"})
            self.assertEqual((again.status_code, again.json()["id"], again.json()["relationship"]), (200, first.json()["id"], "Sister"))
        arabic = await self.recorded_person(client.user, "أحمد", mentions=0)
        same = await client.post("/api/people", json={"name": "احمد"})  # the app folds alef-hamza
        self.assertEqual((same.status_code, same.json()["id"]), (200, arabic.id))
        self.assertEqual(await self.db.person.count(where={"userId": client.user.id}), 2)

    async def test_rename_keeps_the_app_rule(self):
        from app.entities import normalize_name

        client = await self.signed_in()
        sara = (await client.post("/api/people", json={"name": "Sara"})).json()
        omar = (await client.post("/api/people", json={"name": "Omar"})).json()
        clash = await client.patch(f"/api/people/{omar['id']}", json={"name": " sara "})
        self.assertEqual((clash.status_code, clash.json()["message"]), (409, "Someone with this name already exists"))
        renamed = await client.patch(f"/api/people/{sara['id']}", json={"name": "Sara Haddad"})
        self.assertEqual(renamed.status_code, 200)
        row = await self.db.person.find_unique(where={"id": sara["id"]})
        self.assertEqual((row.name, row.normalizedName, row.aliases), ("Sara Haddad", normalize_name("Sara Haddad"), ["Sara"]))
        self.assertEqual((await client.patch(f"/api/people/{sara['id']}", json={"name": "SARA HADDAD"})).status_code, 200)

    async def test_deleting_keeps_the_entries(self):
        client = await self.signed_in()
        person = await self.recorded_person(client.user)
        self.assertEqual((await client.delete(f"/api/people/{person.id}")).json(), {"ok": True})
        self.assertEqual(await self.db.entry.count(where={"userId": client.user.id}), 2)
        self.assertEqual(await self.db.entryperson.count(where={"personId": person.id}), 0)

    def test_avatar_colour_reads_utf16_units(self):
        from app.os.people import pick_color

        # JavaScript reads the first UTF-16 unit of each code point (a lone high surrogate for emoji)
        self.assertEqual(pick_color("Sara"), "#8b5cf6")
        self.assertEqual(pick_color("😀"), "#14b8a6")  # by code point it would be #ec4899
        self.assertEqual(pick_color("علي"), "#14b8a6")


if __name__ == "__main__":
    unittest.main()
