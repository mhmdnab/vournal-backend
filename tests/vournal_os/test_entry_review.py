"""The app's review card (design 2026-11-06): a new app's entry waits as a proposal until the
user saves it, and an unreviewed entry is in no list. Older apps (no `review`) are unchanged.
The extraction model is patched (no paid calls)."""

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from vournal_os.support import OsTestCase

RESULT = {
    "summary": "Lunch with Ali; need to call mom.",
    "mood": {"score": 4, "label": "good", "emotions": ["calm"]},
    "todos": [{"text": "Call mom", "dueDate": None, "time": None}],
    "topics": ["work"],
    "people": ["Ali"],
    "places": [],
}


class ReviewBase(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app import main

        patcher = patch.object(main, "extract", AsyncMock(side_effect=lambda *a, **k: (copy.deepcopy(RESULT), None)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = await self.mobile()
        self.user = self.app.user

    async def entry(self, review=True, transcript="call mom, lunch with Ali"):
        r = await self.app.post("/entries", json={"transcript": transcript, **({"review": True} if review else {})})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class ReviewMode(ReviewBase):
    async def test_review_text_entry_stores_a_proposal_and_creates_nothing(self):
        e = await self.entry()
        self.assertEqual((e["status"], e["reviewState"]), ("DONE", "needs_review"))
        self.assertEqual(await self.db.todo.count(where={"userId": self.user.id}), 0)
        self.assertEqual(await self.db.entryperson.count(where={"entryId": e["id"]}), 0)
        self.assertEqual(await self.db.entrytopic.count(where={"entryId": e["id"]}), 0)
        row = await self.db.entry.find_unique(where={"id": e["id"]})
        self.assertEqual(row.proposal["result"]["todos"][0]["text"], "Call mom")
        self.assertTrue(row.proposal["journalSuggested"])
        self.assertEqual(row.summary, RESULT["summary"])

    async def test_without_review_nothing_changes(self):
        e = await self.entry(review=False)
        self.assertIsNone(e["reviewState"])
        self.assertEqual(await self.db.todo.count(where={"entryId": e["id"]}), 1)
        self.assertEqual(await self.db.entryperson.count(where={"entryId": e["id"]}), 1)

    async def test_hidden_from_every_list(self):
        e = await self.entry()
        self.assertEqual([x["id"] for x in (await self.app.get("/entries")).json()], [])
        self.assertEqual([x["id"] for x in (await self.app.get("/entries?review=pending")).json()], [e["id"]])
        insights = (await self.app.get("/insights")).json()
        self.assertEqual((insights["entryCount"], insights["moodTrend"]), (0, []))
        web = await self.signed_in(self.user)
        self.assertEqual((await web.get("/api/journal")).json()["items"], [])

    async def test_not_in_journal_entries_are_hidden_but_their_todos_count(self):
        e = await self.entry(review=False)
        await self.db.entry.update(where={"id": e["id"]}, data={"reviewState": "saved", "inJournal": False})
        self.assertEqual((await self.app.get("/entries")).json(), [])
        self.assertEqual((await self.app.get("/insights")).json()["entryCount"], 0)
        self.assertEqual([t["text"] for t in (await self.app.get("/todos")).json()], ["Call mom"])
        threads = (await self.app.get("/threads?kind=people")).json()
        self.assertEqual([t["name"] for t in threads], [])

    async def test_saved_entries_list_as_before(self):
        e = await self.entry(review=False)
        await self.db.entry.update(where={"id": e["id"]}, data={"reviewState": "saved"})
        self.assertEqual([x["id"] for x in (await self.app.get("/entries")).json()], [e["id"]])

    async def test_dashboard_journal_shape_is_unchanged(self):
        await self.entry(review=False)
        web = await self.signed_in(self.user)
        item = (await web.get("/api/journal")).json()["items"][0]
        self.assertFalse({"reviewState", "inJournal", "review"} & set(item))


class StaleReviews(ReviewBase):
    async def test_stale_needs_review_entries_are_discarded(self):
        from app import review

        old = await self.entry()
        recent = await self.entry()
        saved = await self.entry(review=False)
        eight_days = datetime.now(timezone.utc) - timedelta(days=8)
        await self.db.entry.update(where={"id": old["id"]}, data={"createdAt": eight_days})
        await self.db.entry.update(where={"id": recent["id"]}, data={"createdAt": datetime.now(timezone.utc) - timedelta(days=6)})
        await self.db.entry.update(where={"id": saved["id"]}, data={"createdAt": eight_days})
        self.assertEqual(await review.discard_stale(), 1)
        left = {e.id for e in await self.db.entry.find_many(where={"userId": self.user.id})}
        self.assertEqual(left, {recent["id"], saved["id"]})
