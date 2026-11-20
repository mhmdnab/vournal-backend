"""Message buttons (design 2026-11-19): a thumbs up or down on a reply (with an optional note), and
Try again on the last reply when it changed nothing. The model is fake_model (no paid calls)."""

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase


class Feedback(ChatBase):
    async def reply(self, text="Here you go."):
        fm.script(fm.message(fm.text(text)))
        out = await self.say("hello")
        return out["conversation"]["id"], out["messages"][1]["id"]

    async def rate(self, message_id, body):
        return await self.client.put(f"/api/assistant/messages/{message_id}/feedback", json=body)

    async def test_thumbs_up_is_stored_and_shown_with_the_reply(self):
        cid, mid = await self.reply()
        r = await self.rate(mid, {"rating": "up"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"rating": "up", "comment": None})
        conv = (await self.client.get(f"/api/assistant/conversations/{cid}")).json()
        self.assertEqual(conv["messages"][1]["feedback"], {"rating": "up", "comment": None})
        self.assertIsNone(conv["messages"][0]["feedback"])

    async def test_thumbs_down_with_a_note_replaces_the_rating(self):
        cid, mid = await self.reply()
        await self.rate(mid, {"rating": "up"})
        r = await self.rate(mid, {"rating": "down", "comment": "  It planned the wrong day.  "})
        self.assertEqual(r.json(), {"rating": "down", "comment": "It planned the wrong day."})
        self.assertEqual(await self.db.assistantfeedback.count(where={"messageId": mid}), 1)

    async def test_null_takes_the_rating_back(self):
        _, mid = await self.reply()
        await self.rate(mid, {"rating": "up"})
        r = await self.rate(mid, {"rating": None})
        self.assertEqual(r.json(), {"rating": None, "comment": None})
        self.assertEqual(await self.db.assistantfeedback.count(where={"messageId": mid}), 0)

    async def test_only_your_own_replies(self):
        _, mid = await self.reply()
        other = await self.signed_in()
        await other.patch("/api/settings", json={"aiEnabled": True})
        r = await other.put(f"/api/assistant/messages/{mid}/feedback", json={"rating": "up"})
        self.assertEqual(r.status_code, 404)

    async def test_your_own_message_cant_be_rated(self):
        fm.script(fm.message(fm.text("Hi")))
        out = await self.say("hello")
        r = await self.rate(out["messages"][0]["id"], {"rating": "up"})
        self.assertEqual(r.status_code, 400)

    async def test_a_bad_rating_or_a_long_note_is_refused(self):
        _, mid = await self.reply()
        self.assertEqual((await self.rate(mid, {"rating": "meh"})).status_code, 400)
        self.assertEqual((await self.rate(mid, {"rating": "down", "comment": "x" * 1001})).status_code, 400)


