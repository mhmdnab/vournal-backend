"""Notes and files in a workspace (spec 2026-11-09-workspaces-design, phase 4).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from vournal_os.test_workspaces import WorkspaceCase


class WorkspaceNotesFiles(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.w, self.owner, self.admin, self.member_ = await self.team()
        self.ws = {"X-Vournal-Space": self.w}

    async def upload(self, client, entity_type, entity_id, headers):
        return await client.post(
            "/api/attachments",
            data={"entityType": entity_type, "entityId": entity_id},
            files={"file": ("plan.txt", b"the plan", "text/plain")},
            headers=headers,
        )

    async def test_note_shared_with_members(self):
        note = (await self.owner.post("/api/notes", json={"title": "Specs", "content": "# v1"}, headers=self.ws)).json()
        got = await self.member_.get(f"/api/notes/{note['id']}", headers=self.ws)
        self.assertEqual(got.status_code, 200, got.text)
        listed = (await self.member_.get("/api/notes", headers=self.ws)).json()
        self.assertIn(note["id"], [n["id"] for n in (listed["items"] if isinstance(listed, dict) else listed)])
        mine = (await self.owner.get("/api/notes")).json()
        self.assertNotIn(note["id"], [n["id"] for n in (mine["items"] if isinstance(mine, dict) else mine)])
        self.assertEqual((await self.member_.delete(f"/api/notes/{note['id']}", headers=self.ws)).status_code, 404)

    async def test_files_follow_their_item(self):
        task = (await self.owner.post("/api/tasks", json={"title": "With a file"}, headers=self.ws)).json()
        up = await self.upload(self.owner, "TASK", task["id"], self.ws)
        self.assertEqual(up.status_code, 201, up.text)
        file_id = up.json()["id"]
        row = await self.db.attachment.find_unique(where={"id": file_id})
        self.assertTrue(row.storageKey.startswith(f"os/attachments/ws/{self.w}/"), row.storageKey)
        self.assertEqual(row.workspaceId, self.w)
        listed = (await self.member_.get("/api/attachments", params={"entityType": "TASK", "entityId": task["id"]}, headers=self.ws)).json()
        self.assertEqual([a["id"] for a in listed["items"]], [file_id])
        down = await self.member_.get(f"/api/attachments/{file_id}/download", headers=self.ws)
        self.assertEqual((down.status_code, down.content), (200, b"the plan"))
        stranger = await self.member()
        self.assertEqual((await stranger.get(f"/api/attachments/{file_id}/download")).status_code, 404)
        self.assertEqual((await stranger.get(f"/api/attachments/{file_id}/download", headers=self.ws)).status_code, 404)
        # a member who didn't upload it can't delete it; an admin can
        self.assertEqual((await self.member_.delete(f"/api/attachments/{file_id}", headers=self.ws)).status_code, 404)
        self.assertIn((await self.admin.delete(f"/api/attachments/{file_id}", headers=self.ws)).status_code, (200, 204))

    async def test_document_parent_refused_in_workspace(self):
        response = await self.upload(self.owner, "DOCUMENT", "anything", self.ws)
        self.assertEqual(response.status_code, 400, response.text)
