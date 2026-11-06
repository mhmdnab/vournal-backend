"""The review card's routes (design 2026-11-06): what the card shows, Save (only what the user
kept, once), Discard, answering the AI's question, Undo, the last day's AI actions, and the AI
actions consent. Models patched / fake (no paid calls)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from prisma import Json

from vournal_os.parity import fake_model as fm
from vournal_os.test_entry_actions import RESULT, EntryActionsBase

LUNCH = ("add_transaction", {"type": "expense", "amount": 20, "title": "Lunch"})
GIFT = ("add_gift", {"person_id": "proposal-person:0", "title": "A book", "given": True})


class ReviewRoutes(EntryActionsBase):
    async def card(self, *uses):
        fm.script(fm.tools(*uses), fm.message(fm.text("ok"))) if uses else fm.script(fm.message(fm.text("ok")))
        r = await self.app.post("/entries", json={"transcript": "paid 20 for lunch with Ali, gave him a book", "review": True})
        self.assertEqual(r.status_code, 200, r.text)
        return (await self.app.get(f"/entries/{r.json()['id']}")).json()

    def body(self, e, **over):
        review = e["review"]
        body = {
            "inJournal": True,
            "todos": [{"index": t["index"], "keep": True} for t in review["proposal"]["todos"]],
            "people": [{"index": p["index"], "keep": True} for p in review["proposal"]["people"]],
            "topics": [{"index": t["index"], "keep": True} for t in review["proposal"]["topics"]],
            "places": [],
            "actions": [{"id": a["id"], "keep": True} for a in review["actions"]],
        }
        body.update(over)
        return body

    async def save(self, e, status=200, **over):
        r = await self.app.post(f"/entries/{e['id']}/save", json=self.body(e, **over))
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    async def test_review_block_lists_proposal_and_pending_actions(self):
        e = await self.card(LUNCH, GIFT)
        review = e["review"]
        self.assertEqual(review["proposal"]["todos"], [{"index": 0, "text": "Call mom", "dueDate": None}])
        self.assertEqual(review["proposal"]["people"], [{"index": 0, "name": "Ali"}])
        self.assertTrue(review["proposal"]["journalSuggested"])
        self.assertEqual([(a["tool"], a["status"], a["dependsOnPeople"]) for a in review["actions"]],
                         [("add_transaction", "pending", []), ("add_gift", "pending", [0])])
        self.assertEqual(review["actions"][0]["details"]["amount"], 20)
        self.assertEqual((review["question"], review["skipped"], review["answersLeft"]), (None, None, 3))
        again = (await self.app.get(f"/entries/{e['id']}")).json()
        self.assertEqual([a["id"] for a in again["review"]["actions"]], [a["id"] for a in review["actions"]])

    async def test_save_creates_only_kept_items(self):
        e = await self.card(LUNCH)
        saved = await self.save(e, topics=[{"index": 0, "keep": False}])
        self.assertEqual(saved["reviewState"], "saved")
        self.assertEqual(await self.db.todo.count(where={"entryId": e["id"]}), 1)
        self.assertEqual(await self.db.entrytopic.count(where={"entryId": e["id"]}), 0)
        self.assertEqual(await self.db.entryperson.count(where={"entryId": e["id"]}), 1)
        self.assertEqual([(a["tool"], a["status"]) for a in saved["review"]["actions"]], [("add_transaction", "done")])
        t = await self.db.transaction.find_first(where={"userId": self.user.id})
        self.assertEqual((float(t.amount), t.title), (20.0, "Lunch"))
        self.assertEqual([x["id"] for x in (await self.app.get("/entries")).json()], [e["id"]])

    async def test_save_with_edited_todo_and_unticked_action(self):
        e = await self.card(LUNCH)
        await self.save(e, todos=[{"index": 0, "keep": True, "text": "Call mom tonight"}], actions=[{"id": e["review"]["actions"][0]["id"], "keep": False}])
        self.assertEqual([t.text for t in await self.db.todo.find_many(where={"entryId": e["id"]})], ["Call mom tonight"])
        self.assertEqual(await self.db.transaction.count(where={"userId": self.user.id}), 0)
        a = await self.db.assistantaction.find_unique(where={"id": e["review"]["actions"][0]["id"]})
        self.assertEqual(a.status, "cancelled")

    async def test_save_resolves_new_person_placeholder(self):
        e = await self.card(GIFT)
        saved = await self.save(e)
        self.assertEqual(saved["review"]["actions"][0]["status"], "done")
        ali = await self.db.person.find_first(where={"userId": self.user.id, "normalizedName": "ali"})
        gift = await self.db.giftidea.find_first(where={"personId": ali.id})
        self.assertEqual((gift.title, gift.isPurchased), ("A book", True))

    async def test_save_drops_action_whose_person_was_unticked(self):
        e = await self.card(GIFT)
        saved = await self.save(e, people=[{"index": 0, "keep": False}])
        self.assertEqual(saved["review"]["actions"], [])
        self.assertEqual(await self.db.giftidea.count(), 0)
        self.assertEqual(await self.db.person.count(where={"userId": self.user.id}), 0)

    async def test_save_rejects_bad_edit_before_writing_anything(self):
        e = await self.card(LUNCH)
        bad = [{"id": e["review"]["actions"][0]["id"], "keep": True, "input": {"amount": "lots"}}]
        r = await self.app.post(f"/entries/{e['id']}/save", json=self.body(e, actions=bad))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["field"], "amount")
        self.assertEqual(await self.db.todo.count(where={"entryId": e["id"]}), 0)
        self.assertEqual((await self.db.entry.find_unique(where={"id": e["id"]})).reviewState, "needs_review")

    async def test_save_with_edited_amount(self):
        e = await self.card(LUNCH)
        await self.save(e, actions=[{"id": e["review"]["actions"][0]["id"], "keep": True, "input": {"amount": 25}}])
        t = await self.db.transaction.find_first(where={"userId": self.user.id})
        self.assertEqual(float(t.amount), 25.0)

    async def test_save_twice_returns_the_same_result(self):
        e = await self.card(LUNCH)
        first = await self.save(e)
        second = await self.save(e)
        self.assertEqual([a["id"] for a in second["review"]["actions"]], [a["id"] for a in first["review"]["actions"]])
        self.assertEqual(await self.db.transaction.count(where={"userId": self.user.id}), 1)
        self.assertEqual(await self.db.todo.count(where={"entryId": e["id"]}), 1)

    async def test_save_with_journal_off_hides_entry(self):
        e = await self.card(LUNCH)
        saved = await self.save(e, inJournal=False)
        self.assertFalse(saved["inJournal"])
        self.assertEqual((await self.app.get("/entries")).json(), [])
        self.assertEqual(len((await self.app.get("/todos")).json()), 1)

    async def test_discard_deletes_entry_and_actions(self):
        e = await self.card(LUNCH)
        r = await self.app.post(f"/entries/{e['id']}/discard")
        self.assertEqual(r.status_code, 204)
        self.assertIsNone(await self.db.entry.find_unique(where={"id": e["id"]}))
        self.assertEqual(await self.db.assistantaction.count(where={"userId": self.user.id}), 0)
        saved = await self.card()
        await self.save(saved)
        self.assertEqual((await self.app.post(f"/entries/{saved['id']}/discard")).status_code, 409)

    async def test_undo_within_24h_and_not_after(self):
        e = await self.card(LUNCH)
        saved = await self.save(e)
        aid = saved["review"]["actions"][0]["id"]
        self.assertTrue(saved["review"]["actions"][0]["canUndo"])
        r = await self.app.post(f"/entries/{e['id']}/actions/{aid}/undo")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "undone"))
        self.assertEqual(await self.db.transaction.count(where={"userId": self.user.id}), 0)
        e2 = await self.card(LUNCH)
        saved2 = await self.save(e2)
        aid2 = saved2["review"]["actions"][0]["id"]
        await self.db.assistantaction.update(where={"id": aid2}, data={"resolvedAt": datetime.now(timezone.utc) - timedelta(hours=25)})
        self.assertEqual((await self.app.post(f"/entries/{e2['id']}/actions/{aid2}/undo")).status_code, 400)

    async def test_recent_lists_only_last_24h_done_entry_actions(self):
        e = await self.card(LUNCH)
        saved = await self.save(e)
        aid = saved["review"]["actions"][0]["id"]
        recent = (await self.app.get("/entries/actions/recent")).json()
        self.assertEqual([(a["id"], a["entryId"]) for a in recent], [(aid, e["id"])])
        await self.db.assistantaction.update(where={"id": aid}, data={"resolvedAt": datetime.now(timezone.utc) - timedelta(hours=25)})
        self.assertEqual((await self.app.get("/entries/actions/recent")).json(), [])

    async def test_dashboard_cannot_approve_entry_actions(self):
        e = await self.card(LUNCH)
        aid = e["review"]["actions"][0]["id"]
        web = await self.signed_in(self.user)
        self.assertEqual((await web.post(f"/api/assistant/actions/{aid}/approve")).status_code, 404)
        self.assertEqual((await web.post(f"/api/assistant/actions/{aid}/cancel")).status_code, 404)
        self.assertEqual(await self.db.transaction.count(where={"userId": self.user.id}), 0)

    async def test_ai_actions_consent_toggles_aiEnabled_and_is_plus_only(self):
        self.assertEqual((await self.app.get("/me/ai-actions")).json(), {"enabled": True})
        r = await self.app.post("/me/ai-actions", json={"enabled": False})
        self.assertEqual(r.json(), {"enabled": False})
        s = await self.db.usersettings.find_unique(where={"userId": self.user.id})
        self.assertEqual(s.aiEnabled, False)
        self.assertIsNotNone(s.aiActionsAskedAt)
        free = await self.mobile(await self.make_user(plan="free"))
        r = await free.get("/me/ai-actions")
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (403, "plus_required"))

    async def test_propose_after_consent_runs_and_spends_once(self):
        await self.allow(False)
        e = await self.card()
        self.assertEqual(e["review"]["skipped"], "consent_needed")
        await self.app.post("/me/ai-actions", json={"enabled": True})
        before = await self.remaining()
        fm.script(fm.tools(LUNCH), fm.message(fm.text("ok")))
        r = await self.app.post(f"/entries/{e['id']}/propose", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([a["tool"] for a in r.json()["review"]["actions"]], ["add_transaction"])
        self.assertIsNone(r.json()["review"]["skipped"])
        self.assertEqual(await self.remaining(), before - 1)

    async def test_answer_runs_without_spending_and_stops_after_three(self):
        fm.script(fm.tools(("ask_user", {"question": "Cash or card?", "choices": ["Cash", "Card"]})))
        r = await self.app.post("/entries", json={"transcript": "paid 20", "review": True})
        e = (await self.app.get(f"/entries/{r.json()['id']}")).json()
        self.assertEqual(e["review"]["question"]["question"], "Cash or card?")
        before = await self.remaining()
        for left in (2, 1, 0):
            fm.script(fm.tools(("ask_user", {"question": "And?", "choices": ["A"]})))
            r = await self.app.post(f"/entries/{e['id']}/propose", json={"answer": {"choice": "Card"}})
            self.assertEqual((r.status_code, r.json()["review"]["answersLeft"]), (200, left))
        self.assertEqual(await self.remaining(), before)
        r = await self.app.post(f"/entries/{e['id']}/propose", json={"answer": {"choice": "Card"}})
        self.assertEqual(r.status_code, 400)


class ReviewFixes(ReviewRoutes):
    async def test_pending_list_and_buttons_wait_for_the_ai_step(self):
        e = await self.card(LUNCH)
        await self.db.entry.update(where={"id": e["id"]}, data={"status": "PROCESSING"})
        self.assertEqual((await self.app.get("/entries?review=pending")).json(), [])
        self.assertEqual((await self.app.post(f"/entries/{e['id']}/save", json=self.body(e))).status_code, 409)
        self.assertEqual((await self.app.post(f"/entries/{e['id']}/discard")).status_code, 409)

    async def test_save_after_a_failure_part_way_does_not_repeat_todos(self):
        from app.os import assistant

        e = await self.card(LUNCH)
        with patch.object(assistant, "_approve", AsyncMock(side_effect=RuntimeError("crash"))):
            with self.assertRaises(RuntimeError):
                await self.app.post(f"/entries/{e['id']}/save", json=self.body(e))
        self.assertEqual(await self.db.todo.count(where={"entryId": e["id"]}), 1)
        saved = await self.save(e)
        self.assertEqual(saved["reviewState"], "saved")
        self.assertEqual(await self.db.todo.count(where={"entryId": e["id"]}), 1)
        self.assertEqual(await self.db.transaction.count(where={"userId": self.user.id}), 1)

    async def _saving_since(self, e, minutes):
        row = await self.db.entry.find_unique(where={"id": e["id"]})
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        await self.db.entry.update(where={"id": e["id"]}, data={"reviewState": "saving", "proposal": Json({**row.proposal, "savingAt": since})})

    async def test_a_save_cut_off_part_way_can_be_saved_later(self):
        e = await self.card(LUNCH)
        await self._saving_since(e, 10)
        self.assertEqual([x["id"] for x in (await self.app.get("/entries?review=pending")).json()], [e["id"]])
        self.assertEqual((await self.save(e))["reviewState"], "saved")

    async def test_a_save_in_progress_still_answers_409(self):
        e = await self.card(LUNCH)
        await self._saving_since(e, 0)
        self.assertEqual((await self.app.post(f"/entries/{e['id']}/save", json=self.body(e))).status_code, 409)

    async def test_an_edit_card_shows_the_old_value(self):
        web = await self.signed_in(self.user)
        t = (await web.post("/api/transactions", json={"type": "EXPENSE", "amount": 20, "title": "Lunch"})).json()
        e = await self.card(("update_transaction", {"id": t["id"], "amount": 25}))
        (a,) = e["review"]["actions"]
        self.assertEqual((a["kind"], a["details"]["amount"], a["before"]["amount"]), ("update", 25, 20.0))
