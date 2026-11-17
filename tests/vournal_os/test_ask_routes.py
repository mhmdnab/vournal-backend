"""Ask vournal from a recording (design 2026-11-17): /entries/{id}/ask hands a question to the
assistant and hides the entry, giving its monthly entry back; /unask turns it back into a journal
entry. The assistant is patched (no paid calls)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from vournal_os.parity import fake_model as fm
from vournal_os.test_entry_actions import RESULT, EntryActionsBase

QUESTION = {**RESULT, "intent": "ask", "wantsActions": False, "todos": []}


def reply(status="ok"):
    return {"status": status, "conversation": {"id": "conv-1", "title": "Focus"} if status == "ok" else None,
            "messages": [], "remainingThisMonth": 174}


class AskBase(EntryActionsBase):
    result = QUESTION

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.send = AsyncMock(return_value=reply())
        p = patch("app.os.assistant.send", self.send)
        p.start()
        self.addCleanup(p.stop)
        await self.consent(True)

    async def consent(self, allowed: bool):
        await self.db.usersettings.update(where={"userId": self.user.id},
                                          data={"assistantJournalConsentAt": datetime.now(timezone.utc) if allowed else None})

    async def used(self):
        return (await self.db.user.find_unique(where={"id": self.user.id})).usagePeriodEntries

    async def ask(self, entry_id, status=200, **body):
        r = await self.app.post(f"/entries/{entry_id}/ask", json=body)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    async def state(self, entry_id):
        return (await self.db.entry.find_unique(where={"id": entry_id})).reviewState


class Ask(AskBase):
    async def test_question_becomes_a_chat_and_gives_its_entry_back(self):
        entry = await self.record("what should I focus on this week?")
        before = await self.used()
        out = await self.ask(entry.id)
        self.assertEqual((out["status"], out["entryState"], out["conversation"]["id"]), ("ok", "asked", "conv-1"))
        self.send.assert_awaited_once()
        args, kwargs = self.send.await_args
        self.assertEqual(args[1], "what should I focus on this week?")
        self.assertIs(kwargs.get("always_ask"), True)
        stored = await self.db.entry.find_unique(where={"id": entry.id})
        self.assertEqual((stored.reviewState, stored.proposal["conversationId"]), ("asked", "conv-1"))
        self.assertEqual(await self.used(), before - 1)
        listed = [e["id"] for e in (await self.app.get("/entries")).json()]
        pending = [e["id"] for e in (await self.app.get("/entries", params={"review": "pending"})).json()]
        self.assertNotIn(entry.id, listed)
        self.assertNotIn(entry.id, pending)

    async def test_both_opens_a_chat_and_keeps_the_entry_waiting(self):
        self.result = {**QUESTION, "intent": "both"}
        entry = await self.record("rough day, how do I sleep better?")
        before = await self.used()
        out = await self.ask(entry.id)
        self.assertEqual(out["entryState"], "needs_review")
        self.assertEqual(await self.state(entry.id), "needs_review")
        self.assertEqual(await self.used(), before)

    async def test_a_second_ask_is_refused_and_never_sends_twice(self):
        entry = await self.record()
        await self.ask(entry.id)
        await self.ask(entry.id, status=409)
        self.assertEqual(self.send.await_count, 1)

    async def test_journal_intent_needs_force(self):
        self.result = {**QUESTION, "intent": "journal"}
        entry = await self.record("a calm day")
        await self.ask(entry.id, status=400)
        out = await self.ask(entry.id, force=True)
        self.assertEqual(out["entryState"], "asked")

    async def test_free_users_cannot_ask(self):
        self.app = await self.mobile(await self.make_user(plan="free"))
        entry = await self.record()
        out = await self.ask(entry.id, status=403)
        self.assertEqual(out["detail"]["code"], "plus_required")

    async def test_without_permission_nothing_is_sent(self):
        await self.consent(False)
        entry = await self.record()
        self.assertEqual(await self.ask(entry.id), {"status": "consent_needed"})
        self.assertEqual(await self.state(entry.id), "needs_review")
        self.send.assert_not_awaited()

    async def test_a_failed_answer_leaves_the_entry_waiting(self):
        self.send.return_value = reply("limit")
        entry = await self.record()
        before = await self.used()
        out = await self.ask(entry.id)
        self.assertEqual(out["status"], "limit")
        self.assertEqual(await self.state(entry.id), "needs_review")
        self.assertEqual(await self.used(), before)

    async def test_someone_elses_entry_is_not_found(self):
        entry = await self.record()
        self.app = await self.mobile(await self.make_user())
        await self.ask(entry.id, status=404)


class Unask(AskBase):
    async def test_unask_brings_the_entry_back_and_counts_it_again(self):
        entry = await self.record()
        await self.ask(entry.id)
        before = await self.used()
        r = await self.app.post(f"/entries/{entry.id}/unask")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["review"]["proposal"]["unasked"])
        self.assertEqual(await self.state(entry.id), "needs_review")
        self.assertEqual(await self.used(), before + 1)

    async def test_unask_at_the_monthly_limit_keeps_it_asked(self):
        entry = await self.record()
        await self.ask(entry.id)
        await self.db.user.update(where={"id": self.user.id}, data={"usagePeriodEntries": 150, "usagePeriodStart": datetime.now(timezone.utc)})
        r = await self.app.post(f"/entries/{entry.id}/unask")
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(await self.state(entry.id), "asked")

    async def test_only_an_asked_entry_can_be_unasked(self):
        entry = await self.record()
        self.assertEqual((await self.app.post(f"/entries/{entry.id}/unask")).status_code, 409)


class HiddenEntries(AskBase):
    async def test_asked_entries_expire_after_seven_days(self):
        from app import review

        old = await self.record()
        new = await self.record()
        for e, days in ((old, 8), (new, 6)):
            await self.db.entry.update(where={"id": e.id}, data={"reviewState": "asked", "createdAt": datetime.now(timezone.utc) - timedelta(days=days)})
        await review.discard_stale()
        self.assertIsNone(await self.db.entry.find_unique(where={"id": old.id}))
        self.assertIsNotNone(await self.db.entry.find_unique(where={"id": new.id}))

    async def test_a_question_spends_no_ai_on_actions(self):
        self.result = {**QUESTION, "wantsActions": True}
        before = await self.remaining()
        fm.script()
        await self.record("how much did I spend on food?")
        self.assertEqual(await self.remaining(), before)
        self.assertEqual(fm.requests("python"), [])


class ReviewFixes(AskBase):
    """Final review (2026-11-17): a claim that works across instances, no second AI step on unask,
    and the card knows when an entry already has a chat."""

    async def test_two_taps_at_once_send_once(self):
        import asyncio

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.3)
            return reply()

        self.send.side_effect = slow
        entry = await self.record()
        first, second = await asyncio.gather(self.app.post(f"/entries/{entry.id}/ask", json={}),
                                             self.app.post(f"/entries/{entry.id}/ask", json={}))
        self.assertEqual(sorted([first.status_code, second.status_code]), [200, 409])
        self.assertEqual(self.send.await_count, 1)

    async def test_an_ask_in_progress_is_refused_and_a_stale_one_runs_again(self):
        from prisma import Json

        entry = await self.record()
        started = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await self.db.entry.update(where={"id": entry.id}, data={"reviewState": "asking", "proposal": Json({**entry.proposal, "askStartedAt": started})})
        await self.ask(entry.id, status=409)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await self.db.entry.update(where={"id": entry.id}, data={"proposal": Json({**entry.proposal, "askStartedAt": stale})})
        pending = [e["id"] for e in (await self.app.get("/entries", params={"review": "pending"})).json()]
        self.assertIn(entry.id, pending)  # cut off by a restart: back under Needs review
        out = await self.ask(entry.id)
        self.assertEqual(out["entryState"], "asked")

    async def test_a_failed_answer_releases_the_claim(self):
        self.send.return_value = reply("unavailable")
        entry = await self.record()
        await self.ask(entry.id)
        stored = await self.db.entry.find_unique(where={"id": entry.id})
        self.assertEqual(stored.reviewState, "needs_review")
        self.assertNotIn("askStartedAt", stored.proposal)

    async def test_unask_after_ask_instead_does_not_run_the_ai_step_again(self):
        self.result = {**QUESTION, "intent": "journal"}
        entry = await self.record("paid 20 for lunch")
        await self.ask(entry.id, force=True)
        with patch("app.entry_actions.after_extraction", AsyncMock()) as step:
            r = await self.app.post(f"/entries/{entry.id}/unask")
            self.assertEqual(r.status_code, 200, r.text)
            step.assert_not_awaited()

    async def test_unask_of_a_question_runs_the_skipped_ai_step(self):
        entry = await self.record()
        await self.ask(entry.id)
        with patch("app.entry_actions.after_extraction", AsyncMock()) as step:
            await self.app.post(f"/entries/{entry.id}/unask")
            step.assert_awaited_once()

    async def test_the_card_knows_an_entry_already_has_a_chat(self):
        self.result = {**QUESTION, "intent": "both"}
        entry = await self.record()
        before = (await self.app.get(f"/entries/{entry.id}")).json()["review"]["proposal"]
        self.assertIsNone(before["conversationId"])
        await self.ask(entry.id)
        after = (await self.app.get(f"/entries/{entry.id}")).json()["review"]["proposal"]
        self.assertEqual(after["conversationId"], "conv-1")
