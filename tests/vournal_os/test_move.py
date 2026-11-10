"""Moving items between Personal and a workspace (spec 2026-11-09-workspaces-design).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from datetime import datetime, timezone

from vournal_os.test_workspaces import WorkspaceCase


class Move(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.w, self.owner, self.admin, self.member_ = await self.team()
        self.ws = {"X-Vournal-Space": self.w}

    async def move(self, client, type_, id, to):
        return await client.post("/api/move", json={"type": type_, "id": id, "to": to})

    async def test_move_project_brings_everything(self):
        me = self.member_.user.id
        category = await self.db.category.create(data={"userId": me, "name": "Mine", "scope": "TASK"})
        project = await self.db.project.create(data={"userId": me, "createdById": me, "name": "Launch"})
        task = await self.db.task.create(data={"userId": me, "createdById": me, "title": "T", "projectId": project.id, "categoryId": category.id})
        sub = await self.db.task.create(data={"userId": me, "createdById": me, "title": "S", "parentId": task.id})
        event = await self.db.event.create(
            data={"userId": me, "createdById": me, "title": "E", "projectId": project.id, "startAt": datetime.now(timezone.utc)}
        )
        note = await self.db.note.create(data={"userId": me, "createdById": me, "title": "N", "content": "", "projectId": project.id})
        file = await self.db.attachment.create(
            data={"userId": me, "createdById": me, "entityType": "TASK", "entityId": task.id, "fileName": "a.txt",
                  "mimeType": "text/plain", "size": 1, "storageKey": "os/attachments/x/a"}
        )
        response = await self.move(self.member_, "project", project.id, self.w)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["moved"], {"projects": 1, "tasks": 2, "events": 1, "notes": 1, "attachments": 1})
        for model, row in (("project", project), ("task", task), ("task", sub), ("event", event), ("note", note), ("attachment", file)):
            fresh = await getattr(self.db, model).find_unique(where={"id": row.id})
            self.assertEqual(fresh.workspaceId, self.w, model)
        self.assertIsNone((await self.db.task.find_unique(where={"id": task.id})).categoryId)
        seen = (await self.owner.get(f"/api/projects/{project.id}", headers=self.ws))
        self.assertEqual(seen.status_code, 200, seen.text)

    async def test_move_back_makes_it_mine(self):
        task = (await self.owner.post("/api/tasks", json={"title": "T", "assigneeId": self.member_.user.id}, headers=self.ws)).json()
        refused = await self.move(self.member_, "task", task["id"], None)
        self.assertEqual(refused.status_code, 404, refused.text)
        done = await self.move(self.admin, "task", task["id"], None)
        self.assertEqual(done.status_code, 200, done.text)
        row = await self.db.task.find_unique(where={"id": task["id"]})
        self.assertEqual((row.workspaceId, row.userId, row.createdById, row.assigneeId), (None, self.admin.user.id, self.admin.user.id, None))

    async def test_move_task_out_of_project_unlinks(self):
        project = (await self.owner.post("/api/projects", json={"name": "P"}, headers=self.ws)).json()
        task = (await self.owner.post("/api/tasks", json={"title": "T", "projectId": project["id"]}, headers=self.ws)).json()
        await self.move(self.owner, "task", task["id"], None)
        self.assertIsNone((await self.db.task.find_unique(where={"id": task["id"]})).projectId)

    async def test_cannot_move_into_a_workspace_im_not_in(self):
        stranger = await self.member()
        task = (await stranger.post("/api/tasks", json={"title": "Mine"})).json()
        response = await self.move(stranger, "task", task["id"], self.w)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIsNone((await self.db.task.find_unique(where={"id": task["id"]})).workspaceId)

    async def test_assignee_outside_the_destination_is_cleared(self):
        other_owner = self.owner
        w2 = await self.workspace(other_owner, "Other")
        task = (await self.owner.post("/api/tasks", json={"title": "T", "assigneeId": self.member_.user.id}, headers=self.ws)).json()
        response = await self.move(self.owner, "task", task["id"], w2)
        self.assertEqual(response.status_code, 200, response.text)
        row = await self.db.task.find_unique(where={"id": task["id"]})
        self.assertEqual((row.workspaceId, row.assigneeId), (w2, None))
