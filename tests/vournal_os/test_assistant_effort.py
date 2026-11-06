"""Effort is chosen per message (owner decision, 6 November 2026): low for one clear change or
lookup, medium when the message needs reasoning or several steps, and medium for the rest of a
message once a tool call fails or a round asks for 3 or more changes. No extra AI call."""

import unittest

from app.os.assistant_effort import escalate, pick_effort
from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase

TABLE = [
    ("add milk to my list", "low"),
    ("log a 20 dollar lunch", "low"),
    ("what's on tomorrow?", "low"),
    ("mark the dentist task done", "low"),
    ("move all of thursday's tasks to friday and remind me the night before", "medium"),
    ("plan my week", "medium"),
    ("make me a meal plan for next week", "medium"),
    ("compare my spending with last month", "medium"),
    ("delete every task I finished", "medium"),
    ("add eggs, then create a reminder to cook at 7", "medium"),
    ("ضيف حليب على اللائحة", "low"),
    ("شو عندي بكرا؟", "low"),
    ("انقل كل مهام الخميس للجمعة وذكرني قبلها بليلة", "medium"),
    ("اعملي خطة أكل للأسبوع", "medium"),
    ("قارن مصاريفي مع الشهر الماضي", "medium"),
]


class Rule(unittest.TestCase):
    def test_the_table(self):
        got = [(text, pick_effort(text, last_reply_failed=False)) for text, _ in TABLE]
        self.assertEqual(got, TABLE)

    def test_a_failed_last_reply_means_medium(self):
        self.assertEqual(pick_effort("add milk", last_reply_failed=True), "medium")

    def test_escalate(self):
        self.assertEqual(escalate("low", 3, False), "medium")
        self.assertEqual(escalate("low", 1, True), "medium")
        self.assertEqual(escalate("low", 2, False), "low")
        self.assertEqual(escalate("medium", 0, False), "medium")


class Requests(ChatBase):
    def efforts(self):
        return [r["body"]["output_config"]["effort"] for r in fm.requests("python")]

    async def test_simple_message_asks_low(self):
        fm.script(fm.message(fm.text("Added.")))
        await self.say("add milk")
        self.assertEqual(self.efforts(), ["low"])

    async def test_planning_asks_medium(self):
        fm.script(fm.message(fm.text("Here's a plan.")))
        await self.say("plan my week")
        self.assertEqual(self.efforts(), ["medium"])

    async def test_a_failed_tool_call_moves_the_rest_to_medium(self):
        fm.script(fm.tools(("update_task", {"id": "nope", "status": "done"})), fm.message(fm.text("Couldn't find it.")))
        await self.say("tick my gym task")
        self.assertEqual(self.efforts(), ["low", "medium"])


if __name__ == "__main__":
    unittest.main()
