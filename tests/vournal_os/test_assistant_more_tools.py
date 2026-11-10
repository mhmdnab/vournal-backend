"""The assistant's tools for habits, projects, people, gifts, important dates and goals (design
2026-11-06, shared by the dashboard chat and the app's review card). Each one changes the row
the route would, says what it did, and can be undone. The model is fake_model (no paid calls)."""

from datetime import date

from vournal_os.parity import fake_model as fm
from vournal_os.test_assistant import ChatBase


class MoreTools(ChatBase):
    async def act(self, tool: str, data: dict) -> dict:
        fm.script(fm.tools((tool, data)), fm.message(fm.text("Done.")))
        actions = (await self.say("do it"))["messages"][1]["actions"]
        self.assertEqual(len(actions), 1, actions)
        self.assertNotEqual(actions[0]["status"], "failed", actions[0])
        return actions[0]

    async def undo(self, action: dict) -> None:
        r = await self.client.post(f"/api/assistant/actions/{action['id']}/undo")
        self.assertEqual(r.status_code, 200, r.text)

    async def person(self, name="Ali"):
        return await self.db.person.create(data={"userId": self.client.user.id, "name": name, "normalizedName": name.lower()})

    async def habit(self, name="Gym"):
        return await self.db.habit.create(data={"userId": self.client.user.id, "name": name})

    async def test_create_habit(self):
        a = await self.act("create_habit", {"name": "Read", "target_per_day": 2, "unit": "pages"})
        self.assertEqual((a["kind"], a["entityType"], a["label"]), ("create", "habit", "Read"))
        h = await self.db.habit.find_unique(where={"id": a["entityId"]})
        self.assertEqual((h.name, h.targetPerDay, h.unit, h.frequency), ("Read", 2, "pages", "DAILY"))
        await self.undo(a)
        self.assertIsNone(await self.db.habit.find_unique(where={"id": a["entityId"]}))

    async def test_update_habit(self):
        h = await self.habit()
        a = await self.act("update_habit", {"id": h.id, "name": "Gym session", "archived": True})
        self.assertEqual((a["kind"], a["entityType"]), ("update", "habit"))
        row = await self.db.habit.find_unique(where={"id": h.id})
        self.assertEqual((row.name, row.isArchived), ("Gym session", True))
        await self.undo(a)
        row = await self.db.habit.find_unique(where={"id": h.id})
        self.assertEqual((row.name, row.isArchived), ("Gym", False))

    async def test_log_habit(self):
        h = await self.habit()
        a = await self.act("log_habit", {"id": h.id})
        self.assertEqual((a["kind"], a["entityType"]), ("create", "habit_log"))
        logs = await self.db.habitlog.find_many(where={"habitId": h.id})
        self.assertEqual([l.count for l in logs], [1])
        again = await self.act("log_habit", {"id": h.id})
        self.assertEqual([l.count for l in await self.db.habitlog.find_many(where={"habitId": h.id})], [2])
        await self.undo(again)
        self.assertEqual([l.count for l in await self.db.habitlog.find_many(where={"habitId": h.id})], [1])
        await self.undo(a)
        self.assertEqual(await self.db.habitlog.count(where={"habitId": h.id}), 0)

    async def test_create_project(self):
        a = await self.act("create_project", {"name": "Vournal 2", "deadline": "2027-01-31"})
        self.assertEqual((a["kind"], a["entityType"], a["label"]), ("create", "project", "Vournal 2"))
        p = await self.db.project.find_unique(where={"id": a["entityId"]})
        self.assertEqual((p.name, p.status, p.deadline.date()), ("Vournal 2", "ACTIVE", date(2027, 1, 31)))
        await self.undo(a)
        self.assertIsNone(await self.db.project.find_unique(where={"id": a["entityId"]}))

    async def test_update_project(self):
        p = await self.db.project.create(data={"userId": self.client.user.id, "name": "Website"})
        a = await self.act("update_project", {"id": p.id, "status": "completed"})
        row = await self.db.project.find_unique(where={"id": p.id})
        self.assertEqual(row.status, "COMPLETED")
        self.assertIsNotNone(row.completedAt)
        await self.undo(a)
        row = await self.db.project.find_unique(where={"id": p.id})
        self.assertEqual((row.status, row.completedAt), ("ACTIVE", None))

    async def test_update_person(self):
        ali = await self.person()
        a = await self.act("update_person", {"id": ali.id, "relationship": "Friend", "notes": "Likes books"})
        row = await self.db.person.find_unique(where={"id": ali.id})
        self.assertEqual((row.relationship, row.notes), ("Friend", "Likes books"))
        await self.undo(a)
        row = await self.db.person.find_unique(where={"id": ali.id})
        self.assertEqual((row.relationship, row.notes), (None, None))

    async def test_add_gift(self):
        ali = await self.person()
        a = await self.act("add_gift", {"person_id": ali.id, "title": "A book", "price": 25, "given": True, "occasion": "Visit"})
        self.assertEqual((a["kind"], a["entityType"]), ("create", "gift"))
        g = await self.db.giftidea.find_unique(where={"id": a["entityId"]})
        self.assertEqual((g.personId, g.title, float(g.price), g.isPurchased, g.occasion), (ali.id, "A book", 25.0, True, "Visit"))
        await self.undo(a)
        self.assertIsNone(await self.db.giftidea.find_unique(where={"id": a["entityId"]}))

    async def test_add_important_date(self):
        ali = await self.person()
        a = await self.act("add_important_date", {"person_id": ali.id, "title": "Ali's birthday", "date": "1990-05-04"})
        d = await self.db.importantdate.find_unique(where={"id": a["entityId"]})
        self.assertEqual((d.personId, d.title, d.repeatsYearly), (ali.id, "Ali's birthday", True))
        await self.undo(a)
        self.assertIsNone(await self.db.importantdate.find_unique(where={"id": a["entityId"]}))

    async def test_create_goal(self):
        a = await self.act("create_goal", {"name": "Run a 10k", "deadline": "2027-06-01"})
        g = await self.db.goal.find_unique(where={"id": a["entityId"]})
        self.assertEqual((g.name, g.status), ("Run a 10k", "ACTIVE"))
        await self.undo(a)
        self.assertIsNone(await self.db.goal.find_unique(where={"id": a["entityId"]}))

    async def test_update_goal(self):
        g = await self.db.goal.create(data={"userId": self.client.user.id, "name": "Save", "startDate": date.today().isoformat() + "T00:00:00Z"})
        a = await self.act("update_goal", {"id": g.id, "status": "paused", "current_value": 40})
        row = await self.db.goal.find_unique(where={"id": g.id})
        self.assertEqual((row.status, float(row.currentValue)), ("PAUSED", 40.0))
        await self.undo(a)
        row = await self.db.goal.find_unique(where={"id": g.id})
        self.assertEqual((row.status, float(row.currentValue)), ("ACTIVE", 0.0))

    async def test_delete_item_accepts_new_kinds(self):
        h = await self.habit()
        await self.db.habitlog.create(data={"habitId": h.id, "userId": self.client.user.id, "date": date.today().isoformat() + "T00:00:00Z"})
        fm.script(fm.tools(("delete_item", {"type": "habit", "id": h.id, "label": "Gym"})), fm.message(fm.text("Confirm below.")))
        a = (await self.say("delete gym"))["messages"][1]["actions"][0]
        self.assertEqual(a["status"], "pending")  # it takes its log with it, so it asks
        done = (await self.client.post(f"/api/assistant/actions/{a['id']}/approve")).json()
        self.assertEqual(done["status"], "done")
        self.assertIsNone(await self.db.habit.find_unique(where={"id": h.id}))
        await self.undo(done)
        self.assertIsNotNone(await self.db.habit.find_unique(where={"id": h.id}))
        self.assertEqual(await self.db.habitlog.count(where={"habitId": h.id}), 1)

    async def test_delete_a_gift(self):
        ali = await self.person()
        g = await self.db.giftidea.create(data={"personId": ali.id, "title": "Scarf"})
        fm.script(fm.tools(("delete_item", {"type": "gift", "id": g.id, "label": "Scarf"})), fm.message(fm.text("Done.")))
        a = (await self.say("delete the scarf"))["messages"][1]["actions"][0]
        self.assertEqual((a["status"], a["entityType"]), ("done", "gift"))
        self.assertIsNone(await self.db.giftidea.find_unique(where={"id": g.id}))

    async def test_manual_defers_new_tools(self):
        await self.client.patch("/api/settings", json={"assistantMode": "manual"})
        fm.script(fm.tools(("create_habit", {"name": "Walk"})), fm.message(fm.text("Approve below.")))
        a = (await self.say("add walk"))["messages"][1]["actions"][0]
        self.assertEqual((a["status"], a["kind"], a["entityType"]), ("pending", "create", "habit"))
        self.assertEqual(await self.db.habit.count(where={"userId": self.client.user.id}), 0)


class NoteTools(MoreTools):
    async def test_add_note_then_delete_and_undo(self):
        a = await self.act("add_note", {"title": "Ideas", "content": "a list"})
        note_id = a["entityId"]
        self.assertIsNotNone(await self.db.note.find_unique(where={"id": note_id}))
        fm.script(fm.tools(("delete_item", {"type": "note", "id": note_id, "label": "Ideas"})), fm.message(fm.text("Confirm below.")))
        card = (await self.say("delete the ideas note"))["messages"][1]["actions"][0]
        if card["status"] == "pending":
            card = (await self.client.post(f"/api/assistant/actions/{card['id']}/approve")).json()
        self.assertEqual(card["status"], "done", card)
        self.assertIsNone(await self.db.note.find_unique(where={"id": note_id}))
        await self.undo(card)
        self.assertIsNotNone(await self.db.note.find_unique(where={"id": note_id}))
