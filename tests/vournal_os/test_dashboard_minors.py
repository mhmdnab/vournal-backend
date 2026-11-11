"""Small workspace fixes before launch (2026-11-11): share links expire, deleting a shared
project's tasks keeps colleagues' tasks, report links open the task's own space.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from datetime import datetime, timedelta, timezone

from vournal_os.test_shared_projects import SharedProjects


def _links(value, task_id, found=None):
    """Every `link` next to this task id, anywhere in a report."""
    found = [] if found is None else found
    if isinstance(value, dict):
        if value.get("id") == task_id and "link" in value:
            found.append(value["link"])
        for v in value.values():
            _links(v, task_id, found)
    elif isinstance(value, list):
        for v in value:
            _links(v, task_id, found)
    return found


class DashboardMinors(SharedProjects):
    async def test_project_share_link_expires_after_14_days(self):
        owner, guest = await self.member(), await self.guest()
        project = (await owner.post("/api/projects", json={"name": "Launch"})).json()
        await owner.post(f"/api/projects/{project['id']}/shares", json={"email": guest.user.email})
        code = self.share_links[-1].rsplit("/", 1)[-1]
        await self.db.projectshare.update_many(
            where={"projectId": project["id"]}, data={"createdAt": datetime.now(timezone.utc) - timedelta(days=15)}
        )
        self.assertEqual((await guest.get(f"/api/invites/project/{code}")).status_code, 404)
        self.assertEqual((await guest.post(f"/api/invites/project/{code}/accept")).status_code, 404)

    async def test_deleting_a_shared_project_with_its_tasks_keeps_colleagues_tasks(self):
        w, owner, _admin, member = await self.team()
        ws = {"X-Vournal-Space": w}
        project = (await owner.post("/api/projects", json={"name": "Launch"}, headers=ws)).json()
        mine = (await owner.post("/api/tasks", json={"title": "Mine", "projectId": project["id"]}, headers=ws)).json()
        theirs = (await member.post("/api/tasks", json={"title": "Theirs", "projectId": project["id"]}, headers=ws)).json()
        gone = await owner.delete(f"/api/projects/{project['id']}?deleteTasks=true", headers=ws)
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertIsNone(await self.db.task.find_unique(where={"id": mine["id"]}))
        kept = await self.db.task.find_unique(where={"id": theirs["id"]})
        self.assertIsNotNone(kept)
        self.assertIsNone(kept.projectId)

    async def test_report_links_to_a_workspace_task_open_its_space(self):
        w, owner, _admin, member = await self.team()
        ws = {"X-Vournal-Space": w}
        due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        task = (await owner.post("/api/tasks", json={"title": "Ship it", "dueAt": due, "assigneeId": member.user.id}, headers=ws)).json()
        found = []
        for period in ("", "?offset=1"):
            response = await member.get(f"/api/reports/weekly{period}")
            if response.status_code == 200:
                found += _links(response.json(), task["id"])
        self.assertTrue(found, "the task shows up in a weekly report")
        for link in found:
            self.assertIn(f"space={w}", link)
