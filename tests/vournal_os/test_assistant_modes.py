"""The chat's two modes (design 2026-11-06): Manual waits for the user's approval of every
change, Auto acts at once with an Undo. The mode is the user's setting only: no tool can read
or change it. The model is fake_model (no paid calls)."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class ReviewFixes(ChatBase):
    """Final review (2026-11-06): no false "done", no card stuck running, no Undo that can't
    bring a file back, and Manual cards that show what will change."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.client.patch("/api/settings", json={"assistantMode": "manual"})

    async def pending(self, *uses, message="do it"):
        fm.script(fm.tools(*uses), fm.message(fm.text("Approve below.")))
        return (await self.say(message))["messages"][1]

    async def test_an_approval_that_adds_nothing_is_not_done(self):
        entry = await self.db.entry.create(data={"userId": self.client.user.id, "rawTranscript": "x"})
        await self.db.todo.create(data={"entryId": entry.id, "userId": self.client.user.id, "text": "Gym"})
        card = (await self.pending(("create_task", {"title": "Gym"})))["actions"][0]
        done = (await self.client.post(f"/api/assistant/actions/{card['id']}/approve")).json()
        self.assertEqual(done["status"], "failed")
        self.assertIn("already exists", done["error"])

    async def test_a_crash_while_approving_fails_the_card(self):
        from app.os import assistant_pending

        card = (await self.pending(("create_task", {"title": "Gym"})))["actions"][0]
        with patch.object(assistant_pending, "run_approved", AsyncMock(side_effect=RuntimeError("boom"))):
            done = await self.client.post(f"/api/assistant/actions/{card['id']}/approve")
        self.assertEqual((done.status_code, done.json()["status"]), (200, "failed"))

    async def test_a_card_left_running_shows_failed_and_can_be_cancelled(self):
        sent = await self.pending(("create_task", {"title": "Gym"}))
        card = sent["actions"][0]
        await self.db.assistantaction.update(where={"id": card["id"]}, data={"status": "running",
                                                                           "resolvedAt": datetime.now(timezone.utc) - timedelta(minutes=10)})
        conversation = (await self.db.assistantaction.find_unique(where={"id": card["id"]})).conversationId
        shown = (await self.client.get(f"/api/assistant/conversations/{conversation}")).json()
        self.assertEqual(shown["messages"][1]["actions"][0]["status"], "failed")
        self.assertEqual((await self.client.post(f"/api/assistant/actions/{card['id']}/cancel")).status_code, 200)

    async def test_a_delete_that_takes_files_offers_no_undo(self):
        task = (await self.client.post("/api/tasks", json={"title": "Old"})).json()
        await self.db.attachment.create(data={"userId": self.client.user.id, "entityType": "TASK", "entityId": task["id"], "fileName": "a.pdf",
                                              "mimeType": "application/pdf", "size": 3, "storageKey": "k/a.pdf"})
        card = (await self.pending(("delete_item", {"type": "task", "id": task["id"], "label": "Old"})))["actions"][0]
        with patch("app.os.attachments.delete_objects", AsyncMock()):
            done = (await self.client.post(f"/api/assistant/actions/{card['id']}/approve")).json()
        self.assertEqual((done["status"], done["canUndo"]), ("done", False))

    async def test_manual_cards_show_what_will_change(self):
        card = (await self.pending(("create_task", {"title": "Gym", "date": "2026-11-12", "time": "07:00", "priority": "HIGH"})))["actions"][0]
        self.assertEqual(card["details"], {"title": "Gym", "date": "2026-11-12", "time": "07:00", "priority": "HIGH"})
        task = (await self.client.post("/api/tasks", json={"title": "Run"})).json()
        update = (await self.pending(("update_task", {"id": task["id"], "date": "2026-11-13"})))["actions"][0]
        self.assertEqual(update["details"], {"date": "2026-11-13"})  # ids are left out
        done = (await self.client.post(f"/api/assistant/actions/{card['id']}/approve")).json()
        self.assertIsNone(done["details"])


