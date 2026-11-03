"""Tasks never touch another user's rows, and log what they do."""

import unittest

from vournal_os.support import OsTestCase


class Tasks(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.task.create(data={"userId": other.id, "title": "Theirs"})
        for method, path, body in (
            ("GET", f"/api/tasks/{theirs.id}", None),
            ("PATCH", f"/api/tasks/{theirs.id}", {"title": "Mine"}),
            ("POST", f"/api/tasks/{theirs.id}/complete", None),
            ("POST", f"/api/tasks/{theirs.id}/reopen", None),
            ("DELETE", f"/api/tasks/{theirs.id}", None),
        ):
            response = await client.request(method, path, json=body)
            self.assertEqual(response.status_code, 404, f"{method} {path}")
            self.assertEqual(response.json()["message"], "Task not found")
        for path, body in (
            ("/api/tasks/reorder", {"items": [{"id": theirs.id, "position": 1}]}),
            ("/api/tasks", {"title": "Sub", "parentId": theirs.id}),
        ):
            self.assertEqual((await client.post(path, json=body)).status_code, 404)
        focus = await client.put("/api/tasks/focus", json={"taskIds": [theirs.id]})
        self.assertEqual(focus.status_code, 404)
        row = await self.db.task.find_unique(where={"id": theirs.id})
        self.assertEqual((row.title, row.status, row.focusDate), ("Theirs", "TODO", None))

    async def test_task_activity_logged(self):
        client = await self.signed_in()
        made = (await client.post("/api/tasks", json={"title": "Water plants", "rrule": "FREQ=DAILY", "dueAt": "2026-11-03"})).json()
        await client.patch(f"/api/tasks/{made['id']}", json={"priority": "HIGH"})
        done = (await client.post(f"/api/tasks/{made['id']}/complete")).json()
        await client.post(f"/api/tasks/{made['id']}/reopen")
        await client.delete(f"/api/tasks/{made['id']}")
        rows = await self.db.activitylog.find_many(where={"userId": client.user.id}, order={"createdAt": "asc"})
        self.assertEqual(
            [r.action for r in rows],
            ["task.created", "task.updated", "task.created", "task.completed", "task.reopened", "task.deleted"],
        )
        self.assertEqual(rows[0].entityType, "TASK")
        self.assertEqual(rows[0].metadata, {"title": "Water plants", "projectId": None, "parentId": None})
        self.assertEqual(rows[3].metadata["nextOccurrenceId"], done["nextOccurrence"]["id"])


if __name__ == "__main__":
    unittest.main()
