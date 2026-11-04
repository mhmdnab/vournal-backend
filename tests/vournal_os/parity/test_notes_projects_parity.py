"""Notes and projects answer like NestJS did: tags and links, filters and sorting,
excerpts, pins and archives; project progress, budget against spending, deadlines."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.parity.harness import ParityCase, copy_reference_market_rates


class NotesProjectsParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def pair(self, method, path, body=None, ids=None):
        """The same request on both sides, with per-side ids put into the body."""
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            payload = {**(body or {}), **{k: v[i] if isinstance(v, tuple) else [x[i] for x in v] for k, v in (ids or {}).items()}}
            out.append(await client.request(method, f"{prefix}{path}", json=payload if method != "GET" else None))
        self.assertParity(*out)
        return tuple(r.json().get("id") if isinstance(r.json(), dict) else None for r in out)

    async def on_both(self, method, base, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, f"{base}/{ids[0]}{suffix}", **kwargs),
            await self.py.request(method, f"/api{base}/{ids[1]}{suffix}", **kwargs),
        )

    async def note_category(self):
        return await self.pair("POST", "/categories", {"scope": "NOTE", "name": "Ideas"})

    async def test_notes(self):
        cat = await self.note_category()
        red, blue = await self.pair("POST", "/tags", {"name": "red"}), await self.pair("POST", "/tags", {"name": "blue"})
        project = await self.pair("POST", "/projects", {"name": "House"})
        long = "# Plan\n\n" + "- [ ] **buy** paint and `brushes`\n" * 20
        a = await self.pair("POST", "/notes", {"title": " Plan ", "content": long, "isPinned": True}, {"categoryId": cat, "tagIds": [red, blue], "projectId": project})
        await self.pair("POST", "/notes", {"title": "Groceries", "content": "milk eggs", "isFavorite": True}, {"tagIds": [blue]})
        await self.pair("POST", "/notes", {"title": "Old", "isArchived": True})
        for query in ("", "?archived=true", "?favorite=true", "?pinned=false", "?q=PAINT", "?sort=title", "?sort=createdAt&pageSize=1&page=2", "?sort=size"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/notes{query}"))
        self.assertParity(await self.nest.get(f"/notes?tagId={red[0]}"), await self.py.get(f"/api/notes?tagId={red[1]}"))
        self.assertParity(await self.nest.get(f"/notes?projectId={project[0]}"), await self.py.get(f"/api/notes?projectId={project[1]}"))
        self.assertParity(*await self.on_both("GET", "/notes", a))
        for body in ({"isArchived": True}, {"isPinned": False}, {"isFavorite": True}, {"content": None, "color": "#ff0000"}, {"tagIds": []}, {"title": None}, {"categoryId": None, "isPinned": None}):
            with self.subTest(body=body):
                self.assertParity(*await self.on_both("PATCH", "/notes", a, json=body))
        self.assertParity(*await self.on_both("DELETE", "/notes", a))
        self.assertParity(*await self.on_both("GET", "/notes", a))

    async def test_note_validation(self):
        for body in ({}, {"title": ""}, {"title": "x" * 201}, {"title": "x", "color": "red"}, {"title": "x", "tagIds": "a"},
                     {"title": "x", "extra": 1}, {"title": "x", "projectId": "cnosuchprojectxxxxxxxxxxxx"}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/notes", body))
        expense = await self.pair("POST", "/categories", {"scope": "EXPENSE", "name": "Stuff"})
        await self.pair("POST", "/notes", {"title": "x"}, {"categoryId": expense})

    async def test_projects(self):
        await self.both("PUT", "/exchange-rates", {"rates": {"LBP": 89500}})
        soon = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
        house = await self.pair("POST", "/projects", {"name": " House ", "budget": 1000, "deadline": soon, "lifeArea": "HOME", "color": "#00aa00", "icon": "house"})
        await self.pair("POST", "/projects", {"name": "Thesis", "status": "PLANNING", "deadline": past, "progressOverride": 40})
        await self.pair("POST", "/projects", {"name": "Done thing", "status": "COMPLETED"})
        for title, extra in (("Paint", {"dueAt": soon}), ("Tiles", {"dueAt": past}), ("Plumber", {"status": "IN_PROGRESS"}), ("Keys", {"status": "COMPLETED"})):
            task = await self.pair("POST", "/tasks", {"title": title, **extra}, {"projectId": house})
            if title == "Paint":
                await self.pair("POST", "/tasks", {"title": "Primer", "dueAt": soon}, {"projectId": house, "parentId": task})
        await self.pair("POST", "/transactions", {"type": "EXPENSE", "amount": 250.5, "title": "Paint"}, {"projectId": house})
        await self.pair("POST", "/transactions", {"type": "EXPENSE", "amount": 8950000, "currency": "LBP", "title": "Tiles"}, {"projectId": house})
        await self.pair("POST", "/events", {"title": "Visit", "startAt": f"{soon}T10:00:00.000Z"}, {"projectId": house})
        await self.pair("POST", "/notes", {"title": "Brief"}, {"projectId": house})
        for query in ("", "?status=ACTIVE,PLANNING", "?lifeArea=HOME", "?q=hou", "?sort=name", "?sort=updatedAt", "?status=DONE", "?sort=size"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/projects{query}"))
        self.assertParity(*await self.on_both("GET", "/projects", house))
        for body in ({"status": "COMPLETED"}, {"status": "ACTIVE", "budget": 100}, {"budget": None, "deadline": None}, {"progressOverride": 120}, {"name": None}):
            with self.subTest(body=body):
                self.assertParity(*await self.on_both("PATCH", "/projects", house, json=body))
        self.assertParity(*await self.on_both("DELETE", "/projects", house, "?deleteTasks=maybe"))
        self.assertParity(*await self.on_both("DELETE", "/projects", house, "?deleteTasks=true"))
        self.assertParity(*await self.both("GET", "/tasks"))


if __name__ == "__main__":
    unittest.main()
