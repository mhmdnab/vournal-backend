"""Rich notes through the API (design 2026-11-18): HTML saves are sanitized on the server, lists and
search read the words, the off switch (NOTES_RICH_EDITOR=0) pauses HTML saves, and a note's inline
images go with it."""

import os
from unittest.mock import patch

from vournal_os.support import OsTestCase


class NotesRich(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web = await self.signed_in()
        self.note = (await self.web.post("/api/notes", json={"title": "N"})).json()

    async def patch(self, status=200, **body):
        r = await self.web.patch(f"/api/notes/{self.note['id']}", json=body)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    async def row(self):
        return await self.db.note.find_unique(where={"id": self.note["id"]})

    async def test_html_save_is_sanitized_and_texted(self):
        out = await self.patch(content="<p>Hi<script>x</script></p>", contentFormat="html")
        row = await self.row()
        self.assertEqual((row.content, row.contentText, row.contentFormat), ("<p>Hi</p>", "Hi", "html"))
        self.assertEqual((out["contentFormat"], out["editor"], out["content"]), ("html", "rich", "<p>Hi</p>"))

    async def test_markdown_save_unchanged(self):
        await self.patch(content="# T\n\n**bold** item")
        row = await self.row()
        self.assertEqual((row.content, row.contentFormat), ("# T\n\n**bold** item", "markdown"))
        self.assertEqual(row.contentText, "T\n\nbold item")

    async def test_list_excerpt_from_text_for_html(self):
        await self.patch(content='<h1>Plan</h1><p><span style="color:#ff0000">red words</span></p>', contentFormat="html")
        item = next(i for i in (await self.web.get("/api/notes")).json()["items"] if i["id"] == self.note["id"])
        self.assertEqual((item["excerpt"], item["wordCount"]), ("Plan red words", 3))

    async def test_search_finds_html_by_words_not_tags(self):
        await self.patch(content='<p><span style="color:#ff0000">giraffe</span></p>', contentFormat="html")
        ids = lambda r: [i["id"] for i in r.json()["items"]]  # noqa: E731
        self.assertNotIn(self.note["id"], ids(await self.web.get("/api/notes", params={"q": "span"})))
        self.assertIn(self.note["id"], ids(await self.web.get("/api/notes", params={"q": "giraffe"})))
        md = (await self.web.post("/api/notes", json={"title": "M", "content": "an okapi here"})).json()
        self.assertIn(md["id"], ids(await self.web.get("/api/notes", params={"q": "okapi"})))

    async def test_long_note_converts(self):
        words = ("lorem ipsum dolor sit amet " * 3800)[:95_000]
        html = "".join(f'<p style="color:#123456;font-size:15px">{words[i:i + 500]}</p>' for i in range(0, len(words), 500))
        self.assertGreater(len(html), 100_000)
        await self.patch(content=html, contentFormat="html")
        self.assertLessEqual(len((await self.row()).contentText), 100_000)

    async def test_text_over_limit_refused(self):
        await self.patch(status=400, content="<p>" + "x" * 100_001 + "</p>", contentFormat="html")
        await self.patch(status=400, content="x" * 100_001)

    async def test_switch_off_refuses_html(self):
        with patch.dict(os.environ, {"NOTES_RICH_EDITOR": "0"}):
            await self.patch(status=409, content="<p>x</p>", contentFormat="html")
            out = await self.patch(content="still markdown")
            self.assertEqual(out["editor"], "classic")
            r = await self.web.post("/api/notes", json={"title": "x", "content": "<p>x</p>", "contentFormat": "html"})
            self.assertEqual(r.status_code, 409)

    async def test_markdown_save_after_html_switches_format(self):
        await self.patch(content="<p>x</p>", contentFormat="html")
        await self.patch(content="plain again")
        self.assertEqual((await self.row()).contentFormat, "markdown")

    async def test_create_with_html(self):
        r = await self.web.post("/api/notes", json={"title": "R", "content": "<p onclick=x>hey</p>", "contentFormat": "html"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["content"], r.json()["contentFormat"]), ("<p>hey</p>", "html"))

    async def test_delete_removes_inline_images_only(self):
        user_id = self.web.user.id
        rows = []
        for inline in (True, False):
            key = f"os/attachments/{user_id}/{'in' if inline else 'out'}"
            self.objects[key] = b"img"
            rows.append(await self.db.attachment.create(data={"userId": user_id, "entityType": "NOTE", "entityId": self.note["id"],
                                                              "fileName": "a.png", "mimeType": "image/png", "size": 3,
                                                              "storageKey": key, "inline": inline}))
        r = await self.web.delete(f"/api/notes/{self.note['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(await self.db.attachment.find_unique(where={"id": rows[0].id}))
        self.assertNotIn(rows[0].storageKey, self.objects)
        self.assertIsNotNone(await self.db.attachment.find_unique(where={"id": rows[1].id}))


class NoteReaders(NotesRich):
    """Search, trips, export and the AI read a rich note's words, never its tags."""

    HTML = '<p><span style="color:#ff0000">zebra crossing</span> notes</p>'

    async def test_global_search_snippet_is_text(self):
        await self.patch(content=self.HTML, contentFormat="html")
        results = (await self.web.get("/api/search", params={"q": "zebra"})).json()["results"]
        hit = next(r for r in results if r.get("id") == self.note["id"])
        self.assertNotIn("<", str(hit))
        self.assertIn("zebra crossing", str(hit))
        self.assertFalse([r for r in (await self.web.get("/api/search", params={"q": "span"})).json()["results"] if r.get("id") == self.note["id"]])

    async def test_trip_notes_list_shows_text(self):
        trip = (await self.web.post("/api/trips", json={"name": "Rome", "destination": "Rome"})).json()
        await self.patch(content=self.HTML, contentFormat="html", tripId=trip["id"])
        body = (await self.web.get(f"/api/trips/{trip['id']}")).json()
        [n] = [n for n in body["notesList"] if n["id"] == self.note["id"]]
        self.assertEqual(n["content"], "zebra crossing notes")

    async def test_export_includes_format(self):
        await self.patch(content=self.HTML, contentFormat="html")
        r = await self.web.get("/api/users/me/export")
        notes = r.json()["data"]["notes"]
        [n] = [n for n in notes if n["id"] == self.note["id"]]
        self.assertEqual(n["contentFormat"], "html")

    async def test_ai_add_note_is_markdown(self):
        import json as _json
        from datetime import datetime, timezone

        from app.os import assistant_tools

        ctx = assistant_tools.Ctx(self.web.user, "Asia/Beirut", assistant_tools.local_day(datetime.now(timezone.utc), "Asia/Beirut"), "auto")
        out = await assistant_tools.run("add_note", {"title": "From AI", "content": "# Plan\n\n- one"}, ctx)
        self.assertFalse(out.get("isError"), out)
        note_id = _json.loads(out["content"])["id"]
        row = await self.db.note.find_unique(where={"id": note_id})
        self.assertEqual((row.contentFormat, row.contentText), ("markdown", "Plan\n\none"))
        tools = _json.load(open("app/os/data/assistant.json"))["tools"]
        add = next(t for t in tools if t["name"] == "add_note")
        self.assertIn("no HTML", add["input_schema"]["properties"]["content"]["description"])


class DeferredNotes(NotesRich):
    async def test_html_excerpt_keeps_every_character(self):
        await self.patch(content="<p>open my_file_name and *star* - dash 1. one</p>", contentFormat="html")
        item = next(i for i in (await self.web.get("/api/notes")).json()["items"] if i["id"] == self.note["id"])
        self.assertEqual(item["excerpt"], "open my_file_name and *star* - dash 1. one")

    async def test_unused_editor_route_is_gone(self):
        self.assertEqual((await self.web.get("/api/notes/editor")).status_code, 404)
