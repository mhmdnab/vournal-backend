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


class ChatBase(OsTestCase):
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


class Chat(ChatBase):
    async def test_each_message_spends_the_allowance(self):
        fm.script(fm.message(fm.text("Hi!")), fm.message(fm.text("Hi again!")))
        first = await self.say("hello")
        self.assertEqual((first["status"], first["remainingThisMonth"], first["messages"][1]["text"]), ("ok", 2999, "Hi!"))
        from app.os import ai

        with patch.object(ai, "MONTHLY_LIMIT", 2):
            self.assertEqual((await self.say("again", first["conversation"]["id"]))["remainingThisMonth"], 0)
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

    async def test_a_cut_off_reply_never_breaks_the_chat(self):
        """A reply stopped by max_tokens mid tool call is stored without the call, or as the
        canned text when nothing else is left, so the next message replays a valid history."""
        cut = fm.message({"type": "thinking", "thinking": "t", "signature": "s"}, fm.text("Logging"),
                         fm.tool_use("log_food", {"name": "Rice"}), stop="max_tokens")
        empty = fm.message({"type": "thinking", "thinking": "t", "signature": "s"}, fm.tool_use("log_food", {}), stop="max_tokens")
        fm.script(cut, empty, fm.message(fm.text("ok")))
        conv = (await self.say("log my lunch"))["conversation"]["id"]
        second = await self.say("and dinner", conv)
        self.assertEqual(second["messages"][1]["text"], "Something went wrong on my side. Please try again.")
        await self.say("thanks", conv)
        replay = fm.requests("python")[-1]["body"]["messages"]
        uses = [b for m in replay if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
        self.assertEqual(uses, [])
        self.assertEqual([b["type"] for b in replay[1]["content"]], ["thinking", "text"])
        self.assertEqual(replay[3]["content"], [{"type": "text", "text": "Something went wrong on my side. Please try again."}])

    async def test_undo_puts_back_what_was_there(self):
        await self.client.put("/api/wellness/weight", json={"weightKg": 80})
        task = (await self.client.post("/api/tasks", json={"title": "Gym", "dueAt": "2026-11-02", "rrule": "FREQ=WEEKLY"})).json()
        fm.script(fm.tools(("log_weight", {"kg": 81}), ("log_weight", {"kg": 70, "date": "2026-10-01"}), ("update_task", {"id": task["id"], "status": "done"})),
                  fm.message(fm.text("ok")))
        actions = (await self.say("weigh, tick gym"))["messages"][1]["actions"]
        for a in actions:
            self.assertEqual((await self.client.post(f"/api/assistant/actions/{a['id']}/undo")).status_code, 200, a)
        weights = (await self.client.get("/api/wellness/weight?days=365")).json()["items"]
        self.assertEqual([w["weightKg"] for w in weights], [80])  # today's 80 restored; the new 1 October entry removed
        tasks = (await self.client.get("/api/tasks?status=TODO")).json()["items"]
        self.assertEqual([(t["id"], t["title"]) for t in tasks], [(task["id"], "Gym")])  # reopened, and next week's copy gone

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


class AppTodos(ChatBase):
    """The app's to-dos in the chat: listed with the web's tasks and on the agenda, ticked and
    rescheduled through the app's own rules, never deleted; and no journal text anywhere."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        uid = self.client.user.id
        await self.db.user.update(where={"id": uid}, data={"timezone": "Asia/Beirut"})  # the app's own zone, as the phone sets it
        self.entry = await self.db.entry.create(data={"userId": uid, "rawTranscript": "zebra secret: I need milk and to call mom", "summary": "zebra day"})
        today = datetime.now(timezone.utc).date()
        self.milk = await self.db.todo.create(data={"entryId": self.entry.id, "userId": uid, "text": "Buy milk", "urgency": 3,
                                                     "dueDate": datetime(today.year, today.month, today.day, tzinfo=timezone.utc)})
        self.call = await self.db.todo.create(data={"entryId": self.entry.id, "userId": uid, "text": "Call mom"})
        await self.client.post("/api/tasks", json={"title": "Web task"})

    def results(self) -> list:
        """The tool results the model got back in the last request."""
        last = fm.requests("python")[-1]["body"]["messages"][-1]["content"]
        return [json.loads(c["content"]) if c["content"].startswith(("{", "[")) else c["content"] for c in last]

    async def test_listed_with_tasks_and_on_the_agenda_without_journal_text(self):
        today = datetime.now(timezone.utc).date().isoformat()
        fm.script(fm.tools(("list_tasks", {}), ("list_tasks", {"query": "MILK"}), ("get_agenda", {"from": today, "to": today}),
                           ("search", {"query": "zebra"}), ("search", {"query": "milk"}), ("get_options", {})), fm.message(fm.text("ok")))
        await self.say("what do I have?")
        listed, queried, agenda, zebra, milk, options = self.results()
        self.assertEqual([(t["title"], t.get("source")) for t in listed["items"]], [("Web task", None), ("Buy milk", "app"), ("Call mom", "app")])
        self.assertEqual(listed["total"], 3)
        self.assertEqual(listed["items"][1]["priority"], "URGENT")
        self.assertIn("vournal app", listed["appTodos"])
        self.assertEqual([t["title"] for t in queried["items"]], ["Buy milk"])
        self.assertIn("Buy milk", [x["title"] for x in agenda])
        self.assertEqual(zebra, [])
        self.assertEqual([(r["type"], r["title"]) for r in milk], [("todo", "Buy milk")])
        sent = json.dumps(fm.requests("python")[-1]["body"], ensure_ascii=False)
        self.assertNotIn("zebra", sent.replace('"query":"zebra"', "").replace('"zebra"', ""))
        self.assertNotIn("secret", sent)

    async def test_ticked_rescheduled_and_never_deleted(self):
        fm.script(fm.tools(("update_task", {"id": self.call.id, "status": "done"}),
                           ("update_task", {"id": self.milk.id, "date": "2026-12-01", "time": "09:30", "priority": "LOW"}),
                           ("update_task", {"id": self.milk.id, "title": "Buy oat milk"}),
                           ("update_task", {"id": self.call.id, "time": "10:00", "date": "2026-02-30"}),
                           ("delete_item", {"type": "task", "id": self.milk.id})), fm.message(fm.text("done")))
        reply = await self.say("tick call mom, move milk")
        ticked, moved, renamed, bad_day, deleted = self.results()
        self.assertEqual((ticked["ok"], ticked["status"]), (True, "COMPLETED"))
        self.assertEqual((moved["due"], moved["priority"]), ("2026-12-01 09:30", "MEDIUM"))  # the app has no "low": urgency 0 reads Medium
        self.assertIn("vournal app", renamed)
        self.assertTrue(bad_day.startswith("Error: "))
        self.assertIn("can't be deleted here", deleted)
        call = await self.db.todo.find_unique(where={"id": self.call.id})
        milk = await self.db.todo.find_unique(where={"id": self.milk.id})
        self.assertEqual((call.done, milk.text, milk.urgency, milk.dueDate.date().isoformat()), (True, "Buy milk", 0, "2026-12-01"))
        actions = {a["kind"]: a for a in reply["messages"][1]["actions"]}
        self.assertEqual((actions["complete"]["entityType"], actions["complete"]["link"], actions["complete"]["canUndo"]),
                         ("app_todo", f"/journal/{self.entry.id}", True))
        self.assertNotIn("delete", actions)
        undone = await self.client.post(f"/api/assistant/actions/{actions['complete']['id']}/undo")
        self.assertEqual(undone.json()["status"], "undone")
        self.assertFalse((await self.db.todo.find_unique(where={"id": self.call.id})).done)

    async def test_add_checks_the_apps_list_first(self):
        fm.script(fm.tools(("create_task", {"title": "buy  MILK"})), fm.tools(("create_task", {"title": "Buy milk"})), fm.message(fm.text("ok")))
        await self.say("add buy milk")
        refused = fm.requests("python")[1]["body"]["messages"][-1]["content"][0]["content"]
        self.assertIn("already exists in the vournal app", refused)
        self.assertIn(self.milk.id, refused)
        titles = [t["title"] for t in (await self.client.get("/api/tasks")).json()["items"]]
        self.assertEqual(titles.count("Buy milk"), 1)  # the second ask, in the same message, adds it

    async def test_times_are_the_web_users_even_when_the_phone_is_elsewhere(self):
        await self.db.user.update(where={"id": self.client.user.id}, data={"timezone": "Europe/London"})
        fm.script(fm.tools(("update_task", {"id": self.milk.id, "date": "2026-12-01", "time": "09:00"}),
                           ("update_task", {"id": self.call.id, "date": "2026-12-02", "time": "23:30"})), fm.message(fm.text("ok")))
        await self.say("move them")
        moved, late = self.results()
        self.assertEqual((moved["due"], late["due"]), ("2026-12-01 09:00", "2026-12-02 23:30"))
        milk = await self.db.todo.find_unique(where={"id": self.milk.id})
        self.assertEqual(milk.startAt, datetime(2026, 12, 1, 7, 0, tzinfo=timezone.utc))  # 09:00 Beirut


if __name__ == "__main__":
    unittest.main()
