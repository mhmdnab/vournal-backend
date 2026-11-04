"""Meal plan: ownership, and the shopping list built from planned recipes (scaled by
servings, merged by name and unit, topping up unpurchased items)."""

import unittest

from vournal_os.support import OsTestCase


class MealPlan(OsTestCase):
    async def test_other_users_entries_and_recipes_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        recipe = (await owner.post("/api/recipes", json={"name": "Soup"})).json()
        entry = (await owner.post("/api/meal-plan", json={"date": "2026-11-10", "recipeId": recipe["id"]})).json()
        self.assertEqual(entry["title"], "Soup")
        for method in ("GET", "PATCH", "DELETE"):
            response = await other.request(method, f"/api/meal-plan/{entry['id']}", json={} if method == "PATCH" else None)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Meal plan entry not found"))
        stolen = await other.post("/api/meal-plan", json={"date": "2026-11-10", "recipeId": recipe["id"]})
        self.assertEqual((stolen.status_code, stolen.json()["message"]), (404, "Recipe not found"))
        # a title of one's own doesn't skip the recipe's ownership check
        for recipe_id in (recipe["id"], "cnosuchrecipexxxxxxxxxxxxx"):
            titled = await other.post("/api/meal-plan", json={"date": "2026-11-10", "title": "Dinner", "recipeId": recipe_id})
            self.assertEqual((titled.status_code, titled.json()["message"]), (404, "Recipe not found"))
        self.assertEqual(await self.db.mealplanentry.count(where={"userId": other.user.id}), 0)

    async def test_generate_shopping_scales_and_merges(self):
        client = await self.signed_in()
        recipe = (await client.post("/api/recipes", json={"name": "Soup", "servings": 2, "ingredients": [
            {"name": "Lentils", "quantity": 1, "unit": "cup"}, {"name": "Salt"}]})).json()
        for day, servings in (("2026-11-10", 4), ("2026-11-11", None)):
            await client.post("/api/meal-plan", json={"date": day, "recipeId": recipe["id"], "servings": servings})
        lists = (await client.get("/api/shopping/lists")).json()["items"]
        list_id = lists[0]["id"]
        await client.post("/api/shopping/items", json={"listId": list_id, "name": "lentils", "unit": "CUP", "quantity": 0.5})
        result = (await client.post("/api/meal-plan/generate-shopping", json={"from": "2026-11-09", "to": "2026-11-15", "listId": list_id})).json()
        self.assertEqual((result["added"], result["merged"], result["recipeCount"]), (1, 1, 1))
        items = {i["name"]: i for i in (await client.get(f"/api/shopping/items?listId={list_id}")).json()["items"]}
        self.assertEqual(items["lentils"]["quantity"], 3.5)  # 0.5 + 1×(4/2) + 1×1
        self.assertEqual(items["Salt"]["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
