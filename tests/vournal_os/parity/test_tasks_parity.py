"""Tasks answer like NestJS did. The account's timezone is Beirut, so all-day dates,
"overdue" and repeats are worked out on Beirut's wall clock on both sides."""

import unittest

from vournal_os.parity.harness import ParityCase


class TasksParity(ParityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def make(self, body):
        nest, py = await self.both("POST", "/tasks", body)
        self.assertParity(nest, py)
        return nest.json()["id"], py.json()["id"]

    async def on_both(self, method, ids, suffix="", json=None):
        kwargs = {} if json is None else {"json": json}
        return (
            await self.nest.request(method, f"/tasks/{ids[0]}{suffix}", **kwargs),
            await self.py.request(method, f"/api/tasks/{ids[1]}{suffix}", **kwargs),
        )

    async def test_task_crud_and_list_filters(self):
        await self.make({"title": "  Pay rent ", "priority": "HIGH", "dueAt": "2026-11-05", "lifeArea": "FINANCE", "notes": "**now**"})
        await self.make({"title": "Call mum", "dueAt": "2026-11-04T16:30:00.000Z", "estimatedMinutes": 15})
        await self.make({"title": "Overdue thing", "dueAt": "2020-01-01"})
        ids = await self.make({"title": "Someday"})
        for query in (
            "", "?status=TODO", "?status=OVERDUE", "?status=TODO,OVERDUE", "?overdue=true", "?overdue=false",
            "?priority=HIGH,URGENT", "?hasDue=true", "?hasDue=false", "?dueFrom=2026-11-04&dueTo=2026-11-05",
            "?q=rent", "?sort=priority", "?sort=title&order=desc", "?sort=createdAt", "?topLevel=true",
            "?lifeArea=FINANCE", "?page=2&pageSize=2", "?status=NOPE", "?overdue=maybe", "?pageSize=500", "?sort=nope",
        ):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/tasks{query}"))
        self.assertParity(*await self.on_both("GET", ids))
        self.assertParity(*await self.on_both("PATCH", ids, json={"title": "Someday soon", "description": "x", "dueAt": "2026-11-10", "status": "IN_PROGRESS"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"dueAt": "2026-11-10T00:00:00.000Z"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"dueAt": None, "description": None}))
        self.assertParity(*await self.on_both("DELETE", ids))
        self.assertParity(*await self.on_both("GET", ids))

    async def test_subtasks(self):
        ids = await self.make({"title": "Move house", "subtasks": [{"title": "Boxes"}, {"title": " Van ", "priority": "URGENT"}]})
        self.assertParity(*await self.both("GET", "/tasks?topLevel=true"))
        nest_kids = (await self.nest.get(f"/tasks?parentId={ids[0]}")).json()["items"]
        py_kids = (await self.py.get(f"/api/tasks?parentId={ids[1]}")).json()["items"]
        self.assertEqual(len(nest_kids), len(py_kids))
        kid = (nest_kids[0]["id"], py_kids[0]["id"])
        self.assertParity(*await self.on_both("POST", kid, "/complete"))
        self.assertParity(*await self.on_both("GET", ids))
        child = await self.make({"title": "Sub of sub?", "parentId": None})
        self.assertParity(
            await self.nest.post("/tasks", json={"title": "Grandchild", "parentId": kid[0]}),
            await self.py.post("/api/tasks", json={"title": "Grandchild", "parentId": kid[1]}),
        )
        self.assertParity(*await self.on_both("PATCH", ids, json={"parentId": None}))
        self.assertParity(
            await self.nest.patch(f"/tasks/{ids[0]}", json={"parentId": child[0]}),
            await self.py.patch(f"/api/tasks/{ids[1]}", json={"parentId": child[1]}),
        )
        self.assertParity(
            await self.nest.patch(f"/tasks/{ids[0]}", json={"parentId": ids[0]}),
            await self.py.patch(f"/api/tasks/{ids[1]}", json={"parentId": ids[1]}),
        )
        self.assertParity(*await self.on_both("DELETE", ids))
        self.assertParity(*await self.both("GET", "/tasks"))

    async def test_tags_on_tasks(self):
        tag_nest = (await self.nest.post("/tags", json={"name": "home"})).json()["id"]
        tag_py = (await self.py.post("/api/tags", json={"name": "home"})).json()["id"]
        nest = await self.nest.post("/tasks", json={"title": "Fix tap", "tagIds": [tag_nest]})
        py = await self.py.post("/api/tasks", json={"title": "Fix tap", "tagIds": [tag_py]})
        self.assertParity(nest, py)
        self.assertParity(*await self.both("GET", "/tags"))
        self.assertParity(
            await self.nest.get(f"/tasks?tagId={tag_nest}"), await self.py.get(f"/api/tasks?tagId={tag_py}")
        )
        ids = (nest.json()["id"], py.json()["id"])
        self.assertParity(*await self.on_both("PATCH", ids, json={"tagIds": []}))
        self.assertParity(*await self.both("POST", "/tasks", {"title": "x", "tagIds": ["cnosuchtagidatallxxxxxx"]}))

    async def test_complete_repeating_task_creates_next_once(self):
        for body in (
            {"title": "Gym", "rrule": "FREQ=WEEKLY;BYDAY=MO,TH", "dueAt": "2026-11-02T16:00:00.000Z"},
            {"title": "Bills", "rrule": "FREQ=MONTHLY", "dueAt": "2026-01-31"},
            {"title": "Water", "rrule": "FREQ=DAILY", "dueAt": "2020-03-01"},
            {"title": "Ends", "rrule": "FREQ=DAILY", "dueAt": "2026-11-02", "recurrenceEndAt": "2026-11-02"},
            {"title": "Undated", "rrule": "FREQ=WEEKLY"},
        ):
            with self.subTest(body["title"]):
                ids = await self.make(body)
                self.assertParity(*await self.on_both("POST", ids, "/complete"))
                self.assertParity(*await self.on_both("POST", ids, "/reopen"))
                self.assertParity(*await self.on_both("POST", ids, "/complete"))
                self.assertParity(*await self.both("GET", "/tasks?sort=createdAt"))

    async def test_status_changes_through_patch(self):
        ids = await self.make({"title": "Read", "rrule": "FREQ=DAILY", "dueAt": "2026-11-03"})
        self.assertParity(*await self.on_both("PATCH", ids, json={"status": "COMPLETED"}))
        self.assertParity(*await self.on_both("PATCH", ids, json={"status": "TODO"}))
        self.assertParity(*await self.both("GET", "/tasks?sort=createdAt"))

    async def test_focus(self):
        a = await self.make({"title": "A", "priority": "LOW"})
        b = await self.make({"title": "B", "priority": "URGENT"})
        self.assertParity(*await self.both("GET", "/tasks/focus"))
        self.assertParity(
            await self.nest.put("/tasks/focus", json={"date": "2026-11-04", "taskIds": [a[0], b[0], a[0]]}),
            await self.py.put("/api/tasks/focus", json={"date": "2026-11-04", "taskIds": [a[1], b[1], a[1]]}),
        )
        self.assertParity(*await self.both("GET", "/tasks/focus?date=2026-11-04"))
        for body in ({"taskIds": ["a", "b", "c", "d"]}, {"date": "nope", "taskIds": []}, {}):
            self.assertParity(*await self.both("PUT", "/tasks/focus", body))

    async def test_reorder(self):
        a = await self.make({"title": "A"})
        b = await self.make({"title": "B", "rrule": "FREQ=DAILY", "dueAt": "2026-11-03"})
        self.assertParity(
            await self.nest.post("/tasks/reorder", json={"items": [{"id": a[0], "position": 5}, {"id": b[0], "position": 1.5, "status": "COMPLETED"}]}),
            await self.py.post("/api/tasks/reorder", json={"items": [{"id": a[1], "position": 5}, {"id": b[1], "position": 1.5, "status": "COMPLETED"}]}),
        )
        self.assertParity(*await self.both("GET", "/tasks?sort=position"))
        self.assertParity(*await self.both("POST", "/tasks/reorder", {"items": []}))

    async def test_validation_shapes(self):
        for body in (
            {}, {"title": ""}, {"title": "x" * 201}, {"title": "x", "status": "DONE"}, {"title": "x", "priority": "MAX"},
            {"title": "x", "dueAt": "tomorrow"}, {"title": "x", "rrule": "FREQ=MINUTELY"}, {"title": "x", "rrule": "BYDAY=MO"},
            {"title": "x", "estimatedMinutes": -1}, {"title": "x", "tagIds": "a"}, {"title": "x", "extra": 1},
            {"title": "x", "timeAnchor": {"start": {"prayer": "asr", "offset": 10}}},
            {"title": "x", "timeAnchor": {"start": {"prayer": "noon", "offset": 10}}},
            {"title": "x", "subtasks": [{"title": ""}]},
        ):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/tasks", body))
        ids = await self.make({"title": "Patch me"})
        for body in ({"subtasks": []}, {"title": None}, {"status": "NOPE"}):
            self.assertParity(*await self.on_both("PATCH", ids, json=body))


if __name__ == "__main__":
    unittest.main()
