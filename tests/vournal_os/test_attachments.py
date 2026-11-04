"""Attachments live in the private bucket (an in-memory stand-in here), are served only
to their owner, and go away with their row and with the account."""

import unittest

from vournal_os.support import OsTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Attachments(OsTestCase):
    async def _task(self, client):
        return (await client.post("/api/tasks", json={"title": "Passport renewal"})).json()["id"]

    async def _upload(self, client, entity_id, name="scan é.png", data=PNG, mime="image/png", entity_type="TASK"):
        return await client.post(
            "/api/attachments", data={"entityType": entity_type, "entityId": entity_id}, files={"file": (name, data, mime)}
        )

    async def test_upload_list_download_delete(self):
        client = await self.signed_in()
        task = await self._task(client)
        made = await self._upload(client, task)
        self.assertEqual(made.status_code, 201, made.text)
        meta = made.json()
        self.assertEqual((meta["fileName"], meta["mimeType"], meta["size"]), ("scan é.png", "image/png", len(PNG)))
        self.assertNotIn("storageKey", meta)
        key = (await self.db.attachment.find_unique(where={"id": meta["id"]})).storageKey
        self.assertTrue(key.startswith(f"os/attachments/{client.user.id}/"))
        self.assertEqual(self.objects[key], PNG)
        listed = (await client.get(f"/api/attachments?entityType=TASK&entityId={task}")).json()
        self.assertEqual([i["id"] for i in listed["items"]], [meta["id"]])
        got = await client.get(f"/api/attachments/{meta['id']}/download")
        self.assertEqual(got.content, PNG)
        self.assertEqual(got.headers["content-type"], "image/png")
        self.assertEqual(got.headers["content-disposition"], "attachment; filename*=UTF-8''scan%20%C3%A9.png")
        self.assertEqual(got.headers["cache-control"], "private, no-store")
        inline = await client.get(f"/api/attachments/{meta['id']}/download?inline=1")
        self.assertTrue(inline.headers["content-disposition"].startswith("inline;"))
        gone = await client.delete(f"/api/attachments/{meta['id']}")
        self.assertEqual(gone.json(), {"id": meta["id"]})
        self.assertNotIn(key, self.objects)

    async def test_another_users_files_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        task = await self._task(owner)
        meta = (await self._upload(owner, task)).json()
        for method, path in (("GET", f"/api/attachments/{meta['id']}/download"), ("DELETE", f"/api/attachments/{meta['id']}")):
            response = await other.request(method, path)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Attachment not found"))
        self.assertEqual((await other.get(f"/api/attachments?entityType=TASK&entityId={task}")).status_code, 404)
        self.assertEqual((await self._upload(other, task)).status_code, 404)

    async def test_limits(self):
        client = await self.signed_in()
        task = await self._task(client)
        big = await self._upload(client, task, data=b"x" * (10 * 1024 * 1024 + 1))
        self.assertEqual((big.status_code, big.json()["message"]), (413, "File too large"))
        exe = await self._upload(client, task, name="x.exe", mime="application/x-msdownload")
        self.assertEqual((exe.status_code, exe.json()["message"]), (400, "This file type is not allowed"))
        none = await client.post("/api/attachments", data={"entityType": "TASK", "entityId": task})
        self.assertEqual((none.status_code, none.json()["message"]), (400, "File is required"))
        self.assertEqual(self.objects, {})

    async def test_account_deletion_removes_the_files(self):
        from app.auth import delete_account

        client = await self.signed_in()
        task = await self._task(client)
        await self._upload(client, task)
        self.assertEqual(len(self.objects), 1)
        await delete_account(client.user)
        self.assertEqual(self.objects, {})


if __name__ == "__main__":
    unittest.main()
