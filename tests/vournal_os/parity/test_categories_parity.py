"""Categories answer like NestJS did, starting from the data seeded at sign-up."""

import unittest

from vournal_os.parity.harness import ParityCase


class CategoriesParity(ParityCase):
    async def _ids(self, name: str, scope: str = "EXPENSE") -> tuple[str, str]:
        """The id of the category called `name` on each backend."""
        found = []
        for client, prefix in ((self.nest, ""), (self.py, "/api")):
            items = (await client.get(f"{prefix}/categories", params={"scope": scope})).json()["items"]
            found.append(next(c["id"] for c in items if c["name"] == name))
        return found[0], found[1]

    async def both_on(self, method: str, paths: tuple[str, str], json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, paths[0], **kwargs),
            await self.py.request(method, f"/api{paths[1]}", **kwargs),
        )

    async def test_default_categories_match_signup(self):
        nest, py = await self.both("GET", "/categories")
        self.assertParity(nest, py)
        self.assertGreater(py.json()["total"], 50)

    async def test_scope_filter(self):
        for query in ("?scope=EXPENSE,INCOME", "?scope=TASK&scope=EVENT", "?scope=NOPE", "?scope="):
            nest, py = await self.both("GET", f"/categories{query}")
            self.assertParity(nest, py)

    async def test_create_get_update_delete(self):
        nest, py = await self.both("POST", "/categories", {"scope": "EXPENSE", "name": "  Pets ", "color": "#aabbcc", "icon": "paw-print"})
        self.assertParity(nest, py)
        paths = (f"/categories/{nest.json()['id']}", f"/categories/{py.json()['id']}")
        self.assertParity(*await self.both_on("GET", paths))
        self.assertParity(*await self.both_on("PATCH", paths, {"name": "Animals", "color": None, "sortOrder": 3}))
        self.assertParity(*await self.both_on("PATCH", paths, {"scope": "INCOME"}))
        self.assertParity(*await self.both_on("DELETE", paths))
        self.assertParity(*await self.both_on("GET", paths))

    async def test_create_subcategory_and_errors(self):
        parent = await self._ids("Food")
        nest, py = (
            await self.nest.post("/categories", json={"scope": "EXPENSE", "name": "Coffee", "parentId": parent[0]}),
            await self.py.post("/api/categories", json={"scope": "EXPENSE", "name": "Coffee", "parentId": parent[1]}),
        )
        self.assertParity(nest, py)
        child = (nest.json()["id"], py.json()["id"])
        for body in (
            lambda i: {"scope": "INCOME", "name": "Coffee", "parentId": parent[i]},
            lambda i: {"scope": "EXPENSE", "name": "Beans", "parentId": child[i]},
            lambda i: {"scope": "EXPENSE", "name": "coffee", "parentId": parent[i]},
            lambda i: {"scope": "EXPENSE", "name": "x", "parentId": "cnosuchcategoryidatall00"},
        ):
            self.assertParity(
                await self.nest.post("/categories", json=body(0)),
                await self.py.post("/api/categories", json=body(1)),
            )
        self.assertParity(
            await self.nest.patch(f"/categories/{parent[0]}", json={"parentId": parent[0]}),
            await self.py.patch(f"/api/categories/{parent[1]}", json={"parentId": parent[1]}),
        )
        nest, py = await self.both("GET", "/categories?scope=EXPENSE")
        self.assertParity(nest, py)

    async def test_reorder(self):
        ids = [await self._ids(name) for name in ("Food", "Bills", "Other")]
        order = [40, 10, 20]
        nest, py = (
            await self.nest.post("/categories/reorder", json={"items": [{"id": i[0], "sortOrder": o} for i, o in zip(ids, order)]}),
            await self.py.post("/api/categories/reorder", json={"items": [{"id": i[1], "sortOrder": o} for i, o in zip(ids, order)]}),
        )
        self.assertParity(nest, py)
        self.assertParity(*await self.both("GET", "/categories?scope=EXPENSE"))
        self.assertParity(*await self.both("POST", "/categories/reorder", {"items": []}))
        self.assertParity(*await self.both("POST", "/categories/reorder", {"items": [{"id": "cnosuchcategoryidatall00", "sortOrder": 1}]}))

    async def test_validation_shapes(self):
        for body in (
            {"name": "No scope"},
            {"scope": "NOPE", "name": "x"},
            {"scope": "TASK", "name": "   "},
            {"scope": "TASK", "name": "x" * 61},
            {"scope": "TASK", "name": "x", "color": "red"},
            {"scope": "TASK", "name": "x", "icon": "Not An Icon"},
            {"scope": "TASK", "name": "x", "sortOrder": -1},
            {"scope": "TASK", "name": "x", "bogus": True},
        ):
            self.assertParity(*await self.both("POST", "/categories", body))

    async def test_arabic_defaults_after_language_switch(self):
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        self.assertParity(*await self.both("GET", "/categories?scope=TASK,SHOPPING"))


if __name__ == "__main__":
    unittest.main()
