"""The chatbot answers like NestJS did, with the model played by fake_model on both sides:
every tool on a seeded Beirut account (and its errors), the tool results the model gets
back, the stored history, the buttons (confirm, cancel, undo, expired), the round cap,
refusals and provider errors. The requests each backend sent the model are compared too,
ids masked, so the prompt, tools, thinking blocks and history replay are the same."""

import json
import unittest
from unittest.mock import patch

from vournal_os.parity import fake_model as fm
from vournal_os.parity.harness import ParityCase, copy_reference_market_rates, normalize
from vournal_os.parity.seed import SeedMixin, at, day

SIDE = {"nest": 0, "python": 1}
ALLOWANCE = ("remainingToday", "remainingThisMonth", "mentionCount")  # NestJS: 100 a day; here: monthly, renamed


def done(text="Done."):
    return fm.message(fm.text(text))


def per_side(pairs):
    """A tools() round whose inputs carry each side's own ids: pairs of (name, input(side_index))."""
    return lambda side: fm.tools(*[(name, make(SIDE[side])) for name, make in pairs])


def comparable(requests: list[dict]) -> list:
    """The requests with tool results parsed (their JSON is compared as data, not text)."""
    out = []
    for r in requests:
        body = json.loads(json.dumps(r["body"]))
        for m in body.get("messages", []):
            for block in m["content"] if isinstance(m["content"], list) else []:
                if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                    try:
                        block["content"] = json.loads(block["content"])
                    except ValueError:
                        pass
        out.append({"beta": r["beta"], "body": body})
    return normalize(out)


