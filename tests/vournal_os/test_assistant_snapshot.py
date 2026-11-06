"""Undo for the assistant's deletes (design 2026-11-06): a copy of the row, of what the database
deletes with it and of the links it clears, restored under the same ids, all or nothing."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from prisma import Json

from app.os import assistant_snapshot as snap
from app.os.assistant_tools import ToolError
from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase


class Snapshots(ChatBase):
    async def subtasks(self):
        parent = (await self.client.post("/api/tasks", json={"title": "Move house"})).json()
        kids = [(await self.client.post("/api/tasks", json={"title": t, "parentId": parent["id"]})).json() for t in ("Pack", "Clean")]
        return parent, kids

    async def test_relations_come_from_the_database(self):
        graph = await snap._graph()
        self.assertIn(("Task", "parentId", "c"), graph["Task"])
        self.assertIn(("DebtPayment", "debtId", "c"), graph["Debt"])
        self.assertIn(("Transaction", "categoryId", "n"), graph["Category"])
        self.assertIn(("Task", "projectId", "n"), graph["Project"])

    async def test_snapshot_and_restore_a_task_with_subtasks(self):
        parent, kids = await self.subtasks()
        copy = await snap.take("Task", parent["id"], self.client.user.id)
        self.assertEqual((copy["count"], copy["has_file"]), (3, False))
        await self.client.delete(f"/api/tasks/{parent['id']}")
        self.assertEqual((await self.client.get(f"/api/tasks/{kids[0]['id']}")).status_code, 404)
        await snap.restore(copy)
        for t in (parent, *kids):
            back = (await self.client.get(f"/api/tasks/{t['id']}")).json()
            self.assertEqual((back["id"], back["title"], back.get("parentId")), (t["id"], t["title"], t.get("parentId")))

    async def test_restore_relinks_cleared_links(self):
        food = (await self.client.post("/api/categories", json={"scope": "EXPENSE", "name": "Snack runs"})).json()
        spent = [(await self.client.post("/api/transactions", json={"type": "EXPENSE", "amount": a, "categoryId": food["id"]})).json() for a in (5, 7)]
        copy = await snap.take("Category", food["id"], self.client.user.id)
        self.assertEqual(sorted(x["id"] for x in copy["relinks"]), sorted(t["id"] for t in spent))
        await self.client.delete(f"/api/categories/{food['id']}")
        self.assertIsNone((await self.client.get(f"/api/transactions/{spent[0]['id']}")).json()["categoryId"])
        await snap.restore(copy)
        for t in spent:
            self.assertEqual((await self.client.get(f"/api/transactions/{t['id']}")).json()["categoryId"], food["id"])

    async def test_restore_refuses_on_clash_and_changes_nothing(self):
        parent, kids = await self.subtasks()
        copy = await snap.take("Task", parent["id"], self.client.user.id)
        await self.client.delete(f"/api/tasks/{parent['id']}")
        # something now holds one of the copied ids
        await self.db.task.create(data={"id": kids[1]["id"], "userId": self.client.user.id, "title": "Someone else"})
        with self.assertRaises(ToolError):
            await snap.restore(copy)
        self.assertEqual((await self.client.get(f"/api/tasks/{parent['id']}")).status_code, 404)  # the parent didn't come back alone
        self.assertEqual((await self.client.get(f"/api/tasks/{kids[0]['id']}")).status_code, 404)

    async def test_snapshot_keeps_encrypted_fields_encrypted(self):
        doc = (await self.client.post("/api/documents", json={"name": "Passport", "type": "PASSPORT", "documentNumber": "P1234567",
                                                              "notes": "kept in the safe"})).json()
        row = await self.db.document.find_unique(where={"id": doc["id"]})
        copy = await snap.take("Document", doc["id"], self.client.user.id)
        self.assertEqual(copy["rows"][0]["row"]["documentNumberEnc"], row.documentNumberEnc)
        text = json.dumps(copy)
        self.assertNotIn("P1234567", text)
        self.assertNotIn("kept in the safe", text)

    async def test_an_attachment_marks_the_copy_as_having_a_file(self):
        parent, _ = await self.subtasks()
        await self.db.attachment.create(data={"userId": self.client.user.id, "entityType": "TASK", "entityId": parent["id"], "fileName": "a.pdf",
                                              "mimeType": "application/pdf", "size": 3, "storageKey": "k/a.pdf"})
        self.assertTrue((await snap.take("Task", parent["id"], self.client.user.id))["has_file"])

    async def test_another_users_row_cannot_be_copied(self):
        stranger = await self.signed_in()
        theirs = (await stranger.post("/api/tasks", json={"title": "Theirs"})).json()
        with self.assertRaises(ToolError):
            await snap.take("Task", theirs["id"], self.client.user.id)

    async def test_a_confirmed_delete_can_be_undone(self):
        parent, kids = await self.subtasks()
        fm.script(fm.tools(("delete_item", {"type": "task", "id": parent["id"], "label": "Move house"})), fm.message(fm.text("Confirm below.")))
        pending = (await self.say("delete the move"))["messages"][1]["actions"][0]
        done = (await self.client.post(f"/api/assistant/actions/{pending['id']}/approve")).json()
        self.assertEqual((done["status"], done["canUndo"]), ("done", True))
        self.assertEqual((await self.client.post(f"/api/assistant/actions/{pending['id']}/undo")).status_code, 200)
        self.assertEqual([(await self.client.get(f"/api/tasks/{t['id']}")).status_code for t in (parent, *kids)], [200, 200, 200])

    async def test_old_undo_copies_are_cleared(self):
        from app.os import jobs

        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Added.")))
        action = (await self.say("add gym"))["messages"][1]["actions"][0]
        await self.db.assistantaction.update(where={"id": action["id"]}, data={"createdAt": datetime.now(timezone.utc) - timedelta(hours=25)})
        await jobs.assistant_undo_cleanup()
        self.assertIsNone((await self.db.assistantaction.find_unique(where={"id": action["id"]})).undo)
        refused = await self.client.post(f"/api/assistant/actions/{action['id']}/undo")
        self.assertEqual((refused.status_code, refused.json()["message"]), (400, "This cannot be undone"))


if __name__ == "__main__":
    unittest.main()