class Retry(ChatBase):
    async def test_try_again_replaces_the_last_reply(self):
        fm.script(fm.message(fm.text("First answer")), fm.message(fm.text("Second answer")))
        first = await self.say("plan my week")
        cid, old = first["conversation"]["id"], first["messages"][1]["id"]
        r = await self.client.post(f"/api/assistant/messages/{old}/retry")
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual((out["status"], [m["text"] for m in out["messages"]]), ("ok", ["plan my week", "Second answer"]))
        self.assertEqual(out["remainingThisMonth"], first["remainingThisMonth"] - 1)  # a try again is one request
        conv = (await self.client.get(f"/api/assistant/conversations/{cid}")).json()
        self.assertEqual([m["text"] for m in conv["messages"]], ["plan my week", "Second answer"])
        self.assertIsNone(await self.db.assistantmessage.find_unique(where={"id": old}))

    async def test_only_the_last_reply(self):
        fm.script(fm.message(fm.text("One")), fm.message(fm.text("Two")))
        first = await self.say("first")
        await self.say("second", first["conversation"]["id"])
        r = await self.client.post(f"/api/assistant/messages/{first['messages'][1]['id']}/retry")
        self.assertEqual(r.status_code, 400)

    async def test_a_reply_that_changed_something_cant_be_tried_again(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Added it.")))
        out = await self.say("add gym")
        reply = out["messages"][1]
        self.assertEqual(reply["actions"][0]["status"], "done")
        r = await self.client.post(f"/api/assistant/messages/{reply['id']}/retry")
        self.assertEqual(r.status_code, 409)
        self.assertIn("undo", r.json()["message"].lower())

    async def test_a_reply_whose_change_was_undone_can_be_tried_again(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Added it.")), fm.message(fm.text("Fresh answer")))
        out = await self.say("add gym")
        reply = out["messages"][1]
        await self.client.post(f"/api/assistant/actions/{reply['actions'][0]['id']}/undo")
        r = await self.client.post(f"/api/assistant/messages/{reply['id']}/retry")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["messages"][-1]["text"], "Fresh answer")

    async def test_someone_elses_reply_is_not_found(self):
        fm.script(fm.message(fm.text("Hi")))
        out = await self.say("hello")
        other = await self.signed_in()
        r = await other.post(f"/api/assistant/messages/{out['messages'][1]['id']}/retry")
        self.assertEqual(r.status_code, 404)


class RetryAlwaysAsk(ChatBase):
    async def test_the_apps_try_again_asks_before_every_change_too(self):
        fm.script(fm.message(fm.text("Hmm")), fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        first = await self.say("add gym")
        r = await self.client.post(f"/api/assistant/messages/{first['messages'][1]['id']}/retry", json={"alwaysAsk": True})
        self.assertEqual(r.status_code, 200, r.text)
        reply = r.json()["messages"][-1]
        self.assertEqual(reply["actions"][0]["status"], "pending")
        self.assertEqual(await self.db.task.count(where={"userId": self.client.user.id}), 0)


class RetryGuards(ChatBase):
    """Review fixes (2026-11-20): a double tap, import turns, and expired cards."""

    async def visible(self, cid):
        return [m["text"] for m in (await self.client.get(f"/api/assistant/conversations/{cid}")).json()["messages"]]

    async def test_a_double_tap_tries_again_once(self):
        import asyncio

        fm.script(fm.message(fm.text("A1")), fm.message(fm.text("A2")), fm.message(fm.text("A3")))
        first = await self.say("plan my week")
        cid, old = first["conversation"]["id"], first["messages"][1]["id"]
        before = first["remainingThisMonth"]
        rs = await asyncio.gather(*[self.client.post(f"/api/assistant/messages/{old}/retry") for _ in range(2)])
        self.assertEqual(sorted(r.status_code for r in rs), [200, 409], [r.text for r in rs])
        self.assertEqual(await self.visible(cid), ["plan my week", "A2"])
        ok = next(r for r in rs if r.status_code == 200).json()
        self.assertEqual(ok["remainingThisMonth"], before - 1)

    async def test_an_import_report_cant_be_tried_again(self):
        from app.os import assistant

        fm.script(fm.message(fm.text("Report: added 3 things")))
        res = await assistant.send(self.client.user, "THE REAL REPORT PROMPT", None, display="Import report", importing=True)
        cid, mid = res["conversation"]["id"], res["messages"][1]["id"]
        r = await self.client.post(f"/api/assistant/messages/{mid}/retry")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(await self.visible(cid), ["Import report", "Report: added 3 things"])

    async def test_a_running_import_blocks_try_again(self):
        from unittest.mock import patch

        fm.script(fm.message(fm.text("A1")))
        first = await self.say("plan my week")
        with patch("app.os.assistant_import.import_running", lambda cid: True):
            r = await self.client.post(f"/api/assistant/messages/{first['messages'][1]['id']}/retry")
        self.assertEqual(r.status_code, 409, r.text)

    async def test_an_expired_waiting_card_doesnt_block_try_again(self):
        from datetime import datetime, timedelta, timezone

        await self.client.patch("/api/settings", json={"assistantMode": "manual"})
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")), fm.message(fm.text("Fresh")))
        out = await self.say("add gym")
        reply = out["messages"][1]
        self.assertEqual(reply["actions"][0]["status"], "pending")
        await self.db.assistantaction.update(where={"id": reply["actions"][0]["id"]}, data={"createdAt": datetime.now(timezone.utc) - timedelta(hours=25)})
        r = await self.client.post(f"/api/assistant/messages/{reply['id']}/retry")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["messages"][-1]["text"], "Fresh")
