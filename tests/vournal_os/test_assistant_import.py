"""Import from other AI chats: a .md file, fed to the assistant part by part, then a report
(spec 2026-11-10-ai-chat-import-design). The model is fake_model (no paid calls).

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import asyncio
import unittest
from unittest.mock import patch

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase

PLAN = """# Projects
## Website relaunch
- Tasks:
  - [ ] Send the homepage copy to Rami

# Money
## Debts
- I owe Rami 200 USD
"""


class Split(unittest.TestCase):
    def test_split_at_top_level_headings(self):
        from app.os.assistant_import import split_parts

        parts = split_parts(PLAN, limit=80)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0].startswith("# Projects"))
        self.assertTrue(parts[1].startswith("# Money"))

    def test_small_sections_share_a_part(self):
        from app.os.assistant_import import split_parts

        self.assertEqual(split_parts(PLAN), [PLAN.strip()])

    def test_split_without_headings(self):
        from app.os.assistant_import import split_parts

        text = "\n".join(f"- task number {i}" for i in range(2000))
        parts = split_parts(text, limit=1000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("\n".join(parts), text)  # nothing lost, cut on line boundaries


class Import(ChatBase):
    async def upload(self, text=PLAN, name="plan.md", conversation=None):
        return await self.client.post(
            "/api/assistant/import", files={"file": (name, text.encode(), "text/markdown")}
        )

    async def wait(self, conversation_id: str) -> dict:
        for _ in range(200):
            status = (await self.client.get(f"/api/assistant/import/{conversation_id}")).json()
            if status["status"] != "running":
                return status
            await asyncio.sleep(0.05)
        self.fail("the import never finished")

    async def test_import_runs_each_part_then_reports(self):
        from app.os import assistant_import

        with patch.object(assistant_import, "PART_LIMIT", 80):
            fm.script(
                fm.tools(("create_project", {"name": "Website relaunch"})), fm.message(fm.text("Added the project.")),
                fm.message(fm.text("Noted the debt.")),
                fm.message(fm.text("Added: Website relaunch. Couldn't add: nothing.")),
            )
            started = await self.upload()
        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()["parts"], 2)
        conversation = started.json()["conversationId"]
        status = await self.wait(conversation)
        self.assertEqual((status["status"], status["done"], status["total"], status["failed"]), ("done", 2, 2, 0))
        messages = (await self.client.get(f"/api/assistant/conversations/{conversation}")).json()["messages"]
        texts = [m["text"] for m in messages]
        self.assertIn("plan.md — part 1 of 2", texts)
        self.assertIn("plan.md — part 2 of 2", texts)
        self.assertEqual(texts[-1], "Added: Website relaunch. Couldn't add: nothing.")
        self.assertEqual(await self.db.project.count(where={"userId": self.client.user.id, "name": "Website relaunch"}), 1)
        # the model got the whole part, under the import instruction
        first_user = fm.requests("python")[0]["body"]["messages"][-1]["content"][0]["text"]
        self.assertIn("Send the homepage copy to Rami", first_user)
        self.assertIn("never delete", first_user.lower())

    async def test_bad_files_are_refused(self):
        too_big = await self.upload("x" * 200_001)
        self.assertEqual(too_big.status_code, 400, too_big.text)
        wrong = await self.upload("hello", name="plan.pdf")
        self.assertEqual(wrong.status_code, 400, wrong.text)
        empty = await self.upload("   ")
        self.assertEqual(empty.status_code, 400, empty.text)

    async def test_import_refused_when_allowance_too_low(self):
        from app.os import ai

        fm.script()  # an empty script also clears the requests log
        with patch.object(ai, "MONTHLY_LIMIT", 1):
            response = await self.upload()
        self.assertEqual((response.status_code, response.json()["state"]), (403, "limit"), response.text)
        self.assertEqual(fm.requests("python"), [])

    async def test_one_import_at_a_time(self):
        from app.os import assistant_import

        assistant_import._running.add(self.client.user.id)
        try:
            response = await self.upload()
        finally:
            assistant_import._running.discard(self.client.user.id)
        self.assertEqual(response.status_code, 409, response.text)

    async def test_a_failed_part_does_not_stop_the_import(self):
        from app.os import assistant_import

        with patch.object(assistant_import, "PART_LIMIT", 80):
            # the client retries a 500 twice by itself: a part that fails three times has failed
            broken = fm.error(500, "api_error")
            fm.script(broken, broken, broken, fm.message(fm.text("Noted the debt.")), fm.message(fm.text("Report.")))
            started = await self.upload()
        status = await self.wait(started.json()["conversationId"])
        self.assertEqual((status["status"], status["done"], status["failed"]), ("done", 2, 1))

    async def test_import_never_deletes(self):
        task = await self.db.task.create(data={"userId": self.client.user.id, "title": "Keep me"})
        fm.script(fm.tools(("delete_item", {"type": "task", "id": task.id})), fm.message(fm.text("Done.")), fm.message(fm.text("Report.")))
        started = await self.upload()
        await self.wait(started.json()["conversationId"])
        self.assertIsNotNone(await self.db.task.find_unique(where={"id": task.id}))
        self.assertEqual(await self.db.assistantaction.count(where={"userId": self.client.user.id, "kind": "delete"}), 0)

    async def test_approve_all_covers_the_whole_import(self):
        from app.os import assistant_import

        await self.client.patch("/api/settings", json={"assistantMode": "manual"})
        with patch.object(assistant_import, "PART_LIMIT", 80):
            fm.script(
                fm.tools(("create_project", {"name": "Website relaunch"})), fm.message(fm.text("Waiting for you.")),
                fm.tools(("create_task", {"title": "Pay Rami back"})), fm.message(fm.text("Waiting too.")),
                fm.message(fm.text("Report.")),
            )
            started = await self.upload()
        conversation = started.json()["conversationId"]
        await self.wait(conversation)
        pending = await self.db.assistantaction.count(where={"conversationId": conversation, "status": "pending"})
        self.assertEqual(pending, 2)
        done = await self.client.post(f"/api/assistant/import/{conversation}/approve-all")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual([a["status"] for a in done.json()], ["done", "done"])
        self.assertEqual(await self.db.project.count(where={"userId": self.client.user.id, "name": "Website relaunch"}), 1)
