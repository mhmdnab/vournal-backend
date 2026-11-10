"""Workspaces: create, invite by email, roles, leaving and deleting (spec 2026-11-09).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from datetime import datetime, timezone
from unittest.mock import patch

from vournal_os.support import OsTestCase


class WorkspaceCase(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.links: list[str] = []
        self.accepted: list[str] = []

        async def capture_invite(to, workspace_name, inviter, url):
            self.links.append(url)
            return True

        async def capture_accepted(to, who, workspace_name):
            self.accepted.append(to)
            return True

        for name, fake in (("send_workspace_invite", capture_invite), ("send_invite_accepted", capture_accepted)):
            patcher = patch(f"app.email.{name}", fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self._delete_workspaces)

    async def _delete_workspaces(self):
        users = await self.db.user.find_many(where={"email": {"in": self.emails}})
        ids = [m.workspaceId for m in await self.db.workspacemember.find_many(where={"userId": {"in": [u.id for u in users]}})]
        if ids:
            await self.db.workspace.delete_many(where={"id": {"in": ids}})

    async def member(self):
        """A signed-in Plus account with a confirmed email."""
        return await self.signed_in(await self.make_user(emailVerifiedAt=datetime.now(timezone.utc)))

    async def workspace(self, owner, name="Acme"):
        response = await owner.post("/api/workspaces", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def invite(self, admin, workspace_id, who, role="MEMBER"):
        response = await admin.post(f"/api/workspaces/{workspace_id}/invites", json={"email": who.user.email, "role": role})
        self.assertEqual(response.status_code, 201, response.text)
        return self.links[-1].rsplit("/", 1)[-1]

    async def join(self, admin, workspace_id, who, role="MEMBER"):
        code = await self.invite(admin, workspace_id, who, role)
        response = await who.post(f"/api/invites/workspace/{code}/accept")
        self.assertEqual(response.status_code, 200, response.text)

    async def team(self):
        """An owner, an admin and a member in one workspace."""
        owner, admin, member = await self.member(), await self.member(), await self.member()
        w = await self.workspace(owner)
        await self.join(owner, w, admin, "ADMIN")
        await self.join(owner, w, member)
        return w, owner, admin, member

    async def roles(self, w) -> dict:
        return {m.userId: str(getattr(m.role, "value", m.role)) for m in await self.db.workspacemember.find_many(where={"workspaceId": w})}


class Workspaces(WorkspaceCase):
    async def test_create_and_list(self):
        owner = await self.member()
        w = await self.workspace(owner, "Side business")
        listed = (await owner.get("/api/workspaces")).json()
        self.assertEqual([(x["id"], x["name"], x["role"], x["memberCount"]) for x in listed], [(w, "Side business", "OWNER", 1)])

    async def test_invite_accept_by_email(self):
        owner, guest, stranger = await self.member(), await self.member(), await self.member()
        w = await self.workspace(owner)
        code = await self.invite(owner, w, guest)
        self.assertTrue(self.links[-1].endswith(f"/invite/workspace/{code}"))
        preview = (await guest.get(f"/api/invites/workspace/{code}")).json()
        self.assertEqual((preview["workspaceName"], preview["emailMatches"]), ("Acme", True))
        wrong = await stranger.post(f"/api/invites/workspace/{code}/accept")
        self.assertEqual((wrong.status_code, wrong.json()["message"]), (403, "This invite is for another email address."))
        self.assertEqual((await guest.post(f"/api/invites/workspace/{code}/accept")).status_code, 200)
        self.assertEqual(self.accepted, [owner.user.email])
        self.assertEqual((await guest.post(f"/api/invites/workspace/{code}/accept")).status_code, 404)
        self.assertEqual([x["id"] for x in (await guest.get("/api/workspaces")).json()], [w])

    async def test_unconfirmed_email_cannot_accept(self):
        owner, guest = await self.member(), await self.signed_in()
        w = await self.workspace(owner)
        code = await self.invite(owner, w, guest)
        response = await guest.post(f"/api/invites/workspace/{code}/accept")
        self.assertEqual(response.status_code, 403, response.text)

    async def test_unknown_workspace_is_404(self):
        me = await self.member()
        response = await me.get("/api/workspaces/nope/members")
        self.assertEqual((response.status_code, response.json()["message"]), (404, "Workspace not found"))

    async def test_roles(self):
        w, owner, admin, member = await self.team()
        outsider = await self.member()
        refused = await member.post(f"/api/workspaces/{w}/invites", json={"email": outsider.user.email, "role": "MEMBER"})
        self.assertEqual(refused.status_code, 404)
        await self.invite(admin, w, outsider)
        promote = await admin.patch(f"/api/workspaces/{w}/members/{member.user.id}", json={"role": "ADMIN"})
        self.assertEqual(promote.status_code, 404)
        promote = await owner.patch(f"/api/workspaces/{w}/members/{member.user.id}", json={"role": "ADMIN"})
        self.assertEqual(promote.status_code, 200, promote.text)
        self.assertEqual((await self.roles(w))[member.user.id], "ADMIN")
        members = (await member.get(f"/api/workspaces/{w}/members")).json()
        self.assertEqual(len(members["members"]), 3)

    async def test_owner_leaving_hands_on(self):
        w, owner, admin, member = await self.team()
        self.assertEqual((await owner.post(f"/api/workspaces/{w}/leave")).status_code, 200)
        self.assertEqual(await self.roles(w), {admin.user.id: "OWNER", member.user.id: "MEMBER"})
        await admin.post(f"/api/workspaces/{w}/leave")
        self.assertEqual(await self.roles(w), {member.user.id: "OWNER"})
        await member.post(f"/api/workspaces/{w}/leave")
        self.assertIsNone(await self.db.workspace.find_unique(where={"id": w}))

    async def test_transfer_ownership(self):
        w, owner, admin, member = await self.team()
        response = await owner.post(f"/api/workspaces/{w}/transfer", json={"userId": member.user.id})
        self.assertEqual(response.status_code, 200, response.text)
        roles = await self.roles(w)
        self.assertEqual((roles[member.user.id], roles[owner.user.id]), ("OWNER", "ADMIN"))

    async def test_removed_member_loses_access_and_assignments(self):
        w, owner, admin, member = await self.team()
        task = await self.db.task.create(data={"userId": owner.user.id, "title": "T", "workspaceId": w, "assigneeId": member.user.id})
        self.assertEqual((await admin.delete(f"/api/workspaces/{w}/members/{member.user.id}")).status_code, 200)
        self.assertIsNone((await self.db.task.find_unique(where={"id": task.id})).assigneeId)
        self.assertEqual((await member.get(f"/api/workspaces/{w}/members")).status_code, 404)
        self.assertEqual((await admin.delete(f"/api/workspaces/{w}/members/{owner.user.id}")).status_code, 404)

    async def test_delete_needs_the_name(self):
        w, owner, admin, member = await self.team()
        self.assertEqual((await admin.request("DELETE", f"/api/workspaces/{w}", json={"confirmName": "Acme"})).status_code, 404)
        self.assertEqual((await owner.request("DELETE", f"/api/workspaces/{w}", json={"confirmName": "acme!"})).status_code, 400)
        self.assertEqual((await owner.request("DELETE", f"/api/workspaces/{w}", json={"confirmName": "Acme"})).status_code, 200)
        self.assertIsNone(await self.db.workspace.find_unique(where={"id": w}))

    async def test_show_in_calendar_is_per_member(self):
        w, owner, admin, member = await self.team()
        response = await member.patch(f"/api/workspaces/{w}/me", json={"showInCalendar": False})
        self.assertEqual(response.status_code, 200, response.text)
        mine = {x["id"]: x["showInCalendar"] for x in (await member.get("/api/workspaces")).json()}
        theirs = {x["id"]: x["showInCalendar"] for x in (await owner.get("/api/workspaces")).json()}
        self.assertEqual((mine[w], theirs[w]), (False, True))


class WorkspaceEndings(WorkspaceCase):
    async def items_by(self, client, w):
        ws = {"X-Vournal-Space": w}
        task = (await client.post("/api/tasks", json={"title": "T"}, headers=ws)).json()
        note = (await client.post("/api/notes", json={"title": "N"}, headers=ws)).json()
        project = (await client.post("/api/projects", json={"name": "P"}, headers=ws)).json()
        event = (await client.post("/api/events", json={"title": "E", "startAt": "2027-01-05T09:00:00.000Z"}, headers=ws)).json()
        return {"task": task["id"], "note": note["id"], "project": project["id"], "event": event["id"]}

    async def test_deleted_member_work_stays(self):
        from app.auth import delete_account

        w, owner, admin, member = await self.team()
        ids = await self.items_by(member, w)
        await delete_account(member.user)
        for model, id in ids.items():
            row = await getattr(self.db, model).find_unique(where={"id": id})
            self.assertIsNotNone(row, model)
            self.assertEqual((row.workspaceId, row.userId, row.createdById), (w, owner.user.id, member.user.id), model)

    async def test_deleted_owner_hands_on_and_work_stays(self):
        from app.auth import delete_account

        w, owner, admin, member = await self.team()
        ids = await self.items_by(owner, w)
        await delete_account(owner.user)
        self.assertEqual(await self.roles(w), {admin.user.id: "OWNER", member.user.id: "MEMBER"})
        row = await self.db.task.find_unique(where={"id": ids["task"]})
        self.assertEqual(row.userId, admin.user.id)

    async def test_last_member_deleted_removes_workspace_and_files(self):
        from app.auth import delete_account

        owner = await self.member()
        w = await self.workspace(owner)
        task = (await owner.post("/api/tasks", json={"title": "T"}, headers={"X-Vournal-Space": w})).json()
        await self.db.attachment.create(
            data={"userId": owner.user.id, "createdById": owner.user.id, "workspaceId": w, "entityType": "TASK", "entityId": task["id"],
                  "fileName": "a.txt", "mimeType": "text/plain", "size": 1, "storageKey": f"os/attachments/ws/{w}/a"}
        )
        self.objects[f"os/attachments/ws/{w}/a"] = b"x"
        await delete_account(owner.user)
        self.assertIsNone(await self.db.workspace.find_unique(where={"id": w}))
        self.assertNotIn(f"os/attachments/ws/{w}/a", self.objects)

    async def test_deleted_assignee_is_unassigned(self):
        from app.auth import delete_account

        w, owner, admin, member = await self.team()
        task = (await owner.post("/api/tasks", json={"title": "T", "assigneeId": member.user.id}, headers={"X-Vournal-Space": w})).json()
        await delete_account(member.user)
        self.assertIsNone((await self.db.task.find_unique(where={"id": task["id"]})).assigneeId)
