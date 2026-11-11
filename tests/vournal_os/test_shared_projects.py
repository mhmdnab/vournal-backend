"""One project shared view-only by email (spec 2026-11-09-workspaces-design, phase 5).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

from datetime import datetime, timezone
from unittest.mock import patch

from vournal_os.test_workspaces import WorkspaceCase


class SharedProjects(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.share_links: list[str] = []

        async def capture(to, project_name, inviter, url):
            self.share_links.append(url)
            return True

        patcher = patch("app.email.send_project_share", capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def guest(self, plan="free"):
        return await self.signed_in(await self.make_user(plan=plan, emailVerifiedAt=datetime.now(timezone.utc)))

    async def share(self, owner, project_id, guest, headers=None):
        response = await owner.post(f"/api/projects/{project_id}/shares", json={"email": guest.user.email}, headers=headers or {})
        self.assertEqual(response.status_code, 201, response.text)
        code = self.share_links[-1].rsplit("/", 1)[-1]
        accepted = await guest.post(f"/api/invites/project/{code}/accept")
        self.assertEqual(accepted.status_code, 200, accepted.text)
        return response.json()

    async def project_with_children(self, owner):
        project = (await owner.post("/api/projects", json={"name": "Launch"})).json()
        await owner.post("/api/tasks", json={"title": "Plan launch", "projectId": project["id"]})
        await owner.post("/api/notes", json={"title": "Launch notes", "projectId": project["id"]})
        await owner.post("/api/events", json={"title": "Launch day", "startAt": "2027-02-01T09:00:00.000Z", "projectId": project["id"]})
        return project

    async def test_free_guest_reads_only_the_shared_project(self):
        owner, guest = await self.member(), await self.guest()
        project = await self.project_with_children(owner)
        other = (await owner.post("/api/projects", json={"name": "Private"})).json()
        await self.share(owner, project["id"], guest)
        listed = (await guest.get("/api/shared/projects")).json()
        self.assertEqual([p["id"] for p in listed], [project["id"]])
        got = await guest.get(f"/api/shared/projects/{project['id']}")
        self.assertEqual(got.status_code, 200, got.text)
        body = got.json()
        self.assertEqual(body["project"]["name"], "Launch")
        self.assertEqual([t["title"] for t in body["tasks"]], ["Plan launch"])
        self.assertEqual([n["title"] for n in body["notes"]], ["Launch notes"])
        self.assertEqual([e["title"] for e in body["events"]], ["Launch day"])
        self.assertEqual((await guest.get(f"/api/shared/projects/{other['id']}")).status_code, 404)
        gated = await guest.get("/api/tasks")
        self.assertEqual((gated.status_code, gated.json().get("code")), (403, "plus_required"))

    async def test_share_needs_matching_email(self):
        owner, guest, stranger = await self.member(), await self.guest(), await self.guest()
        project = await self.project_with_children(owner)
        await owner.post(f"/api/projects/{project['id']}/shares", json={"email": guest.user.email})
        code = self.share_links[-1].rsplit("/", 1)[-1]
        self.assertEqual((await stranger.post(f"/api/invites/project/{code}/accept")).status_code, 403)
        self.assertEqual((await stranger.get(f"/api/shared/projects/{project['id']}")).status_code, 404)

    async def test_removed_share_stops_access(self):
        owner, guest = await self.member(), await self.guest()
        project = await self.project_with_children(owner)
        share = await self.share(owner, project["id"], guest)
        shares = (await owner.get(f"/api/projects/{project['id']}/shares")).json()
        self.assertEqual([s["email"] for s in shares], [guest.user.email])
        self.assertEqual((await owner.delete(f"/api/projects/{project['id']}/shares/{share['id']}")).status_code, 200)
        self.assertEqual((await guest.get(f"/api/shared/projects/{project['id']}")).status_code, 404)

    async def test_member_cannot_share_unless_admin_in_workspace(self):
        w, owner, admin, member = await self.team()
        ws = {"X-Vournal-Space": w}
        project = (await owner.post("/api/projects", json={"name": "Team"}, headers=ws)).json()
        guest = await self.guest()
        refused = await member.post(f"/api/projects/{project['id']}/shares", json={"email": guest.user.email}, headers=ws)
        self.assertEqual(refused.status_code, 404, refused.text)
        await self.share(admin, project["id"], guest, ws)
        self.assertEqual((await guest.get(f"/api/shared/projects/{project['id']}")).status_code, 200)

    async def test_shared_file_downloads_for_the_guest_only(self):
        owner, guest, stranger = await self.member(), await self.guest(), await self.guest()
        project = await self.project_with_children(owner)
        up = await owner.post(
            "/api/attachments", data={"entityType": "PROJECT", "entityId": project["id"]}, files={"file": ("brief.txt", b"brief", "text/plain")}
        )
        self.assertEqual(up.status_code, 201, up.text)
        await self.share(owner, project["id"], guest)
        url = f"/api/shared/projects/{project['id']}/attachments/{up.json()['id']}/download"
        got = await guest.get(url)
        self.assertEqual((got.status_code, got.content), (200, b"brief"))
        self.assertEqual((await stranger.get(url)).status_code, 404)
