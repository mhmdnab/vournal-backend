"""Tasks and projects in a workspace (spec 2026-11-09-workspaces-design, phase 2).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from vournal_os.test_workspaces import WorkspaceCase


class WorkspaceTasks(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.w, self.owner, self.admin, self.member_ = await self.team()
        self.ws = {"X-Vournal-Space": self.w}

    async def task(self, client, title="Ship it", **body):
        response = await client.post("/api/tasks", json={"title": title, **body}, headers=self.ws)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_member_sees_and_edits_workspace_tasks(self):
        task = await self.task(self.owner)
        self.assertEqual(task["lifeArea"], "WORK")
        listed = (await self.member_.get("/api/tasks", headers=self.ws)).json()
        self.assertIn(task["id"], [t["id"] for t in listed["items"]] if isinstance(listed, dict) else [t["id"] for t in listed])
        edited = await self.member_.patch(f"/api/tasks/{task['id']}", json={"title": "Shipped"}, headers=self.ws)
        self.assertEqual(edited.status_code, 200, edited.text)
        done = await self.member_.post(f"/api/tasks/{task['id']}/complete", headers=self.ws)
        self.assertEqual(done.status_code, 200, done.text)
        got = (await self.owner.get(f"/api/tasks/{task['id']}", headers=self.ws)).json()
        self.assertEqual((got["title"], got["status"]), ("Shipped", "COMPLETED"))
        self.assertEqual(got["createdBy"]["id"], self.owner.user.id)

    async def test_other_workspace_is_invisible(self):
        task = await self.task(self.owner)
        stranger = await self.member()
        self.assertEqual((await stranger.get("/api/tasks", headers=self.ws)).status_code, 404)
        self.assertEqual((await stranger.get(f"/api/tasks/{task['id']}")).status_code, 404)
        unknown = await self.owner.get("/api/tasks", headers={"X-Vournal-Space": "nope"})
        self.assertEqual((unknown.status_code, unknown.json()["message"]), (404, "Workspace not found"))
        # the creator's Personal space doesn't reach it either
        self.assertEqual((await self.owner.get(f"/api/tasks/{task['id']}")).status_code, 404)

    async def test_workspace_items_stay_out_of_personal(self):
        task = await self.task(self.owner)
        project = (await self.owner.post("/api/projects", json={"name": "Launch"}, headers=self.ws)).json()
        personal = (await self.owner.get("/api/tasks")).json()
        ids = [t["id"] for t in (personal["items"] if isinstance(personal, dict) else personal)]
        self.assertNotIn(task["id"], ids)
        projects = (await self.owner.get("/api/projects")).json()
        self.assertNotIn(project["id"], [p["id"] for p in (projects["items"] if isinstance(projects, dict) else projects)])
        overview = (await self.owner.get("/api/work/overview")).text
        self.assertNotIn(task["id"], overview)
        in_ws = (await self.member_.get("/api/work/overview", headers=self.ws)).text
        self.assertIn(task["id"], in_ws)

    async def test_assign_to_member_only(self):
        stranger = await self.member()
        refused = await self.owner.post("/api/tasks", json={"title": "X", "assigneeId": stranger.user.id}, headers=self.ws)
        self.assertEqual(refused.status_code, 404, refused.text)
        task = await self.task(self.owner, assigneeId=self.member_.user.id)
        self.assertEqual(task["assignee"]["id"], self.member_.user.id)
        note = await self.db.notification.find_first(where={"userId": self.member_.user.id, "entityId": task["id"]})
        self.assertIsNotNone(note)
        mine = (await self.member_.get("/api/tasks", params={"assignee": "me"}, headers=self.ws)).json()
        self.assertEqual([t["id"] for t in (mine["items"] if isinstance(mine, dict) else mine)], [task["id"]])

    async def test_member_deletes_own_only(self):
        theirs = await self.task(self.owner)
        mine = await self.task(self.member_)
        self.assertEqual((await self.member_.delete(f"/api/tasks/{theirs['id']}", headers=self.ws)).status_code, 404)
        self.assertIn((await self.member_.delete(f"/api/tasks/{mine['id']}", headers=self.ws)).status_code, (200, 204))
        self.assertIn((await self.admin.delete(f"/api/tasks/{theirs['id']}", headers=self.ws)).status_code, (200, 204))

    async def test_personal_links_refused_in_workspace(self):
        category = await self.db.category.create(data={"userId": self.owner.user.id, "name": "Mine", "scope": "TASK"})
        response = await self.owner.post("/api/tasks", json={"title": "X", "categoryId": category.id}, headers=self.ws)
        self.assertEqual((response.status_code, response.json()["message"]), (400, "Personal links aren't available in a workspace"))

    async def test_project_in_workspace_holds_its_tasks(self):
        project = (await self.owner.post("/api/projects", json={"name": "Launch"}, headers=self.ws)).json()
        await self.task(self.member_, projectId=project["id"])
        got = (await self.admin.get(f"/api/projects/{project['id']}", headers=self.ws)).json()
        self.assertEqual(got["name"], "Launch")
        personal_project = (await self.owner.post("/api/projects", json={"name": "Mine"})).json()
        cross = await self.owner.post("/api/tasks", json={"title": "X", "projectId": personal_project["id"]}, headers=self.ws)
        self.assertEqual(cross.status_code, 404, cross.text)
