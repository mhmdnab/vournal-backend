"""The assistant's meal planner and recipe tools (design 2026-11-18): plan a meal, change it, add
the planned recipes' ingredients to a shopping list, add and change recipes. Each one goes through
the section's own route, says what it did, and (except the shopping list) can be undone."""

from vournal_os.assistant_life_base import LifeBase


class MealTools(LifeBase):
    async def recipe(self, name="Lentil soup", **data):
        return await self.post("/api/recipes", {"name": name, **data})

    async def test_plan_meal_with_title(self):
        a = await self.act("plan_meal", {"date": "2026-11-20", "meal": "DINNER", "title": "Lasagne"})
        self.assertEqual((a["kind"], a["entityType"], a["label"], a["link"]), ("create", "meal_plan_entry", "Lasagne", "/meals?date=2026-11-20"))
        e = await self.db.mealplanentry.find_unique(where={"id": a["entityId"]})
        self.assertEqual((e.mealType, e.title, e.date.date().isoformat()), ("DINNER", "Lasagne", "2026-11-20"))
        await self.undo(a)
        self.assertIsNone(await self.db.mealplanentry.find_unique(where={"id": a["entityId"]}))

    async def test_plan_meal_with_recipe_takes_its_name(self):
        r = await self.recipe(servings=4)
        a = await self.act("plan_meal", {"date": "2026-11-21", "recipe_id": r["id"], "servings": 2})
        e = await self.db.mealplanentry.find_unique(where={"id": a["entityId"]})
        self.assertEqual((e.title, e.recipeId, e.servings, e.mealType), ("Lentil soup", r["id"], 2, "DINNER"))

    async def test_plan_meal_without_title_or_recipe_is_an_error(self):
        self.assertIn("title is required when no recipe is selected", await self.act_error("plan_meal", {"date": "2026-11-20"}))

    async def test_plan_meal_someone_elses_recipe_is_not_found(self):
        other = await self.signed_in()
        r = await other.post("/api/recipes", json={"name": "Theirs"})
        self.assertIn("not found", (await self.act_error("plan_meal", {"date": "2026-11-20", "recipe_id": r.json()["id"]})).lower())

    async def test_update_meal_plan_moves_and_undo_puts_back(self):
        e = await self.post("/api/meal-plan", {"date": "2026-11-20", "title": "Pasta", "mealType": "LUNCH"})
        a = await self.act("update_meal_plan", {"id": e["id"], "date": "2026-11-22", "meal": "DINNER"})
        self.assertEqual((a["kind"], a["entityType"], a["link"]), ("update", "meal_plan_entry", "/meals?date=2026-11-22"))
        row = await self.db.mealplanentry.find_unique(where={"id": e["id"]})
        self.assertEqual((row.date.date().isoformat(), row.mealType), ("2026-11-22", "DINNER"))
        await self.undo(a)
        row = await self.db.mealplanentry.find_unique(where={"id": e["id"]})
        self.assertEqual((row.date.date().isoformat(), row.mealType), ("2026-11-20", "LUNCH"))

    async def test_update_meal_plan_to_a_recipe_undo_restores_title(self):
        e = await self.post("/api/meal-plan", {"date": "2026-11-20", "title": "Something"})
        r = await self.recipe()
        a = await self.act("update_meal_plan", {"id": e["id"], "recipe_id": r["id"]})
        self.assertEqual((await self.db.mealplanentry.find_unique(where={"id": e["id"]})).title, "Lentil soup")
        await self.undo(a)
        row = await self.db.mealplanentry.find_unique(where={"id": e["id"]})
        self.assertEqual((row.title, row.recipeId), ("Something", None))

    async def test_update_meal_plan_undo_refused_after_change(self):
        e = await self.post("/api/meal-plan", {"date": "2026-11-20", "title": "Pasta"})
        a = await self.act("update_meal_plan", {"id": e["id"], "title": "Soup"})
        await self.db.mealplanentry.update(where={"id": e["id"]}, data={"title": "Changed on web"})
        self.assertIn("changed since", await self.undo_refused(a))
        self.assertEqual((await self.db.mealplanentry.find_unique(where={"id": e["id"]})).title, "Changed on web")

    async def test_update_meal_plan_nothing_to_change(self):
        e = await self.post("/api/meal-plan", {"date": "2026-11-20", "title": "Pasta"})
        self.assertIn("Nothing to change", await self.act_error("update_meal_plan", {"id": e["id"]}))

    async def test_shopping_from_meal_plan_adds_ingredients_no_undo(self):
        r = await self.recipe(servings=2, ingredients=[{"name": "lentils", "quantity": 200, "unit": "g"}, {"name": "onion", "quantity": 1}])
        await self.post("/api/meal-plan", {"date": "2026-11-20", "recipeId": r["id"], "servings": 4})
        a = await self.act("shopping_from_meal_plan", {"from": "2026-11-16", "to": "2026-11-22"})
        self.assertEqual((a["kind"], a["entityType"], a["canUndo"]), ("create", "shopping_item", False))
        items = await self.db.shoppingitem.find_many(where={"userId": self.uid})
        self.assertEqual(sorted((i.name, float(i.quantity)) for i in items), [("lentils", 400.0), ("onion", 2.0)])

    async def test_add_recipe_with_ingredients_and_undo(self):
        a = await self.act("add_recipe", {"name": "Hummus", "servings": 4, "prep_minutes": 10, "instructions": "Blend.",
                                          "ingredients": [{"name": "chickpeas", "quantity": 400, "unit": "g"}, {"name": "tahini"}]})
        self.assertEqual((a["kind"], a["entityType"], a["label"], a["link"]), ("create", "recipe", "Hummus", f"/meals?recipe={a['entityId']}"))
        r = await self.db.recipe.find_unique(where={"id": a["entityId"]}, include={"ingredients": True})
        self.assertEqual((r.servings, r.prepMinutes, sorted(i.name for i in r.ingredients)), (4, 10, ["chickpeas", "tahini"]))
        await self.undo(a)
        self.assertIsNone(await self.db.recipe.find_unique(where={"id": a["entityId"]}))

    async def test_add_recipe_bad_link_is_the_routes_error(self):
        self.assertIn("sourceUrl must be a URL address", await self.act_error("add_recipe", {"name": "X", "source_url": "not a link"}))

    async def test_update_recipe_ingredients_undo_restores_list(self):
        r = await self.recipe(ingredients=[{"name": "lentils", "quantity": 200, "unit": "g"}])
        a = await self.act("update_recipe", {"id": r["id"], "favorite": True, "ingredients": [{"name": "rice", "quantity": 1, "unit": "cup"}]})
        self.assertEqual((a["kind"], a["entityType"]), ("update", "recipe"))
        row = await self.db.recipe.find_unique(where={"id": r["id"]}, include={"ingredients": True})
        self.assertEqual((row.isFavorite, [i.name for i in row.ingredients]), (True, ["rice"]))
        await self.undo(a)
        row = await self.db.recipe.find_unique(where={"id": r["id"]}, include={"ingredients": True})
        self.assertEqual((row.isFavorite, [(i.name, float(i.quantity), i.unit) for i in row.ingredients]), (False, [("lentils", 200.0, "g")]))

    async def test_delete_meal_plan_entry_and_undo(self):
        e = await self.post("/api/meal-plan", {"date": "2026-11-20", "title": "Pasta"})
        a = await self.delete("meal_plan_entry", e["id"], "Pasta")
        self.assertEqual((a["status"], a["entityType"]), ("done", "meal_plan_entry"))
        self.assertIsNone(await self.db.mealplanentry.find_unique(where={"id": e["id"]}))
        await self.undo(a)
        self.assertIsNotNone(await self.db.mealplanentry.find_unique(where={"id": e["id"]}))

    async def test_delete_recipe_with_ingredients_asks_first(self):
        r = await self.recipe(ingredients=[{"name": "lentils"}])
        a = await self.delete("recipe", r["id"], "Lentil soup")
        self.assertEqual((a["status"], a["link"]), ("pending", f"/meals?recipe={r['id']}"))
        done = await self.approve(a)
        self.assertIsNone(await self.db.recipe.find_unique(where={"id": r["id"]}))
        await self.undo(done)
        self.assertEqual(await self.db.recipeingredient.count(where={"recipeId": r["id"]}), 1)

    async def test_meals_section_off_refuses(self):
        await self.client.patch("/api/settings", json={"enabledSections": ["tasks"]})
        self.assertEqual(await self.act_error("plan_meal", {"date": "2026-11-20", "title": "X"}), "Error: The Meals section is turned off in Settings.")
        self.assertEqual(await self.db.mealplanentry.count(where={"userId": self.uid}), 0)
