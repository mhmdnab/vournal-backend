"""Undo for the assistant's edits (design 2026-11-06): the changed fields' old values go back,
unless the item was changed or deleted since. Water can be undone too. Auto mode (ChatBase)."""

import unittest

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase


class UndoEdits(ChatBase):
    async def undo(self, action):
        return await self.client.post(f"/api/assistant/actions/{action['id']}/undo")

    async def edit(self, tool, tool_input, message="change it"):
        fm.script(fm.tools((tool, tool_input)), fm.message(fm.text("Done.")))
        action = (await self.say(message))["messages"][1]["actions"][0]
        self.assertEqual((action["kind"], action["canUndo"]), ("update", True), action)
        return action

    async def test_undo_a_task_edit(self):
        task = (await self.client.post("/api/tasks", json={"title": "Gym", "dueAt": "2026-11-10"})).json()
        before = (await self.client.get(f"/api/tasks/{task['id']}")).json()
        action = await self.edit("update_task", {"id": task["id"], "title": "Run", "date": "2026-11-12", "priority": "HIGH"})
        self.assertEqual((await self.client.get(f"/api/tasks/{task['id']}")).json()["title"], "Run")
        self.assertEqual((await self.undo(action)).status_code, 200)
        after = (await self.client.get(f"/api/tasks/{task['id']}")).json()
        self.assertEqual({k: after[k] for k in ("title", "dueAt", "hasDueTime", "priority")},
                         {k: before[k] for k in ("title", "dueAt", "hasDueTime", "priority")})

    async def test_undo_an_event_edit(self):
        event = (await self.client.post("/api/events", json={"title": "Dentist", "startAt": "2026-11-10T08:00:00.000Z",
                                                              "endAt": "2026-11-10T09:00:00.000Z", "allDay": False, "location": "Clinic"})).json()
        before = (await self.client.get(f"/api/events/{event['id']}")).json()
        action = await self.edit("update_event", {"id": event["id"], "time": "15:00", "location": "Hospital"})
        self.assertEqual((await self.undo(action)).status_code, 200)
        after = (await self.client.get(f"/api/events/{event['id']}")).json()
        self.assertEqual({k: after[k] for k in ("startAt", "endAt", "allDay", "location")},
                         {k: before[k] for k in ("startAt", "endAt", "allDay", "location")})

    async def test_undo_an_all_day_event_made_timed(self):
        event = (await self.client.post("/api/events", json={"title": "Trip", "date": "2026-11-10", "allDay": True})).json()
        before = (await self.client.get(f"/api/events/{event['id']}")).json()
        action = await self.edit("update_event", {"id": event["id"], "time": "10:00"})
        self.assertEqual((await self.undo(action)).status_code, 200)
        after = (await self.client.get(f"/api/events/{event['id']}")).json()
        self.assertEqual({k: after[k] for k in ("startAt", "allDay")}, {k: before[k] for k in ("startAt", "allDay")})

    async def test_undo_a_transaction_edit(self):
        t = (await self.client.post("/api/transactions", json={"type": "EXPENSE", "amount": 10, "title": "Lunch"})).json()
        action = await self.edit("update_transaction", {"id": t["id"], "amount": 12.5, "title": "Dinner"})
        self.assertEqual((await self.undo(action)).status_code, 200)
        after = (await self.client.get(f"/api/transactions/{t['id']}")).json()
        self.assertEqual((float(after["amount"]), after["title"]), (10.0, "Lunch"))

    async def test_undo_a_health_plan_edit(self):
        await self.client.put("/api/wellness/profile", json={"sex": "MALE", "birthDate": "1995-05-01", "heightCm": 178, "weightKg": 80,
                                                             "activityLevel": "MODERATE", "goal": "LOSE", "targetWeightKg": 75})
        action = await self.edit("update_health_profile", {"target_weight_kg": 72})
        self.assertEqual((await self.undo(action)).status_code, 200)
        self.assertEqual((await self.client.get("/api/wellness/profile")).json()["profile"]["targetWeightKg"], 75)

    async def test_undo_refuses_after_a_later_change(self):
        task = (await self.client.post("/api/tasks", json={"title": "Gym"})).json()
        action = await self.edit("update_task", {"id": task["id"], "title": "Run"})
        await self.client.patch(f"/api/tasks/{task['id']}", json={"title": "Swim"})
        refused = await self.undo(action)
        self.assertEqual((refused.status_code, refused.json()["message"]), (400, "It was changed since — not undone"))
        self.assertEqual((await self.client.get(f"/api/tasks/{task['id']}")).json()["title"], "Swim")

    async def test_undo_refuses_when_deleted_since(self):
        task = (await self.client.post("/api/tasks", json={"title": "Gym"})).json()
        action = await self.edit("update_task", {"id": task["id"], "title": "Run"})
        await self.client.delete(f"/api/tasks/{task['id']}")
        refused = await self.undo(action)
        self.assertEqual((refused.status_code, refused.json()["message"]), (400, "It was deleted since — nothing to undo"))

    async def test_water_can_be_undone(self):
        fm.script(fm.tools(("log_water", {"ml": 250})), fm.message(fm.text("Logged.")))
        action = (await self.say("I drank a glass"))["messages"][1]["actions"][0]
        self.assertTrue(action["canUndo"])
        self.assertEqual((await self.undo(action)).status_code, 200)
        day = (await self.client.get("/api/wellness/day")).json()
        self.assertEqual(day["water"]["ml"], 0)


if __name__ == "__main__":
    unittest.main()
