"""The chat's two modes (design 2026-11-06): Manual waits for the user's approval of every
change, Auto acts at once with an Undo. The mode is the user's setting only: no tool can read
or change it. The model is fake_model (no paid calls)."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vournal_os.parity import fake_model as fm
from vournal_os.support import OsTestCase
from vournal_os.test_assistant import ChatBase

BACKEND = Path(__file__).resolve().parents[2]


class ModeSetting(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = await self.signed_in()

    async def test_mode_defaults_to_manual(self):
        self.assertEqual((await self.client.get("/api/settings")).json()["assistantMode"], "manual")

    async def test_mode_changes_only_to_known_values(self):
        ok = await self.client.patch("/api/settings", json={"assistantMode": "auto"})
        self.assertEqual(ok.json()["assistantMode"], "auto")
        self.assertEqual((await self.client.patch("/api/settings", json={"assistantMode": "yolo"})).status_code, 400)
        self.assertEqual((await self.client.get("/api/settings")).json()["assistantMode"], "auto")


def _last_tool_result(side: str = "python") -> dict:
    """The tool result the model is being sent right now (call from a scripted reply: the lock is
    held, so read the request list directly)."""
    body = fm._requests[side][-1]["body"]
    return json.loads(body["messages"][-1]["content"][0]["content"])


class Manual(ChatBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.client.patch("/api/settings", json={"assistantMode": "manual"})

    async def tasks(self):
        return (await self.client.get("/api/tasks")).json()["items"]

    async def test_manual_write_waits_and_changes_nothing(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        action = (await self.say("add gym"))["messages"][1]["actions"][0]
        self.assertEqual((action["status"], action["kind"], action["label"]), ("pending", "create", "Gym"))
        self.assertEqual(await self.tasks(), [])
        seen = json.loads(fm.requests("python")[1]["body"]["messages"][-1]["content"][0]["content"])
        self.assertEqual((seen["pending"], seen["id"]), (True, f"pending:{action['id']}"))

    async def test_manual_bad_input_goes_back_to_the_model(self):
        fm.script(fm.tools(("create_task", {})), fm.message(fm.text("What should I call it?")))
        reply = (await self.say("add a task"))["messages"][1]
        self.assertEqual(reply["actions"], [])
        result = fm.requests("python")[1]["body"]["messages"][-1]["content"][0]
        self.assertTrue(result.get("is_error"))
        self.assertIn("title", result["content"])

    async def test_approve_runs_the_stored_call(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        sent = await self.say("add gym")
        left = sent["remainingThisMonth"]
        action = sent["messages"][1]["actions"][0]
        done = await self.client.post(f"/api/assistant/actions/{action['id']}/approve")
        self.assertEqual(done.status_code, 200, done.text)
        view = done.json()
        tasks = await self.tasks()
        self.assertEqual([t["title"] for t in tasks], ["Gym"])
        self.assertEqual((view["status"], view["entityId"], view["canUndo"]), ("done", tasks[0]["id"], True))
        self.assertTrue(view["link"])
        self.assertEqual((await self.client.get("/api/ai/status")).json()["remainingThisMonth"], left)

    async def test_approve_runs_once_under_double_click(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        action = (await self.say("add gym"))["messages"][1]["actions"][0]
        url = f"/api/assistant/actions/{action['id']}/approve"
        first, second = await asyncio.gather(self.client.post(url), self.client.post(url))
        self.assertEqual(sorted([first.status_code, second.status_code]), [200, 400])
        loser = first if first.status_code == 400 else second
        self.assertEqual(loser.json()["message"], "Already handled")
        self.assertEqual(len(await self.tasks()), 1)

    async def test_approve_of_a_vanished_item_fails_cleanly(self):
        task = (await self.client.post("/api/tasks", json={"title": "Old"})).json()
        fm.script(fm.tools(("update_task", {"id": task["id"], "title": "New"})), fm.message(fm.text("Approve below.")))
        action = (await self.say("rename it"))["messages"][1]["actions"][0]
        self.assertEqual((action["kind"], action["label"]), ("update", "Old"))
        await self.client.delete(f"/api/tasks/{task['id']}")
        done = await self.client.post(f"/api/assistant/actions/{action['id']}/approve")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "failed")
        self.assertIn("not found", done.json()["error"].lower())

    async def test_placeholders_follow_approval_order(self):
        fm.script(fm.tools(("create_debt", {"direction": "owed_to_me", "who": "Sami", "amount": 100})),
                  lambda side: fm.tools(("add_debt_payment", {"debt_id": _last_tool_result(side)["id"], "amount": 40})),
                  fm.message(fm.text("Approve below.")))
        reply = (await self.say("Sami owes me 100 and paid 40"))["messages"][1]
        message_id = reply["id"]
        self.assertEqual([a["status"] for a in reply["actions"]], ["pending", "pending"])
        approved = await self.client.post(f"/api/assistant/messages/{message_id}/approve-all")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual([a["status"] for a in approved.json()], ["done", "done"])
        debts = (await self.client.get("/api/debts")).json()["items"]
        self.assertEqual([(d["who"], float(d["remaining"])) for d in debts], [("Sami", 60.0)])

    async def test_a_rejected_parent_fails_its_child(self):
        fm.script(fm.tools(("create_debt", {"direction": "owed_to_me", "who": "Sami", "amount": 100})),
                  lambda side: fm.tools(("add_debt_payment", {"debt_id": _last_tool_result(side)["id"], "amount": 40})),
                  fm.message(fm.text("Approve below.")))
        parent, child = (await self.say("Sami owes me 100 and paid 40"))["messages"][1]["actions"]
        self.assertEqual((await self.client.post(f"/api/assistant/actions/{parent['id']}/cancel")).status_code, 200)
        done = (await self.client.post(f"/api/assistant/actions/{child['id']}/approve")).json()
        self.assertEqual(done["status"], "failed")
        self.assertIn("Depends on", done["error"])
        self.assertEqual((await self.client.get("/api/debts")).json()["items"], [])

    async def test_pending_expires_after_24_hours(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        sent = await self.say("add gym")
        action = sent["messages"][1]["actions"][0]
        await self.db.assistantaction.update(where={"id": action["id"]}, data={"createdAt": datetime.now(timezone.utc) - timedelta(hours=25)})
        shown = (await self.client.get(f"/api/assistant/conversations/{sent['conversation']['id']}")).json()
        self.assertEqual(shown["messages"][1]["actions"][0]["status"], "expired")
        for op in ("approve", "cancel"):
            response = await self.client.post(f"/api/assistant/actions/{action['id']}/{op}")
            self.assertEqual((response.status_code, response.json()["message"]), (400, "This request expired — ask again"))
        self.assertEqual(await self.tasks(), [])

    async def test_mode_switch_keeps_pending_cards(self):
        fm.script(fm.tools(("create_task", {"title": "Gym"})), fm.message(fm.text("Approve below.")))
        action = (await self.say("add gym"))["messages"][1]["actions"][0]
        await self.client.patch("/api/settings", json={"assistantMode": "auto"})
        self.assertEqual((await self.client.post(f"/api/assistant/actions/{action['id']}/approve")).json()["status"], "done")
        self.assertEqual(len(await self.tasks()), 1)

    async def test_delete_item_still_pending_and_confirm_alias_works(self):
        for op in ("confirm", "approve"):
            task = (await self.client.post("/api/tasks", json={"title": f"Old {op}"})).json()
            fm.script(fm.tools(("delete_item", {"type": "task", "id": task["id"], "label": "Old"})), fm.message(fm.text("Confirm below.")))
            action = (await self.say("delete it"))["messages"][1]["actions"][0]
            self.assertEqual((action["kind"], action["status"]), ("delete", "pending"))
            self.assertEqual((await self.client.post(f"/api/assistant/actions/{action['id']}/{op}")).json()["status"], "done")
            self.assertEqual((await self.client.get(f"/api/tasks/{task['id']}")).status_code, 404)

    async def test_the_model_is_told_the_mode(self):
        fm.script(fm.message(fm.text("ok")))
        await self.say("hi")
        first = fm.requests("python")[0]["body"]["messages"][-1]["content"][0]["text"]
        self.assertIn("Mode: manual", first)


class NoToolTouchesTheMode(unittest.TestCase):
    def test_no_tool_mentions_settings_or_the_mode(self):
        text = (BACKEND / "app/os/data/assistant.json").read_text()
        self.assertNotIn("assistantMode", text)
        self.assertNotIn("aiEnabled", text)


if __name__ == "__main__":
    unittest.main()
