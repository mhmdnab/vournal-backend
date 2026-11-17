"""The review card carries extraction's intent (design 2026-11-17, Ask vournal). Fake models only."""

from prisma import Json

from vournal_os.test_entry_actions import RESULT, EntryActionsBase


class ReviewIntent(EntryActionsBase):
    result = {**RESULT, "intent": "ask", "wantsActions": False}

    async def test_review_block_shows_intent_and_unasked(self):
        entry = await self.record("what should I focus on this week?")
        review = (await self.app.get(f"/entries/{entry.id}")).json()["review"]
        self.assertEqual(review["proposal"]["intent"], "ask")
        self.assertIs(review["proposal"]["unasked"], False)

    async def test_older_proposal_without_intent_reads_as_journal(self):
        entry = await self.record("a quiet day")
        proposal = dict(entry.proposal)
        proposal["result"] = {k: v for k, v in proposal["result"].items() if k != "intent"}
        await self.db.entry.update(where={"id": entry.id}, data={"proposal": Json(proposal)})
        review = (await self.app.get(f"/entries/{entry.id}")).json()["review"]
        self.assertEqual(review["proposal"]["intent"], "journal")
