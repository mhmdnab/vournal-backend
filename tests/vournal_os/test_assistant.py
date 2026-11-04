"""The chatbot's own rules, past what parity reaches: the 160-turn cap, a deletion that waits
expiring after 24 hours, each message spending the shared allowance, history replayed in the
order written even when a round's two rows share a millisecond, the fallback trimming, and
titles cut by JavaScript's length. The model is fake_model (no paid calls)."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from prisma import Json

from vournal_os.parity import fake_model as fm
from vournal_os.support import OsTestCase

GOLDEN = json.loads((Path(__file__).parent / "golden" / "assistant.json").read_text())


class Pure(unittest.TestCase):
    def test_context_titles_and_replay(self):
        from app.os import assistant

        for case in GOLDEN["context"]:
            c = case["c"]
            self.assertEqual(assistant.context_block(c["now"], c["weekday"], c["timezone"], c["language"], c["currency"], c["name"]), case["out"])
        self.assertEqual(assistant.title_from("  buy\n milk  "), "buy milk")
        self.assertEqual(assistant.title_from("😀" * 31), "😀" * 28 + "…")  # 62 UTF-16 units: cut at 57
        blocks = [{"type": "thinking"}, {"type": "tool_use", "id": "a"}, {"type": "text", "text": "x"}, {"type": "fallback"},
                  {"type": "thinking"}, {"type": "tool_use", "id": "b"}]
        self.assertEqual(assistant.for_replay(blocks), [{"type": "text", "text": "x"}, {"type": "fallback"}, {"type": "thinking"}, {"type": "tool_use", "id": "b"}])
        self.assertEqual(assistant.for_replay(blocks[:3]), blocks[:3])


class Chat(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import ai

        fm.start()
        patcher = patch.object(ai, "_client", fm.python_client())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = await self.signed_in()
        await self.client.patch("/api/settings", json={"timezone": "Asia/Beirut", "aiEnabled": True})

    async def say(self, message, conversation=None):
        response = await self.client.post("/api/assistant/chat", json={"message": message, **({"conversationId": conversation} if conversation else {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_each_message_spends_the_allowance(self):
        fm.script(fm.message(fm.text("Hi!")), fm.message(fm.text("Hi again!")))
        first = await self.say("hello")
        self.assertEqual((first["status"], first["remainingToday"], first["messages"][1]["text"]), ("ok", 2999, "Hi!"))
        from app.os import ai

        with patch.object(ai, "MONTHLY_LIMIT", 2):
            self.assertEqual((await self.say("again", first["conversation"]["id"]))["remainingToday"], 0)
            self.assertEqual((await self.say("more"))["status"], "limit")
        self.assertEqual(len(fm.requests("python")), 2)

    async def test_a_full_chat_and_a_stale_deletion(self):
        fm.script(fm.message(fm.text("ok")))
        conv = (await self.say("start"))["conversation"]["id"]
        await self.db.assistantmessage.create_many(data=[{"conversationId": conv, "role": "user", "content": Json([{"type": "text", "text": "x"}]), "hidden": True}
                                                        for _ in range(158)])
        full = await self.say("one more", conv)
        self.assertEqual((full["status"], full["messages"]), ("too_long", []))
        self.assertTrue((await self.client.get(f"/api/assistant/conversations/{conv}")).json()["full"])

        task = (await self.client.post("/api/tasks", json={"title": "Old"})).json()
        fm.script(fm.tools(("delete_item", {"type": "task", "id": task["id"]})), fm.message(fm.text("Confirm below.")))
        pending = (await self.say("delete Old"))["messages"][1]["actions"][0]
        await self.db.assistantaction.update(where={"id": pending["id"]}, data={"createdAt": datetime.now(timezone.utc) - timedelta(hours=25)})
        expired = await self.client.post(f"/api/assistant/actions/{pending['id']}/confirm")
        self.assertEqual((expired.status_code, expired.json()["message"]), (400, "This request expired — ask again"))
        self.assertEqual((await self.client.get(f"/api/tasks/{task['id']}")).status_code, 200)  # still there

    async def test_history_replays_in_the_order_written(self):
        fm.script(fm.tools(("list_tasks", {})), fm.message(fm.text("none")), fm.message(fm.text("ok")))
        conv = (await self.say("my tasks?"))["conversation"]["id"]
        # every row in one millisecond: only the order written can tell them apart
        await self.db.assistantmessage.update_many(where={"conversationId": conv}, data={"createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc)})
        await self.say("thanks", conv)
        replay = fm.requests("python")[-1]["body"]["messages"]
        self.assertEqual([(m["role"], m["content"][-1]["type"]) for m in replay],
                         [("user", "text"), ("assistant", "tool_use"), ("user", "tool_result"), ("assistant", "text"), ("user", "text")])

    async def test_conversations_stay_with_their_owner(self):
        fm.script(fm.message(fm.text("ok")))
        conv = (await self.say("mine"))["conversation"]["id"]
        stranger = await self.signed_in()
        await stranger.patch("/api/settings", json={"aiEnabled": True})  # else "disabled" answers first, as in NestJS
        for method, path in (("GET", f"/api/assistant/conversations/{conv}"), ("DELETE", f"/api/assistant/conversations/{conv}")):
            self.assertEqual((await stranger.request(method, path)).status_code, 404)
        self.assertEqual((await stranger.post("/api/assistant/chat", json={"message": "hi", "conversationId": conv})).status_code, 404)
        self.assertEqual((await stranger.get("/api/assistant/conversations")).json(), {"items": [], "total": 0})
        free = await self.signed_in(await self.make_user(plan="free"))
        self.assertEqual((await free.post("/api/assistant/chat", json={"message": "hi"})).status_code, 403)


if __name__ == "__main__":
    unittest.main()
