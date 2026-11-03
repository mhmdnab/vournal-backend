"""Tags: unique names per user (case-insensitive), never another user's rows."""

import unittest

from vournal_os.support import OsTestCase


class Tags(OsTestCase):
    async def test_tag_name_clash_is_case_insensitive(self):
        client = await self.signed_in()
        first = await client.post("/api/tags", json={"name": " Urgent "})
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["name"], "Urgent")
        self.assertEqual(first.json()["taskCount"], 0)
        clash = await client.post("/api/tags", json={"name": "urgent"})
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.json()["message"], "A tag with this name already exists")
        other = (await client.post("/api/tags", json={"name": "Later"})).json()
        rename = await client.patch(f"/api/tags/{other['id']}", json={"name": "URGENT"})
        self.assertEqual(rename.status_code, 409)

    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.tag.create(data={"userId": other.id, "name": "theirs"})
        for method, body in (("PATCH", {"name": "mine"}), ("DELETE", None)):
            response = await client.request(method, f"/api/tags/{theirs.id}", json=body)
            self.assertEqual(response.status_code, 404, method)
            self.assertEqual(response.json()["message"], "Tag not found")
        self.assertEqual((await self.db.tag.find_unique(where={"id": theirs.id})).name, "theirs")

    async def test_tag_activity_logged(self):
        client = await self.signed_in()
        made = (await client.post("/api/tags", json={"name": "Home"})).json()
        await client.patch(f"/api/tags/{made['id']}", json={"color": "#112233"})
        await client.delete(f"/api/tags/{made['id']}")
        rows = await self.db.activitylog.find_many(where={"userId": client.user.id}, order={"createdAt": "asc"})
        self.assertEqual([r.action for r in rows], ["tag.created", "tag.deleted"])
        self.assertEqual(rows[0].metadata, {"name": "Home"})


if __name__ == "__main__":
    unittest.main()
