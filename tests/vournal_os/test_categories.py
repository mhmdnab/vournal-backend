"""Categories keep vournalOS's rules (one level of sub-categories, names unique per
scope and parent, case-insensitively) and never touch another user's rows."""

import unittest

from vournal_os.support import OsTestCase


class Categories(OsTestCase):
    async def _create(self, client, **body):
        return await client.post("/api/categories", json=body)

    async def test_category_name_clash_is_case_insensitive(self):
        client = await self.signed_in()
        first = await self._create(client, scope="EXPENSE", name="Pets")
        self.assertEqual(first.status_code, 201, first.text)
        clash = await self._create(client, scope="EXPENSE", name="  pets ")
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.json()["message"], "A category with this name already exists")
        other_scope = await self._create(client, scope="INCOME", name="Pets")
        self.assertEqual(other_scope.status_code, 201)

    async def test_subcategory_rules(self):
        client = await self.signed_in()
        parent = (await self._create(client, scope="EXPENSE", name="Car")).json()
        child = await self._create(client, scope="EXPENSE", name="Fuel", parentId=parent["id"])
        self.assertEqual(child.status_code, 201)
        self.assertTrue(child.json()["isSubcategory"])
        wrong_scope = await self._create(client, scope="INCOME", name="Fuel", parentId=parent["id"])
        self.assertEqual(wrong_scope.status_code, 400)
        self.assertEqual(wrong_scope.json()["message"], "Parent category must have the same scope")
        grandchild = await self._create(client, scope="EXPENSE", name="Diesel", parentId=child.json()["id"])
        self.assertEqual(grandchild.json()["message"], "Sub-categories can only be one level deep")
        own = await client.patch(f"/api/categories/{parent['id']}", json={"parentId": parent["id"]})
        self.assertEqual(own.json()["message"], "A category cannot be its own parent")
        other = (await self._create(client, scope="EXPENSE", name="Home stuff")).json()
        has_children = await client.patch(f"/api/categories/{parent['id']}", json={"parentId": other["id"]})
        self.assertEqual(has_children.status_code, 400)
        self.assertEqual(has_children.json()["message"], "Sub-categories can only be one level deep")

    async def test_reorder_with_foreign_id_changes_nothing(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.category.create(data={"userId": other.id, "scope": "TASK", "name": "Theirs"})
        mine = (await self._create(client, scope="TASK", name="Mine")).json()
        response = await client.post(
            "/api/categories/reorder",
            json={"items": [{"id": mine["id"], "sortOrder": 40}, {"id": theirs.id, "sortOrder": 41}]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "Category not found")
        self.assertEqual((await self.db.category.find_unique(where={"id": mine["id"]})).sortOrder, mine["sortOrder"])
        self.assertEqual((await self.db.category.find_unique(where={"id": theirs.id})).sortOrder, 0)

    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.category.create(data={"userId": other.id, "scope": "TASK", "name": "Theirs"})
        for method, body in (("GET", None), ("PATCH", {"name": "Mine now"}), ("DELETE", None)):
            response = await client.request(method, f"/api/categories/{theirs.id}", json=body)
            self.assertEqual(response.status_code, 404, method)
            self.assertEqual(response.json()["message"], "Category not found")
        self.assertEqual((await self.db.category.find_unique(where={"id": theirs.id})).name, "Theirs")

    async def test_delete_cascades_subcategories(self):
        client = await self.signed_in()
        parent = (await self._create(client, scope="EXPENSE", name="Car")).json()
        child = (await self._create(client, scope="EXPENSE", name="Fuel", parentId=parent["id"])).json()
        response = await client.delete(f"/api/categories/{parent['id']}")
        self.assertEqual(response.json(), {"ok": True})
        self.assertIsNone(await self.db.category.find_unique(where={"id": child["id"]}))

    async def test_category_activity_logged(self):
        client = await self.signed_in()
        made = (await self._create(client, scope="NOTE", name="Poems")).json()
        await client.patch(f"/api/categories/{made['id']}", json={"name": "Poetry"})
        await client.delete(f"/api/categories/{made['id']}")
        rows = await self.db.activitylog.find_many(
            where={"userId": client.user.id}, order={"createdAt": "asc"}
        )
        self.assertEqual([r.action for r in rows], ["category.created", "category.updated", "category.deleted"])
        self.assertEqual(rows[0].metadata, {"name": "Poems", "scope": "NOTE"})
        self.assertIsNone(rows[1].metadata)
        self.assertEqual(rows[2].metadata, {"name": "Poetry"})


if __name__ == "__main__":
    unittest.main()
