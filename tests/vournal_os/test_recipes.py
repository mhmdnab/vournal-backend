"""Recipes never touch another user's rows."""

import unittest

from vournal_os.support import OsTestCase


class Recipes(OsTestCase):
    async def test_other_users_recipes_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        recipe = (await owner.post("/api/recipes", json={"name": "Soup", "ingredients": [{"name": "Water"}]})).json()
        for method in ("GET", "PATCH", "DELETE"):
            response = await other.request(method, f"/api/recipes/{recipe['id']}", json={"ingredients": []} if method == "PATCH" else None)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Recipe not found"))
        self.assertEqual(await self.db.recipeingredient.count(where={"recipeId": recipe["id"]}), 1)


if __name__ == "__main__":
    unittest.main()
