"""The meal plan answers like NestJS did: weeks from the user's week start in Beirut
time, entries with and without recipes, copying a week, building a shopping list."""

import unittest

from vournal_os.parity.harness import ParityCase


class MealPlanParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut", "weekStartsOn": 6}))

    async def pair(self, method, paths, body=None, ids=None):
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            payload = None if body is None and not ids else {**(body or {}), **{k: v[i] for k, v in (ids or {}).items()}}
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}{p}", json=payload))
        self.assertParity(*out)
        return tuple(r.json().get("id") if isinstance(r.json(), dict) else None for r in out)

    async def test_plan(self):
        soup = await self.pair("POST", "/recipes", {"name": "Soup", "servings": 2, "ingredients": [
            {"name": "Lentils", "quantity": 1.25, "unit": "cup"}, {"name": " onion ", "quantity": 1}, {"name": "Salt"}]})
        salad = await self.pair("POST", "/recipes", {"name": "Salad", "ingredients": [{"name": "Onion", "quantity": 0.5}, {"name": "Tomato", "quantity": 3}]})
        e1 = await self.pair("POST", "/meal-plan", {"date": "2026-11-09", "mealType": "LUNCH", "servings": 3}, {"recipeId": soup})
        await self.pair("POST", "/meal-plan", {"date": "2026-11-10"}, {"recipeId": salad})
        await self.pair("POST", "/meal-plan", {"date": "2026-11-10", "title": " Pizza night ", "notes": "order"})
        await self.pair("POST", "/meal-plan", {"date": "2026-11-11"})
        for query in ("", "?from=2026-11-07", "?from=2026-11-07&to=2026-11-13", "?from=2026-11-13&to=2026-11-07", "?from=2025-01-01&to=2026-11-13", "?from=nope"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/meal-plan{query}"))
        await self.pair("PATCH", (f"/meal-plan/{e1[0]}", f"/meal-plan/{e1[1]}"), {}, {"recipeId": salad})
        await self.pair("PATCH", (f"/meal-plan/{e1[0]}", f"/meal-plan/{e1[1]}"), {"title": None, "mealType": None, "servings": None})
        await self.pair("PATCH", (f"/meal-plan/{e1[0]}", f"/meal-plan/{e1[1]}"), {"recipeId": None, "date": "2026-11-12"})
        for body in ({"fromWeekStart": "2026-11-07", "toWeekStart": "2026-11-14"}, {"fromWeekStart": "2026-11-07", "toWeekStart": "2026-11-14"},
                     {"fromWeekStart": "2026-11-07", "toWeekStart": "2026-11-14", "replace": True}, {"fromWeekStart": "2026-11-07", "toWeekStart": "2026-11-07"}):
            with self.subTest(body=body):
                await self.pair("POST", "/meal-plan/copy-week", body)
        await self.pair("POST", "/shopping/items", {"name": "TOMATO", "quantity": 1})
        await self.pair("POST", "/meal-plan/generate-shopping", {"from": "2026-11-07", "to": "2026-11-20"})
        self.assertParity(*await self.both("GET", "/shopping/items"))
        await self.pair("POST", "/meal-plan/generate-shopping", {"from": "2027-01-01", "to": "2027-01-07"})
        self.assertParity(*await self.both("GET", "/meal-plan?from=2026-11-14"))
        await self.pair("DELETE", (f"/meal-plan/{e1[0]}", f"/meal-plan/{e1[1]}"))

    async def test_validation(self):
        for body in ({}, {"date": "2026-11-09"}, {"date": "x", "title": "a"}, {"date": "2026-11-09", "title": "a", "mealType": "BRUNCH"},
                     {"date": "2026-11-09", "title": "a", "servings": 0}, {"date": "2026-11-09", "recipeId": "cnosuchrecipexxxxxxxxxxxxx"},
                     {"date": "2026-11-09", "title": "Dinner", "recipeId": "cnosuchrecipexxxxxxxxxxxxx"}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/meal-plan", body))
        self.assertParity(*await self.both("POST", "/meal-plan/generate-shopping", {}))


if __name__ == "__main__":
    unittest.main()
