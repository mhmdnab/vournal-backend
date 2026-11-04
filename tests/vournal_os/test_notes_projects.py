"""Notes and projects: excerpts as vournalOS made them (golden/notes.json), ownership,
the NOTE category rule, and deleting a project with or without its tasks."""

import json
import pathlib
import unittest

from vournal_os.support import OsTestCase

GOLDEN = json.loads((pathlib.Path(__file__).parent / "golden" / "notes.json").read_text())


class Excerpts(unittest.TestCase):
    def test_every_golden_case(self):
        from app.os.notes import make_excerpt, word_count

        for case in GOLDEN:
            with self.subTest(md=case["md"][:40]):
                self.assertEqual(make_excerpt(case["md"]), case["excerpt"])
                self.assertEqual(word_count(case["md"]), case["wordCount"])


class NotesProjects(OsTestCase):
    async def test_other_users_rows_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        note = (await owner.post("/api/notes", json={"title": "Mine"})).json()
        project = (await owner.post("/api/projects", json={"name": "Mine"})).json()
        for method, path, message in (
            ("GET", f"/api/notes/{note['id']}", "Note not found"), ("PATCH", f"/api/notes/{note['id']}", "Note not found"),
            ("DELETE", f"/api/notes/{note['id']}", "Note not found"), ("GET", f"/api/projects/{project['id']}", "Project not found"),
            ("PATCH", f"/api/projects/{project['id']}", "Project not found"), ("DELETE", f"/api/projects/{project['id']}", "Project not found"),
        ):
            response = await other.request(method, path, json={} if method == "PATCH" else None)
            self.assertEqual((response.status_code, response.json()["message"]), (404, message), f"{method} {path}")
        linked = await other.post("/api/notes", json={"title": "x", "projectId": project["id"]})
        self.assertEqual(linked.status_code, 404)

    async def test_note_category_must_be_a_note_category(self):
        client = await self.signed_in()
        expense = await self.db.category.find_first(where={"userId": client.user.id, "scope": "EXPENSE"})
        response = await client.post("/api/notes", json={"title": "x", "categoryId": expense.id})
        self.assertEqual((response.status_code, response.json()["message"]), (400, "Category must be a NOTE category"))

    async def test_deleting_a_project_keeps_or_deletes_its_tasks(self):
        client = await self.signed_in()
        for delete_tasks in (False, True):
            project = (await client.post("/api/projects", json={"name": f"P{delete_tasks}"})).json()
            task = (await client.post("/api/tasks", json={"title": "t", "projectId": project["id"]})).json()
            query = "?deleteTasks=true" if delete_tasks else ""
            self.assertEqual((await client.delete(f"/api/projects/{project['id']}{query}")).json(), {"ok": True})
            kept = await self.db.task.find_unique(where={"id": task["id"]})
            self.assertEqual(kept is None, delete_tasks)
            if kept:
                self.assertIsNone(kept.projectId)


if __name__ == "__main__":
    unittest.main()
