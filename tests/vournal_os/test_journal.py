"""The journal on the web, read-only (spec): the list with summary, mood, topics, people and
to-dos, newest first and paged; an entry with its transcript; its audio through a
short-lived link. Silent failures stay hidden as in the app; another account's entry is
not found; nothing can be written."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from vournal_os.support import OsTestCase


class Journal(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web = await self.signed_in()
        uid = self.web.user.id
        base = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
        self.entries = []
        for i, text in enumerate(("Gym with Sara", "Work on the thesis", "Dinner plans")):
            entry = await self.db.entry.create(data={"userId": uid, "rawTranscript": text, "summary": f"Summary {i}", "moodScore": 4, "moodLabel": "good",
                                                     "emotions": ["calm"], "status": "DONE", "createdAt": base + timedelta(hours=i),
                                                     **({"audioUrl": f"audio/{uid}/{i}.m4a"} if i == 2 else {})})
            self.entries.append(entry)
        person = await self.db.person.create(data={"userId": uid, "name": "Sara", "normalizedName": "sara"})
        await self.db.entryperson.create(data={"entryId": self.entries[0].id, "personId": person.id})
        await self.db.todo.create(data={"entryId": self.entries[1].id, "userId": uid, "text": "Email the supervisor"})
        # a silent recording that failed: the app hides it, so the web does too
        await self.db.entry.create(data={"userId": uid, "rawTranscript": "", "status": "FAILED", "emotions": [], "createdAt": base + timedelta(hours=5)})

    async def test_list_pages_newest_first(self):
        page = (await self.web.get("/api/journal?limit=2")).json()
        self.assertEqual([e["rawTranscript"] for e in page["items"]], ["Dinner plans", "Work on the thesis"])
        self.assertEqual(page["items"][0]["hasAudio"], True)
        self.assertNotIn("audioUrl", page["items"][0])
        rest = (await self.web.get(f"/api/journal?limit=2&before={page['nextBefore']}")).json()
        self.assertEqual(([e["rawTranscript"] for e in rest["items"]], rest["nextBefore"]), (["Gym with Sara"], None))
        self.assertEqual([p["name"] for p in rest["items"][0]["people"]], ["Sara"])
        self.assertEqual([t["text"] for t in page["items"][1]["todos"]], ["Email the supervisor"])
        self.assertEqual((await self.web.get("/api/journal?limit=0")).status_code, 400)
        self.assertEqual((await self.web.get("/api/journal?before=soon")).status_code, 400)

    async def test_entry_and_audio(self):
        entry = (await self.web.get(f"/api/journal/{self.entries[0].id}")).json()
        self.assertEqual((entry["rawTranscript"], entry["summary"], entry["hasAudio"]), ("Gym with Sara", "Summary 0", False))
        none = await self.web.get(f"/api/journal/{self.entries[0].id}/audio")
        self.assertEqual((none.status_code, none.json()["message"]), (404, "Entry has no audio"))
        with patch("app.main.create_signed_url", AsyncMock(return_value="https://signed.example/1")) as signed:
            audio = await self.web.get(f"/api/journal/{self.entries[2].id}/audio")
        self.assertEqual(audio.json(), {"url": "https://signed.example/1"})
        signed.assert_awaited_once_with(self.entries[2].audioUrl)

    async def test_other_accounts_and_no_writes(self):
        other = await self.signed_in()
        for path in ("", "/audio"):
            response = await other.get(f"/api/journal/{self.entries[2].id}{path}")
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Entry not found"))
        self.assertEqual((await other.get("/api/journal")).json(), {"items": [], "nextBefore": None})
        for method in ("POST", "PATCH", "DELETE"):
            self.assertIn((await self.web.request(method, f"/api/journal/{self.entries[0].id}")).status_code, (404, 405))
        self.assertEqual(await self.db.entry.count(where={"userId": self.web.user.id}), 4)


if __name__ == "__main__":
    unittest.main()
