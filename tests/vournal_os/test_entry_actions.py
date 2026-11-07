"""The AI actions step on an app recording (design 2026-11-06): for a Plus user who allowed AI
actions, an entry that mentions something to act on gets proposed dashboard actions, each one
waiting for the review card's Save. The models are patched / fake (no paid calls)."""

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from vournal_os.parity import fake_model as fm
from vournal_os.support import OsTestCase

RESULT = {
    "summary": "I paid 20 for lunch with Ali and gave him a book.",
    "mood": {"score": 4, "label": "good", "emotions": []},
    "todos": [{"text": "Call mom", "dueDate": None, "time": None}],
    "topics": ["friends"],
    "people": ["Ali"],
    "places": [],
    "wantsActions": True,
    "journalSuggested": True,
}


GIFT_FOR_ALI = ("add_gift", {"person_id": "proposal-person:0", "title": "A book", "given": True})


class EntryActionsBase(OsTestCase):
    result = RESULT

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app import main
        from app.os import ai

        fm.start()
        fm.script()  # no replies, and no requests left from an earlier test
        for patcher in (patch.object(ai, "_client", fm.python_client()),
                        patch.object(main, "extract", AsyncMock(side_effect=lambda *a, **k: (copy.deepcopy(self.result), None)))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = await self.mobile()
        self.user = self.app.user
        await self.allow(True)

    async def allow(self, enabled: bool, asked_at=None):
        from app.os.profile import ensure_os_profile

        await ensure_os_profile(self.user)
        await self.db.usersettings.update(where={"userId": self.user.id}, data={"aiEnabled": enabled, "aiActionsAskedAt": asked_at})

    async def record(self, text="paid 20 for lunch with Ali, gave him a book"):
        r = await self.app.post("/entries", json={"transcript": text, "review": True})
        self.assertEqual(r.status_code, 200, r.text)
        return await self.db.entry.find_unique(where={"id": r.json()["id"]})

    async def pending(self, entry_id):
        return await self.db.assistantaction.find_many(where={"entryId": entry_id}, order={"position": "asc"})

    async def remaining(self):
        from app.os import ai

        return await ai.remaining(self.user.id, "UTC")


class WhenItRuns(EntryActionsBase):
    async def test_free_user_gets_no_step(self):
        self.app = await self.mobile(await self.make_user(plan="free"))
        self.user = self.app.user
        await self.allow(True)
        await self.record()
        self.assertEqual(fm.requests("python"), [])

    async def test_plain_diary_entry_gets_no_step(self):
        self.result = {**RESULT, "wantsActions": False}
        before = await self.remaining()
        entry = await self.record("a quiet day")
        self.assertEqual(fm.requests("python"), [])
        self.assertEqual(await self.remaining(), before)
        self.assertNotIn("skipped", entry.proposal)

    async def test_no_consent_marks_consent_needed_and_sends_nothing(self):
        await self.allow(False)
        entry = await self.record()
        self.assertEqual(fm.requests("python"), [])
        self.assertEqual(entry.proposal["skipped"], "consent_needed")

    async def test_consent_asked_recently_marks_consent_off(self):
        await self.allow(False, asked_at=datetime.now(timezone.utc) - timedelta(days=2))
        entry = await self.record()
        self.assertEqual(fm.requests("python"), [])
        self.assertEqual(entry.proposal["skipped"], "consent_off")

    async def test_consent_asked_long_ago_asks_again(self):
        await self.allow(False, asked_at=datetime.now(timezone.utc) - timedelta(days=8))
        self.assertEqual((await self.record()).proposal["skipped"], "consent_needed")

    async def test_limit_reached_skips_with_reason(self):
        from app.os import ai

        with patch.object(ai, "MONTHLY_LIMIT", 0):
            entry = await self.record()
        self.assertEqual(fm.requests("python"), [])
        self.assertEqual(entry.proposal["skipped"], "limit")


class TheStep(EntryActionsBase):
    async def test_step_spends_one_and_defers_every_write(self):
        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"})), fm.message(fm.text("Proposed.")))
        before = await self.remaining()
        entry = await self.record()
        actions = await self.pending(entry.id)
        self.assertEqual([(a.status, a.kind, a.entityType, a.label) for a in actions], [("pending", "create", "transaction", "Lunch")])
        self.assertEqual(actions[0].call["name"], "add_transaction")
        self.assertEqual(await self.db.transaction.count(where={"userId": self.user.id}), 0)
        self.assertEqual(await self.remaining(), before - 1)
        self.assertEqual(entry.status, "DONE")
        self.assertNotIn("skipped", entry.proposal)

    async def test_step_has_no_create_task(self):
        fm.script(fm.message(fm.text("Nothing to do.")))
        await self.record()
        names = {t["name"] for t in fm.requests("python")[0]["body"]["tools"]}
        self.assertFalse(names & {"create_task", "create_event", "create_reminder"})
        self.assertTrue({"ask_user", "withdraw_proposal", "add_transaction", "add_gift", "log_habit", "search"} <= names)

    async def test_new_person_placeholder_is_offered(self):
        fm.script(fm.message(fm.text("ok")))
        await self.record()
        first = fm.requests("python")[0]["body"]["messages"][0]["content"][0]["text"]
        self.assertIn("proposal-person:0", first)
        self.assertIn("Call mom", first)  # the to-dos it must not repeat

    async def test_known_person_comes_with_their_id(self):
        ali = await self.db.person.create(data={"userId": self.user.id, "name": "Ali", "normalizedName": "ali"})
        fm.script(fm.message(fm.text("ok")))
        await self.record()
        first = fm.requests("python")[0]["body"]["messages"][0]["content"][0]["text"]
        self.assertIn(ali.id, first)
        self.assertNotIn("proposal-person:0", first)

    async def test_gift_for_a_new_person_waits_with_their_name(self):
        fm.script(fm.tools(("add_gift", {"person_id": "proposal-person:0", "title": "A book", "given": True})), fm.message(fm.text("ok")))
        entry = await self.record()
        (a,) = await self.pending(entry.id)
        self.assertEqual((a.status, a.entityType, a.label), ("pending", "gift", "A book → Ali"))

    async def test_ask_user_stores_question_and_stops(self):
        fm.script(fm.tools(("ask_user", {"question": "Cash or card?", "choices": ["Cash", "Card"], "allowText": False})))
        entry = await self.record()
        q = entry.proposal["question"]
        self.assertEqual((q["question"], q["choices"], q["allowText"]), ("Cash or card?", ["Cash", "Card"], False))
        self.assertTrue(q["id"])
        self.assertEqual(len(fm.requests("python")), 1)

    async def test_answer_rerun_replaces_what_it_withdraws_without_spending(self):
        from app import entry_actions

        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"}),
                           ("ask_user", {"question": "Cash or card?", "choices": ["Cash", "Card"], "allowText": True})))
        before = await self.remaining()
        entry = await self.record()
        (first,) = await self.pending(entry.id)
        fm.script(fm.tools(("withdraw_proposal", {"id": f"pending:{first.id}"}),
                           ("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch (card)"})), fm.message(fm.text("ok")))
        await entry_actions.run_step(entry, spend=False, answer={"choice": "Card"})
        rows = await self.pending(entry.id)
        self.assertEqual({(a.id == first.id, a.status) for a in rows}, {(True, "cancelled"), (False, "pending")})
        self.assertEqual(await self.remaining(), before - 1)
        entry = await self.db.entry.find_unique(where={"id": entry.id})
        self.assertEqual((entry.proposal["answers"], entry.proposal.get("question")), (1, None))
        sent = fm.requests("python")[-2]["body"]["messages"]
        self.assertIn("Card", json.dumps(sent[-1], ensure_ascii=False))

    async def test_a_proposal_withdrawn_in_the_same_run_never_reaches_the_card(self):
        from types import SimpleNamespace

        from app.os import assistant_pending

        ids = iter(["first", "second"])
        fake_uuid = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(ids)))
        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"})),
                  fm.tools(("withdraw_proposal", {"id": "pending:first"}),
                           ("add_transaction", {"type": "expense", "amount": 25, "title": "Lunch"})),
                  fm.message(fm.text("ok")))
        with patch.object(assistant_pending, "uuid", fake_uuid):
            entry = await self.record()
        rows = await self.pending(entry.id)
        self.assertEqual([(a.id, a.call["input"]["amount"]) for a in rows], [("second", 25)])

    async def test_answer_rerun_keeps_actions_it_does_not_repeat(self):
        from app import entry_actions

        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"}),
                           ("ask_user", {"question": "What was the gift?", "allowText": True})))
        entry = await self.record()
        (lunch,) = await self.pending(entry.id)
        fm.script(fm.tools(GIFT_FOR_ALI), fm.message(fm.text("ok")))
        await entry_actions.run_step(entry, spend=False, answer={"text": "a book"})
        rows = {a.call["name"]: a.status for a in await self.pending(entry.id)}
        self.assertEqual(rows, {"add_transaction": "pending", "add_gift": "pending"})

    async def test_answer_rerun_that_proposes_nothing_keeps_everything(self):
        from app import entry_actions

        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"}),
                           ("ask_user", {"question": "Cash or card?", "choices": ["Cash", "Card"]})))
        entry = await self.record()
        fm.script(fm.message(fm.text("Got it.")))
        await entry_actions.run_step(entry, spend=False, answer={"choice": "Cash"})
        self.assertEqual([a.status for a in await self.pending(entry.id)], ["pending"])

    async def test_failed_rerun_keeps_previous_actions(self):
        from app import entry_actions

        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"})), fm.message(fm.text("ok")))
        entry = await self.record()
        (first,) = await self.pending(entry.id)
        fm.script(fm.error(500, "api_error"), fm.error(500, "api_error"), fm.error(500, "api_error"))
        await entry_actions.run_step(entry, spend=False, answer={"text": "card"})
        (still,) = await self.pending(entry.id)
        self.assertEqual((still.id, still.status), (first.id, "pending"))
        entry = await self.db.entry.find_unique(where={"id": entry.id})
        self.assertNotIn("skipped", entry.proposal)

    async def test_failed_first_run_marks_unavailable(self):
        fm.script(fm.error(500, "api_error"), fm.error(500, "api_error"), fm.error(500, "api_error"))
        entry = await self.record()
        self.assertEqual((entry.status, entry.proposal["skipped"]), ("DONE", "unavailable"))

    async def test_entry_actions_never_expire_while_waiting(self):
        from app.os import assistant

        fm.script(fm.tools(("add_transaction", {"type": "expense", "amount": 20})), fm.message(fm.text("ok")))
        entry = await self.record()
        (a,) = await self.pending(entry.id)
        await self.db.assistantaction.update(where={"id": a.id}, data={"createdAt": datetime.now(timezone.utc) - timedelta(days=3)})
        a = await self.db.assistantaction.find_unique(where={"id": a.id})
        self.assertFalse(assistant._expired(a))

    async def test_entry_conversation_not_in_dashboard_list(self):
        fm.script(fm.message(fm.text("ok")))
        await self.record()
        web = await self.signed_in(self.user)
        self.assertEqual((await web.get("/api/assistant/conversations")).json()["items"], [])