class AutoDeletes(ChatBase):
    """Auto mode (ChatBase): one plain item goes at once, with an Undo; a delete that takes more
    with it, a second delete in the same message, or anything with a file still asks."""

    async def task(self, title="Old", **extra):
        return (await self.client.post("/api/tasks", json={"title": title, **extra})).json()

    def delete(self, t, title="Old"):
        return ("delete_item", {"type": "task", "id": t["id"], "label": title})

    async def exists(self, t):
        return (await self.client.get(f"/api/tasks/{t['id']}")).status_code == 200

    async def test_auto_deletes_a_single_plain_item_at_once_and_undo_restores_it(self):
        t = await self.task()
        fm.script(fm.tools(self.delete(t)), fm.message(fm.text("Deleted.")))
        action = (await self.say("delete Old"))["messages"][1]["actions"][0]
        self.assertEqual((action["kind"], action["status"], action["canUndo"]), ("delete", "done", True))
        self.assertFalse(await self.exists(t))
        result = fm.requests("python")[1]["body"]["messages"][-1]["content"][0]["content"]
        self.assertEqual(result, 'Deleted "Old". The user can undo it.')
        self.assertEqual((await self.client.post(f"/api/assistant/actions/{action['id']}/undo")).status_code, 200)
        self.assertTrue(await self.exists(t))

    async def test_auto_asks_when_the_delete_takes_children(self):
        t = await self.task()
        await self.task("Sub", parentId=t["id"])
        debt = (await self.client.post("/api/debts", json={"direction": "OWED_TO_ME", "kind": "LOAN", "title": "Sami", "principal": 100})).json()
        await self.client.post(f"/api/debts/{debt['id']}/payments", json={"amount": 10})
        fm.script(fm.tools(self.delete(t)), fm.message(fm.text("Confirm below.")),
                  fm.tools(("delete_item", {"type": "debt", "id": debt["id"], "label": "Sami"})), fm.message(fm.text("Confirm below.")))
        self.assertEqual((await self.say("delete Old"))["messages"][1]["actions"][0]["status"], "pending")
        self.assertEqual((await self.say("delete Sami's debt"))["messages"][1]["actions"][0]["status"], "pending")
        self.assertTrue(await self.exists(t))
        self.assertEqual((await self.client.get(f"/api/debts/{debt['id']}")).status_code, 200)

    async def test_auto_asks_for_both_when_one_round_deletes_two(self):
        a, b = await self.task("A"), await self.task("B")
        fm.script(fm.tools(self.delete(a, "A"), self.delete(b, "B")), fm.message(fm.text("Confirm below.")))
        actions = (await self.say("delete A and B"))["messages"][1]["actions"]
        self.assertEqual([x["status"] for x in actions], ["pending", "pending"])
        self.assertTrue(await self.exists(a) and await self.exists(b))

    async def test_auto_asks_for_a_second_delete_in_a_later_round(self):
        a, b = await self.task("A"), await self.task("B")
        fm.script(fm.tools(self.delete(a, "A")), fm.tools(self.delete(b, "B")), fm.message(fm.text("One done, one to confirm.")))
        actions = (await self.say("delete A, then B"))["messages"][1]["actions"]
        self.assertEqual([x["status"] for x in actions], ["done", "pending"])
        self.assertEqual((await self.exists(a), await self.exists(b)), (False, True))

    async def test_auto_asks_for_anything_with_a_file(self):
        t = await self.task()
        await self.db.attachment.create(data={"userId": self.client.user.id, "entityType": "TASK", "entityId": t["id"], "fileName": "a.pdf",
                                              "mimeType": "application/pdf", "size": 3, "storageKey": "k/a.pdf"})
        fm.script(fm.tools(self.delete(t)), fm.message(fm.text("Confirm below.")))
        self.assertEqual((await self.say("delete Old"))["messages"][1]["actions"][0]["status"], "pending")
        self.assertTrue(await self.exists(t))


class NoToolTouchesTheMode(unittest.TestCase):
    def test_no_tool_mentions_settings_or_the_mode(self):
        text = (BACKEND / "app/os/data/assistant.json").read_text()
        self.assertNotIn("assistantMode", text)
        self.assertNotIn("aiEnabled", text)


if __name__ == "__main__":
    unittest.main()
