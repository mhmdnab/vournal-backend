"""Calendars in a workspace, and the Personal overlay (spec 2026-11-09-workspaces-design, phase 3).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from datetime import datetime, timedelta, timezone

from vournal_os.test_workspaces import WorkspaceCase


def _day(offset: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


class WorkspaceCalendar(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.w, self.owner, self.admin, self.member_ = await self.team()
        self.ws = {"X-Vournal-Space": self.w}

    async def event(self, client, title="Standup", headers=None):
        start = f"{_day()}T09:00:00.000Z"
        response = await client.post(
            "/api/events", json={"title": title, "startAt": start, "endAt": f"{_day()}T09:30:00.000Z"}, headers=self.ws if headers is None else headers
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def feed(self, client, headers=None):
        response = await client.get("/api/calendar/feed", params={"from": _day(-1), "to": _day(3)}, headers=headers or {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    async def test_workspace_event_shared_with_members(self):
        event = await self.event(self.owner)
        got = await self.member_.get(f"/api/events/{event['id']}", headers=self.ws)
        self.assertEqual(got.status_code, 200, got.text)
        listed = (await self.member_.get("/api/events", headers=self.ws)).json()
        self.assertIn(event["id"], [e["id"] for e in listed["items"]])
        stranger = await self.member()
        self.assertEqual((await stranger.get(f"/api/events/{event['id']}")).status_code, 404)

    async def test_overlay_shows_workspace_events_in_personal_calendar(self):
        event = await self.event(self.owner)
        mine = [i for i in await self.feed(self.member_) if i["sourceId"] == event["id"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["meta"]["workspace"]["name"], "Acme")
        self.assertIs(mine[0]["meta"]["readOnly"], True)
        await self.member_.patch(f"/api/workspaces/{self.w}/me", json={"showInCalendar": False})
        self.assertEqual([i for i in await self.feed(self.member_) if i["sourceId"] == event["id"]], [])
        # the workspace's own calendar shows it whatever the switch says
        in_ws = [i for i in await self.feed(self.member_, self.ws) if i["sourceId"] == event["id"]]
        self.assertEqual(len(in_ws), 1)

    async def test_overlay_shows_tasks_assigned_to_me(self):
        due = f"{_day()}T12:00:00.000Z"
        assigned = (await self.owner.post("/api/tasks", json={"title": "Review", "dueAt": due, "hasDueTime": True, "assigneeId": self.member_.user.id}, headers=self.ws)).json()
        other = (await self.owner.post("/api/tasks", json={"title": "Not mine", "dueAt": due, "hasDueTime": True}, headers=self.ws)).json()
        ids = [i["sourceId"] for i in await self.feed(self.member_)]
        self.assertIn(assigned["id"], ids)
        self.assertNotIn(other["id"], ids)

    async def test_workspace_calendar_has_only_its_own_items(self):
        personal = await self.event(self.member_, "Dentist", headers={})
        ids = [i["sourceId"] for i in await self.feed(self.member_, self.ws)]
        self.assertNotIn(personal["id"], ids)

    async def test_overlay_items_are_not_editable_from_personal(self):
        event = await self.event(self.owner)
        response = await self.member_.patch(f"/api/events/{event['id']}", json={"title": "Hacked"})
        self.assertEqual(response.status_code, 404, response.text)

    async def test_household_sharing_is_refused_in_a_workspace(self):
        response = await self.owner.post(
            "/api/events", json={"title": "X", "startAt": f"{_day()}T09:00:00.000Z", "shared": True}, headers=self.ws
        )
        self.assertEqual(response.status_code, 400, response.text)
