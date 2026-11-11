"""Personal sections read Personal only; "what did I do" counts what I did in workspaces
(spec 2026-11-09-workspaces-design, "What stays personal").

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from datetime import datetime, timedelta, timezone

from vournal_os.test_workspaces import WorkspaceCase


class WorkspaceSweep(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.w, self.owner, self.admin, self.member_ = await self.team()
        self.ws = {"X-Vournal-Space": self.w}

    async def test_personal_only_modules_skip_workspace_items(self):
        me = self.owner
        goal = await self.db.goal.create(data={"userId": me.user.id, "name": "Grow"})
        await self.db.task.create(data={"userId": me.user.id, "createdById": me.user.id, "title": "zebra plan", "workspaceId": self.w, "goalId": goal.id})
        await self.db.task.create(data={"userId": me.user.id, "createdById": me.user.id, "title": "zebra mine"})
        found = (await me.get("/api/search", params={"q": "zebra"})).json()
        titles = [r.get("title") for r in (found if isinstance(found, list) else found.get("results", found.get("items", [])))]
        self.assertIn("zebra mine", titles)
        self.assertNotIn("zebra plan", titles)
        goal_view = (await me.get(f"/api/goals/{goal.id}")).json()
        self.assertNotIn("zebra plan", str(goal_view))
        due = datetime.now(timezone.utc) + timedelta(hours=2)
        await self.db.task.create(data={"userId": me.user.id, "createdById": me.user.id, "title": "ws today", "workspaceId": self.w, "dueAt": due, "hasDueTime": True})
        today = (await me.get("/api/dashboard")).text
        self.assertNotIn("ws today", today)

    async def test_assistant_snapshot_reads_personal_only(self):
        from app.os import life_data

        me = self.owner
        project = await self.db.project.create(data={"userId": me.user.id, "createdById": me.user.id, "name": "Secret ws", "workspaceId": self.w})
        mine = await self.db.project.create(data={"userId": me.user.id, "createdById": me.user.id, "name": "Mine"})
        found = await self.db.project.find_many(where={"userId": me.user.id, "workspaceId": None})
        self.assertEqual([p.id for p in found], [mine.id])
        self.assertTrue(hasattr(life_data, "overdue_task_where"))
        overdue = life_data.overdue_task_where(me.user.id, datetime.now(timezone.utc), datetime.now(timezone.utc))
        self.assertIsNone(overdue.get("workspaceId", "missing"))
        self.assertNotEqual(project.id, mine.id)

    async def test_reports_count_assigned_workspace_tasks(self):
        now = datetime.now(timezone.utc)
        before = (await self.member_.get("/api/reports/weekly")).json()
        await self.db.task.create(
            data={"userId": self.owner.user.id, "createdById": self.owner.user.id, "title": "Shipped for the team", "workspaceId": self.w,
                  "assigneeId": self.member_.user.id, "status": "COMPLETED", "completedAt": now}
        )
        after = (await self.member_.get("/api/reports/weekly")).json()
        self.assertEqual(after["tasks"]["completed"], before["tasks"]["completed"] + 1, after["tasks"])

    async def test_dashboard_day_shows_assigned_workspace_tasks_and_events(self):
        due = datetime.now(timezone.utc) + timedelta(minutes=30)
        await self.db.task.create(
            data={"userId": self.owner.user.id, "createdById": self.owner.user.id, "title": "assigned today", "workspaceId": self.w,
                  "assigneeId": self.member_.user.id, "dueAt": due, "hasDueTime": True}
        )
        await self.db.event.create(
            data={"userId": self.owner.user.id, "createdById": self.owner.user.id, "title": "team sync", "workspaceId": self.w, "startAt": due}
        )
        day = (await self.member_.get("/api/dashboard")).text
        self.assertIn("assigned today", day)
        self.assertIn("team sync", day)
        self.assertIn(f"space={self.w}", day)


class WorkspaceReminders(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.w, self.owner, self.admin, self.member_ = await self.team()

    async def notified(self, client) -> list[str]:
        await client.post("/api/notifications/sync")
        return [n.title for n in await self.db.notification.find_many(where={"userId": client.user.id})]

    async def test_task_reminder_goes_to_assignee(self):
        from app.os import notification_sync

        now = datetime.now(timezone.utc)
        await self.db.task.create(data={"userId": self.owner.user.id, "createdById": self.owner.user.id, "title": "Review deck",
                                        "workspaceId": self.w, "assigneeId": self.member_.user.id, "reminderAt": now - timedelta(minutes=1)})
        await self.db.task.create(data={"userId": self.owner.user.id, "createdById": self.owner.user.id, "title": "Nobody's",
                                        "workspaceId": self.w, "reminderAt": now - timedelta(minutes=1)})
        self.assertIn(self.member_.user.id, await notification_sync.timed_candidate_users(now))
        self.assertIn("Review deck", await self.notified(self.member_))
        mine = await self.notified(self.owner)
        self.assertNotIn("Review deck", mine)
        self.assertIn("Nobody's", mine)  # unassigned: whoever added it
        self.assertNotIn("Nobody's", await self.notified(self.admin))

    async def test_event_reminder_goes_to_members_showing_it(self):
        start = datetime.now(timezone.utc) + timedelta(minutes=10)
        await self.db.event.create(data={"userId": self.owner.user.id, "createdById": self.owner.user.id, "title": "Team sync",
                                         "workspaceId": self.w, "startAt": start, "reminderMinutes": 15})
        await self.member_.patch(f"/api/workspaces/{self.w}/me", json={"showInCalendar": False})
        self.assertIn("Team sync", await self.notified(self.admin))
        self.assertNotIn("Team sync", await self.notified(self.member_))

    async def test_search_in_workspace_space(self):
        ws = {"X-Vournal-Space": self.w}
        await self.owner.post("/api/tasks", json={"title": "quokka launch"}, headers=ws)
        await self.member_.post("/api/tasks", json={"title": "quokka personal"})
        found = (await self.member_.get("/api/search", params={"q": "quokka"}, headers=ws)).text
        self.assertIn("quokka launch", found)
        self.assertNotIn("quokka personal", found)

    async def test_export_includes_my_workspace_items(self):
        ws = {"X-Vournal-Space": self.w}
        await self.member_.post("/api/tasks", json={"title": "my team task"}, headers=ws)
        exported = await self.member_.get("/api/users/me/export")
        if exported.status_code == 404:
            exported = await self.member_.get("/api/export")
        self.assertEqual(exported.status_code, 200, exported.text[:200])
        self.assertIn("my team task", exported.text)

    async def test_removed_member_stops_getting_reminders(self):
        now = datetime.now(timezone.utc)
        await self.db.task.create(data={"userId": self.member_.user.id, "createdById": self.member_.user.id, "title": "Old idea",
                                        "workspaceId": self.w, "reminderAt": now - timedelta(minutes=1)})
        await self.owner.delete(f"/api/workspaces/{self.w}/members/{self.member_.user.id}")
        self.assertNotIn("Old idea", await self.notified(self.member_))
        self.assertIn("Old idea", await self.notified(self.owner))  # nobody added it who's still here: the owner
