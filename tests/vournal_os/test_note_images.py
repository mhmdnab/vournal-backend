"""Images pasted into rich notes (design 2026-11-18): uploaded as inline note attachments, deleted
with the note, and cleaned up a week after they leave it. Only inline images are ever touched."""

from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase
from vournal_os.test_attachments import PNG


class NoteImages(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web = await self.signed_in()
        self.note = (await self.web.post("/api/notes", json={"title": "N"})).json()

    async def upload(self, entity_type="NOTE", entity_id=None, inline="true", mime="image/png", name="p.png", status=201):
        data = {"entityType": entity_type, "entityId": entity_id or self.note["id"]}
        if inline is not None:
            data["inline"] = inline
        r = await self.web.post("/api/attachments", data=data, files={"file": (name, PNG, mime)})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    async def row(self, id):
        return await self.db.attachment.find_unique(where={"id": id})

    async def age(self, id, days):
        await self.db.attachment.update(where={"id": id}, data={"createdAt": datetime.now(timezone.utc) - timedelta(days=days)})

    async def use(self, *ids):
        html = "".join(f'<img src="/api/attachments/{i}/download">' for i in ids)
        r = await self.web.patch(f"/api/notes/{self.note['id']}", json={"content": f"<p>x</p>{html}", "contentFormat": "html"})
        self.assertEqual(r.status_code, 200, r.text)

    async def test_inline_upload_on_note(self):
        made = await self.upload()
        self.assertTrue((await self.row(made["id"])).inline)
        plain = await self.upload(inline=None)
        self.assertFalse((await self.row(plain["id"])).inline)

    async def test_inline_refused_elsewhere(self):
        task = (await self.web.post("/api/tasks", json={"title": "t"})).json()
        r = await self.web.post("/api/attachments", data={"entityType": "TASK", "entityId": task["id"], "inline": "true"},
                                files={"file": ("p.png", PNG, "image/png")})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("inline is only for note images", r.text)
        await self.upload(mime="application/pdf", name="a.pdf", status=400)
        await self.upload(inline="maybe", status=400)

    async def test_cleanup_deletes_unreferenced_after_7_days(self):
        from app.os.attachments import cleanup_note_images

        made = await self.upload()
        key = (await self.row(made["id"])).storageKey
        await self.age(made["id"], 8)
        self.assertEqual(await cleanup_note_images(datetime.now(timezone.utc)), 1)
        self.assertIsNone(await self.row(made["id"]))
        self.assertNotIn(key, self.objects)

    async def test_cleanup_keeps_referenced_and_recent_and_non_inline(self):
        from app.os.attachments import cleanup_note_images

        used, recent, plain = await self.upload(), await self.upload(), await self.upload(inline=None)
        await self.use(used["id"])
        for a in (used, plain):
            await self.age(a["id"], 30)
        self.assertEqual(await cleanup_note_images(datetime.now(timezone.utc)), 0)
        for a in (used, recent, plain):
            self.assertIsNotNone(await self.row(a["id"]))

    async def test_cleanup_when_note_deleted_outside_the_route(self):
        from app.os.attachments import cleanup_note_images

        made = await self.upload()
        await self.age(made["id"], 8)
        await self.db.note.delete(where={"id": self.note["id"]})
        self.assertEqual(await cleanup_note_images(datetime.now(timezone.utc)), 1)


class SharedImages(NoteImages):
    """An image copied into another note (final review): deleting or editing the first note keeps it."""

    async def other_note_showing(self, attachment_id):
        other = (await self.web.post("/api/notes", json={"title": "B", "contentFormat": "html",
                                                          "content": f'<p>b</p><img src="/api/attachments/{attachment_id}/download">'})).json()
        return other

    async def test_delete_keeps_images_shown_in_another_note(self):
        made = await self.upload()
        await self.use(made["id"])
        await self.other_note_showing(made["id"])
        r = await self.web.delete(f"/api/notes/{self.note['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(await self.row(made["id"]))

    async def test_cleanup_keeps_images_shown_in_another_note(self):
        from app.os.attachments import cleanup_note_images

        made = await self.upload()
        await self.other_note_showing(made["id"])
        await self.age(made["id"], 30)
        self.assertEqual(await cleanup_note_images(datetime.now(timezone.utc)), 0)
        self.assertIsNotNone(await self.row(made["id"]))
