"""The assistant's rituals tool and the work fields (design 2026-11-18): save_ritual plans and
reviews the day or the week (its reflective text comes back only with the journal permission,
ruling R5); tasks and projects take a life area, events can be meetings."""

import json

from vournal_os.assistant_life_base import LifeBase


class RitualTools(LifeBase):
    async def day_row(self, day):
        from app.os.insight_time import key_to_date

        return await self.db.daylog.find_unique(where={"userId_date": {"userId": self.uid, "date": key_to_date(day)}})

    async def test_save_day_plan_and_undo_deletes_new_row(self):
        a = await self.act("save_ritual", {"kind": "day", "date": "2026-11-19", "plan": True, "intention": "Ship the tools"})
        self.assertEqual((a["kind"], a["entityType"], a["link"]), ("update", "ritual", "/dashboard"))
        row = await self.day_row("2026-11-19")
        self.assertEqual((row.intention, row.plannedAt is not None), ("Ship the tools", True))
        await self.undo(a)
        self.assertIsNone(await self.day_row("2026-11-19"))

    async def test_save_day_win_hidden_in_result_without_journal(self):
        out = await self.result("save_ritual", {"kind": "day", "date": "2026-11-19", "review": True, "win": "Ran 5k", "mood": 4})
        self.assertNotIn("Ran 5k", json.dumps(out))
        self.assertNotIn("mood", out)
        self.assertEqual(out["reviewed"], True)
        row = await self.day_row("2026-11-19")
        self.assertEqual((row.win, row.mood), ("Ran 5k", 4))

    async def test_save_day_win_shown_with_journal(self):
        from app.os import assistant_tools

        ctx = self.ctx()
        ctx.journal = True
        out = json.loads((await assistant_tools.run("save_ritual", {"kind": "day", "date": "2026-11-19", "win": "Ran 5k"}, ctx))["content"])
        self.assertEqual(out["win"], "Ran 5k")

    async def test_save_week_priorities_and_undo_restores_old(self):
        await self.post("/api/rituals/week", {"date": "2026-11-19", "priorities": ["Old one"]}, method="put")
        a = await self.act("save_ritual", {"kind": "week", "date": "2026-11-19", "priorities": ["Finish the report", "Gym 3 times", "Call mom"]})
        review = await self.db.weekreview.find_first(where={"userId": self.uid})
        self.assertEqual(review.priorities, ["Finish the report", "Gym 3 times", "Call mom"])
        await self.undo(a)
        review = await self.db.weekreview.find_first(where={"userId": self.uid})
        self.assertEqual(review.priorities, ["Old one"])

    async def test_save_ritual_undo_refused_after_change(self):
        a = await self.act("save_ritual", {"kind": "day", "date": "2026-11-19", "intention": "Calm"})
        await self.post("/api/rituals/day", {"date": "2026-11-19", "intention": "Changed on web"}, method="put")
        self.assertIn("changed since", await self.undo_refused(a))
        self.assertEqual((await self.day_row("2026-11-19")).intention, "Changed on web")

    async def test_field_of_the_other_kind_is_an_error(self):
        self.assertIn('"priorities"', await self.act_error("save_ritual", {"kind": "day", "priorities": ["A"]}))

    async def test_more_than_three_priorities_is_the_routes_error(self):
        self.assertIn("no more than 3", await self.act_error("save_ritual", {"kind": "week", "priorities": ["a", "b", "c", "d"]}))


class WorkFields(LifeBase):
    async def test_create_task_for_work_sets_life_area(self):
        a = await self.act("create_task", {"title": "Quarterly report", "life_area": "WORK"})
        self.assertEqual((await self.db.task.find_unique(where={"id": a["entityId"]})).lifeArea, "WORK")

    async def test_update_task_life_area_and_undo(self):
        t = await self.post("/api/tasks", {"title": "Report"})
        a = await self.act("update_task", {"id": t["id"], "life_area": "WORK"})
        self.assertEqual((await self.db.task.find_unique(where={"id": t["id"]})).lifeArea, "WORK")
        await self.undo(a)
        self.assertEqual((await self.db.task.find_unique(where={"id": t["id"]})).lifeArea, t["lifeArea"])

    async def test_create_project_life_area(self):
        a = await self.act("create_project", {"name": "Launch", "life_area": "WORK"})
        self.assertEqual((await self.db.project.find_unique(where={"id": a["entityId"]})).lifeArea, "WORK")

    async def test_update_project_life_area_and_undo(self):
        p = await self.db.project.create(data={"userId": self.uid, "name": "Website"})
        a = await self.act("update_project", {"id": p.id, "life_area": "WORK"})
        self.assertEqual((await self.db.project.find_unique(where={"id": p.id})).lifeArea, "WORK")
        await self.undo(a)
        self.assertEqual((await self.db.project.find_unique(where={"id": p.id})).lifeArea, p.lifeArea)

    async def test_create_event_meeting_type(self):
        a = await self.act("create_event", {"title": "Meeting with Rami", "date": "2026-11-20", "time": "10:00", "meeting": True})
        self.assertEqual((await self.db.event.find_unique(where={"id": a["entityId"]})).type, "MEETING")

    async def test_work_overview_shows_the_new_work_task(self):
        from app.os import assistant_tools

        await self.act("create_task", {"title": "Quarterly report", "life_area": "WORK"})
        out = json.loads((await assistant_tools.run("get_section", {"section": "work"}, self.ctx()))["content"])
        self.assertIn("Quarterly report", [t["title"] for t in out["tasks"]])

    async def test_life_area_must_be_one_of_the_eight(self):
        a = await self.act("create_task", {"title": "Thing", "life_area": "GARDEN"})
        self.assertNotEqual((await self.db.task.find_unique(where={"id": a["entityId"]})).lifeArea, "GARDEN")
