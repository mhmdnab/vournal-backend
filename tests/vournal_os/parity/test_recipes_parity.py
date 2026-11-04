"""Recipes answer like NestJS did: ingredients (replaced as a set), favourites first,
search through ingredient names, total minutes, validation."""

import unittest

from vournal_os.parity.harness import ParityCase


class RecipesParity(ParityCase):
    maxDiff = None

    async def make(self, body):
        nest, py = await self.both("POST", "/recipes", body)
        self.assertParity(nest, py)
        return nest.json().get("id"), py.json().get("id")

    async def on_both(self, method, ids, json=None):
        kwargs = {} if json is None else {"json": json}
        return await self.nest.request(method, f"/recipes/{ids[0]}", **kwargs), await self.py.request(method, f"/api/recipes/{ids[1]}", **kwargs)

    async def test_recipes(self):
        soup = await self.make({"name": " Lentil soup ", "servings": 4, "prepMinutes": 10, "cookMinutes": 35, "sourceUrl": "https://example.com/soup",
                                "ingredients": [{"name": " Lentils ", "quantity": 1.5, "unit": "cup"}, {"name": "Onion", "quantity": 1, "unit": ""}, {"name": "Salt", "sortOrder": 9}]})
        await self.make({"name": "Toast", "isFavorite": True, "prepMinutes": 3})
        await self.make({"name": "Salad"})
        for query in ("", "?favorite=true", "?q=onion", "?q=TOAST", "?favorite=maybe", "?bogus=1"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/recipes{query}"))
        self.assertParity(*await self.on_both("GET", soup))
        for body in ({"ingredients": [{"name": "Red lentils", "quantity": 2}]}, {"ingredients": []}, {"cookMinutes": None, "sourceUrl": ""},
                     {"isFavorite": None, "servings": 6}, {"name": None}):
            with self.subTest(body=body):
                self.assertParity(*await self.on_both("PATCH", soup, body))
        self.assertParity(*await self.on_both("DELETE", soup))
        self.assertParity(*await self.on_both("GET", soup))

    async def test_validation(self):
        for body in ({}, {"name": "x", "servings": 0}, {"name": "x", "sourceUrl": "example.com"}, {"name": "x", "sourceUrl": "ftp://example.com"},
                     {"name": "x", "ingredients": [{"name": ""}]}, {"name": "x", "ingredients": [{"name": "a", "quantity": -1}]}, {"name": "x", "extra": 1}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/recipes", body))


if __name__ == "__main__":
    unittest.main()