class AssistantParity(SeedMixin, ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.os import ai

        fm.start()
        patcher = patch.object(ai, "_client", fm.python_client())
        patcher.start()
        self.addCleanup(patcher.stop)
        await copy_reference_market_rates()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut", "aiEnabled": True}))
        self.conversation = None

    async def chat(self, message, new=False):
        conv = None if new else self.conversation
        out = []
        for side, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            body = {"message": message, **({"conversationId": conv[side]} if conv else {})}
            out.append(await client.post(f"{prefix}/assistant/chat", json=body))
        self.assertParity(*out, ignore_keys=ALLOWANCE)
        nest, py = (r.json() for r in out)
        if nest.get("conversation"):
            self.conversation = (nest["conversation"]["id"], py["conversation"]["id"])
        return nest, py

    def assert_same_requests(self, history_as_a_set=True):
        """With history_as_a_set, the replayed messages are compared in any order: NestJS read
        history by createdAt alone, and a round's two rows often share a millisecond, so it
        sometimes replayed the tool results before the calls. Python keeps the order written,
        which is checked on its own."""
        nest, py = fm.requests("nest"), fm.requests("python")
        self.assertEqual(len(nest), len(py))
        a, b = comparable(nest), comparable(py)
        if history_as_a_set:
            for request in (*a, *b):
                request["body"]["messages"].sort(key=lambda m: json.dumps(m, sort_keys=True))
            for request in py:
                self.assert_rounds_in_order(request["body"]["messages"])
        self.assertEqual(a, b)
        return nest

    def assert_rounds_in_order(self, messages):
        for n, m in enumerate(messages):
            results = [c["tool_use_id"] for c in m["content"] if isinstance(m["content"], list) and c.get("type") == "tool_result"]
            if results:
                before = messages[n - 1]
                self.assertEqual(before["role"], "assistant")
                self.assertEqual(results, [c["id"] for c in before["content"] if c.get("type") == "tool_use"])

    @unittest.skip("Dashboard AI part 1 changed this on purpose: the mode line, Manual by default, effort per message (design 2026-11-06)")
    async def test_every_read_tool(self):
        ids = await self.seed_life()
        seeded = await self.on_each("POST", "/tasks", {"title": "Pay the rent", "dueAt": at(day(1), 9)})
        fm.script(
            per_side([
                ("get_options", lambda s: {}),
                ("search", lambda s: {"query": "rent", "types": ["task", "bill", "nonsense"]}),
                ("get_agenda", lambda s: {"from": day(-2), "to": day(7)}),
                ("list_tasks", lambda s: {}),
                ("list_tasks", lambda s: {"status": "all", "query": "rent", "limit": 3.7}),
                ("list_tasks", lambda s: {"overdue": True, "due_from": day(-30), "due_to": day(0)}),
                ("get_money", lambda s: {}),
                ("list_transactions", lambda s: {"type": "expense", "limit": 5}),
                ("list_transactions", lambda s: {"query": "salary", "from": day(-40), "to": day(0), "category_id": ids["food"][s]}),
                ("list_debts", lambda s: {"status": "all"}),
                ("get_health", lambda s: {}),
                ("get_prayer_times", lambda s: {"date": day(1)}),
            ]),
            done("Here is your overview."),
        )
        await self.chat("What's going on? Show me everything")
        self.assertTrue(seeded)
        self.assert_same_requests()

    @unittest.skip("Dashboard AI part 1 changed this on purpose: the mode line, Manual by default, effort per message (design 2026-11-06)")
    async def test_every_write_tool_and_its_buttons(self):
        ids = await self.seed_life()
        task = await self.on_each("POST", "/tasks", {"title": "Call the bank", "dueAt": at(day(1), 9)})
        event = await self.on_each("POST", "/events", {"title": "Lunch", "startAt": at(day(2), 13), "endAt": at(day(2), 14)})
        debt = await self.on_each("POST", "/debts", {"direction": "I_OWE", "title": "Loan", "counterparty": "Rami", "principal": 300})
        fm.script(
            per_side([
                ("create_task", lambda s: {"title": "Water the plants", "repeat": {"every": "week", "weekdays": ["TH", "TH", "XX"], "interval": 2, "until": day(60)},
                                           "priority": "HIGH", "notes": " on the balcony "}),
                ("create_task", lambda s: {"title": "Pray", "prayer": {"prayer": "isha", "offset_minutes": 500}}),
                ("create_task", lambda s: {"title": "Dentist call", "date": day(3), "time": "10:30"}),
                ("update_task", lambda s: {"id": task[s], "date": day(4), "priority": "URGENT", "title": " Call the bank again "}),
                ("update_task", lambda s: {"id": task[s], "status": "done"}),
                ("create_event", lambda s: {"title": "Team sync", "date": day(5), "time": "15:00", "duration_minutes": 45, "location": "Office", "notes": "agenda"}),
                ("create_event", lambda s: {"title": "Holiday", "date": day(9)}),
                ("create_event", lambda s: {"title": "Gym", "date": day(1), "time": "18:00", "repeat": {"every": "week", "weekdays": ["MO", "WE"]}}),
                ("update_event", lambda s: {"id": event[s], "time": "12:30", "duration_minutes": 90, "location": ""}),
                ("create_reminder", lambda s: {"title": "Take pills", "time": "21:00", "repeat": {"every": "day", "until": day(30)}}),
                ("create_reminder", lambda s: {"title": "Renew passport", "date": day(10), "notes": "bring photos"}),
                ("add_transaction", lambda s: {"type": "expense", "amount": 12.345, "title": "Lunch", "category_id": ids["food"][s], "currency": "usd"}),
                ("add_transaction", lambda s: {"type": "income", "amount": 50, "date": day(-1)}),
                ("create_debt", lambda s: {"who": "Sara", "amount": 75, "direction": "owed_to_me", "due_date": day(20), "title": "Concert tickets"}),
                ("add_debt_payment", lambda s: {"debt_id": debt[s], "amount": 100}),
                ("add_note", lambda s: {"title": "Gift ideas", "content": " a book "}),
                ("add_shopping_item", lambda s: {"name": "Milk", "quantity": 2}),
                ("log_food", lambda s: {"name": "Manoushe", "calories": 280.6, "protein": 7.25, "carbs": 35, "fat": -1, "meal": "BREAKFAST"}),
                ("log_water", lambda s: {"ml": 500}),
                ("log_weight", lambda s: {"kg": 72.45}),
                ("update_health_profile", lambda s: {"goal": "LOSE", "height_cm": 175, "calorie_target": None}),
                ("delete_item", lambda s: {"type": "debt", "id": debt[s]}),
                ("delete_item", lambda s: {"type": "task", "id": task[s]}),
            ]),
            done("All set — confirm the deletions below."),
        )
        nest, _py = await self.chat("Do all of this for me")
        self.assert_same_requests()
        actions = [(a["kind"], a["entityType"], a["status"]) for a in nest["messages"][1]["actions"]]
        self.assertEqual(actions[-2:], [("delete", "debt", "pending"), ("delete", "task", "pending")])

        reply_actions = (nest["messages"][1]["actions"], _py["messages"][1]["actions"])
        pick = lambda kind, entity: tuple(next(a["id"] for a in acts if (a["kind"], a["entityType"]) == (kind, entity)) for acts in reply_actions)  # noqa: E731
        confirm, cancel = pick("delete", "debt"), pick("delete", "task")
        undo_note, undo_payment, undo_complete = pick("create", "note"), pick("create", "debt_payment"), pick("complete", "task")
        for action, verb in ((confirm, "confirm"), (cancel, "cancel"), (undo_note, "undo"), (undo_payment, "undo"), (undo_complete, "undo"),
                             (confirm, "confirm"), (confirm, "undo"), (cancel, "cancel"), (pick("create", "water"), "undo")):
            with self.subTest(verb=verb):
                await self.on_each("POST", f"/assistant/actions/{{}}/{verb}", ids=[action])
        await self.check_conversation()
        # the next message replays the history, button notes included
        fm.script(done("Noted."))
        await self.chat("thanks")
        self.assert_same_requests()

    @unittest.skip("Dashboard AI part 1 changed this on purpose: the mode line, Manual by default, effort per message (design 2026-11-06)")
    async def test_tool_errors(self):
        await self.seed_life()
        other_nest, other_py = await self.other_user("Stranger")
        theirs = []
        for client, prefix in ((other_nest, ""), (other_py, "/api")):
            theirs.append((await client.post(f"{prefix}/tasks", json={"title": "Not yours"})).json()["id"])
        fm.script(
            per_side([
                ("no_such_tool", lambda s: {}),
                ("create_task", lambda s: {"title": "  "}),
                ("create_task", lambda s: {"title": "x" * 600}),
                ("update_task", lambda s: {"id": theirs[s], "status": "done"}),
                ("update_task", lambda s: {"id": "made-up"}),
                ("delete_item", lambda s: {"type": "weight", "id": "x"}),
                ("delete_item", lambda s: {"type": "food_log", "id": "nope"}),
                ("get_agenda", lambda s: {"from": day(5), "to": day(1)}),
                ("get_agenda", lambda s: {"from": day(0), "to": day(90)}),
                ("get_agenda", lambda s: {"from": "2026-02-30", "to": "2026-02-32"}),
                ("add_transaction", lambda s: {"amount": -1}),
                ("add_transaction", lambda s: {"amount": 5, "category_id": "made-up"}),
                ("create_event", lambda s: {"title": "No day"}),
                ("create_task", lambda s: {"title": "Bad repeat", "repeat": {"every": "fortnight"}}),
                ("create_reminder", lambda s: {"title": "Bad prayer", "prayer": {"prayer": "lunch"}}),
                ("log_weight", lambda s: {"kg": 900}),
                ("update_health_profile", lambda s: {}),
                ("update_health_profile", lambda s: {"height_cm": 5, "target_weight_kg": 1, "weekly_rate_kg": 9}),
                ("log_food", lambda s: {"name": "Feast", "calories": 99999, "protein": 5000}),
                ("search", lambda s: {}),
                ("add_debt_payment", lambda s: {"debt_id": "made-up", "amount": 5}),
            ]),
            done("Some of that didn't work."),
        )
        await self.chat("break things")
        self.assert_same_requests()

    @unittest.skip("Dashboard AI part 1 changed this on purpose: the mode line, Manual by default, effort per message (design 2026-11-06)")
    async def test_rounds_refusals_failures_and_conversations(self):
        fm.script(*[fm.tools(("list_tasks", {}))] * 7, done("Stopped after many rounds."))
        await self.chat("loop forever", new=True)
        sent = self.assert_same_requests()
        self.assertEqual([("tool_choice" in r["body"]) for r in sent], [False] * 7 + [True])
        fm.script(fm.message(fm.text("partial"), stop="refusal"))
        await self.chat("something bad")
        fm.script(fm.error(400))
        nest, _ = await self.chat("provider down")
        self.assertEqual(nest["status"], "unavailable")
        fm.script(fm.message({"type": "thinking", "thinking": "hm", "signature": "s"}, stop="end_turn"))
        await self.chat("say nothing")
        self.assert_same_requests()
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        fm.script(fm.error(400))
        await self.chat("مرحبا", new=True)
        await self.check_conversations()

    async def check_conversation(self):
        """GET one conversation; a reply's actions sorted, since NestJS saved them in one
        transaction and ordered by createdAt alone (ties in one millisecond come back any order)."""
        out = [await c.get(f"{p}/assistant/conversations/{i}") for (c, p), i in zip(((self.nest, ""), (self.py, "/api")), self.conversation)]
        bodies = [r.json() for r in out]
        for body in bodies:
            for m in body.get("messages", []):
                m["actions"].sort(key=lambda a: (a["kind"], a["entityType"], a["label"], a["status"]))
        self.assertEqual([r.status_code for r in out][0], out[1].status_code)
        self.assertEqual(normalize(bodies[0]), normalize(bodies[1]))

    async def check_conversations(self):
        self.assertParity(*await self.both("GET", "/assistant/conversations"))
        await self.check_conversation()
        await self.on_each("DELETE", "/assistant/conversations/{}", ids=[self.conversation])
        await self.on_each("GET", "/assistant/conversations/{}", ids=[self.conversation])
        for path in ("/assistant/conversations/nope", "/assistant/actions/nope/confirm"):
            self.assertParity(*await self.both("GET" if "conversations" in path else "POST", path))

    async def test_statuses_and_validation(self):
        fm.script()
        await self.chat("hello", new=True)
        self.assertParity(*await self.both("PATCH", "/settings", {"aiEnabled": False}))
        await self.chat("hello", new=True)
        for body in ({}, {"message": "   "}, {"message": "x" * 4001}, {"message": 5}, {"message": "hi", "conversationId": 7}, {"message": "hi", "extra": 1}):
            with self.subTest(body=body):
                nest, py = await self.both("POST", "/assistant/chat", body)
                self.assertParity(nest, py)
                self.assertEqual(nest.json()["message"], py.json()["message"])
        self.assertParity(*await self.both("PATCH", "/settings", {"aiEnabled": True}))
        fm.script()
        nest, py = await self.both("POST", "/assistant/chat", {"message": "hi", "conversationId": "not-mine"})
        self.assertParity(nest, py)
        self.assert_same_requests()


if __name__ == "__main__":
    unittest.main()
