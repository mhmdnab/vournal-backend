"""The rules every new section tool follows (design 2026-11-18): in Manual mode each write waits as
a card, and approving it does the write; an import may add but not change; the recording step
offers only "paid the bill" and "did the maintenance" (ruling R6); the prompt keeps its lines."""

from vournal_os.assistant_life_base import LifeBase
from vournal_os.parity import fake_model as fm


class SectionRules(LifeBase):
    async def seeded(self) -> dict:
        """One real item of each kind, and a minimal valid input for every new write tool."""
        c = await self.post("/api/study/courses", {"name": "Statistics"})
        x = await self.post("/api/study/assessments", {"courseId": c["id"], "title": "Quiz"})
        trip = await self.post("/api/trips", {"name": "Paris", "startDate": "2026-12-12"})
        item = await self.post(f"/api/trips/{trip['id']}/items", {"title": "Flight"})
        meal = await self.post("/api/meal-plan", {"date": "2026-11-20", "title": "Pasta"})
        recipe = await self.post("/api/recipes", {"name": "Soup"})
        budget = await self.post("/api/budgets", {"amount": 100})
        pay = await self.post("/api/recurring-payments", {"name": "Internet", "amount": 30})
        doc = await self.post("/api/documents", {"name": "Passport"})
        job = await self.post("/api/maintenance", {"title": "Oil", "rrule": "FREQ=YEARLY"})
        return {
            "plan_meal": {"date": "2026-11-21", "title": "Lasagne"},
            "update_meal_plan": {"id": meal["id"], "title": "Soup"},
            "shopping_from_meal_plan": {"from": "2026-11-16", "to": "2026-11-22"},
            "add_recipe": {"name": "Hummus"},
            "update_recipe": {"id": recipe["id"], "favorite": True},
            "create_budget": {"amount": 50, "period": "YEARLY"},
            "update_budget": {"id": budget["id"], "amount": 120},
            "create_recurring_payment": {"name": "Netflix", "amount": 12},
            "update_recurring_payment": {"id": pay["id"], "action": "pause"},
            "add_document": {"name": "ID card"},
            "update_document": {"id": doc["id"], "expiry_date": "2030-01-01"},
            "add_maintenance_item": {"title": "Filters", "repeat": {"every": "month"}},
            "update_maintenance_item": {"id": job["id"], "done": True},
            "create_trip": {"name": "Rome"},
            "update_trip": {"id": trip["id"], "status": "BOOKED"},
            "add_trip_item": {"trip_id": trip["id"], "list": "packing", "title": "Socks"},
            "update_trip_item": {"trip_id": trip["id"], "list": "itinerary", "id": item["id"], "done": True},
            "add_study_item": {"kind": "course", "title": "Physics"},
            "update_study_item": {"kind": "assessment", "id": x["id"], "grade": 15},
            "save_ritual": {"kind": "day", "plan": True},
        }

    async def counts(self) -> dict:
        tables = ("mealplanentry", "recipe", "budget", "recurringpayment", "document", "maintenanceitem", "maintenancelog", "trip", "tripitem",
                  "packingitem", "course", "assessment", "shoppingitem", "daylog")
        return {t: await getattr(self.db, t).count() for t in tables}

    async def test_every_new_write_tool_becomes_a_card_in_manual(self):
        from app.os import assistant_tools

        inputs = await self.seeded()
        self.assertEqual(set(inputs), assistant_tools.NEW_ADD_TOOLS | assistant_tools.NEW_CHANGE_TOOLS)
        await self.post("/api/settings", {"assistantMode": "manual"}, method="patch")
        before = await self.counts()
        fm.script(fm.tools(*inputs.items()), fm.message(fm.text("Approve below.")))
        actions = (await self.say("do all of it"))["messages"][1]["actions"]
        self.assertEqual([a["status"] for a in actions], ["pending"] * len(inputs), actions)
        self.assertEqual(await self.counts(), before)

    async def test_approving_a_card_does_the_write(self):
        await self.post("/api/settings", {"assistantMode": "manual"}, method="patch")
        fm.script(fm.tools(("plan_meal", {"date": "2026-11-21", "title": "Lasagne"})), fm.message(fm.text("Approve below.")))
        a = (await self.say("plan it"))["messages"][1]["actions"][0]
        done = await self.approve(a)
        self.assertEqual((done["status"], done["entityType"]), ("done", "meal_plan_entry"))
        self.assertIsNotNone(await self.db.mealplanentry.find_unique(where={"id": done["entityId"]}))

    async def test_manual_card_refused_when_section_turned_off(self):
        await self.post("/api/settings", {"assistantMode": "manual"}, method="patch")
        fm.script(fm.tools(("plan_meal", {"date": "2026-11-21", "title": "Lasagne"})), fm.message(fm.text("Approve below.")))
        a = (await self.say("plan it"))["messages"][1]["actions"][0]
        await self.post("/api/settings", {"enabledSections": ["tasks"]}, method="patch")
        r = await self.client.post(f"/api/assistant/actions/{a['id']}/approve")
        self.assertIn("The Meals section is turned off in Settings", r.text)
        self.assertEqual(await self.db.mealplanentry.count(where={"userId": self.uid}), 0)

    async def test_import_allows_new_add_tools_and_refuses_change_tools(self):
        from app.os import assistant, assistant_tools

        ctx = self.ctx()
        ctx.importing = True
        out = await assistant_tools.run("add_recipe", {"name": "From ChatGPT"}, ctx)
        self.assertFalse(out.get("isError"), out)
        b = await self.post("/api/budgets", {"amount": 100})
        out = await assistant_tools.run("update_budget", {"id": b["id"], "amount": 5}, ctx)
        self.assertIn("an import only adds new things", out["content"])
        offered = {t["name"] for t in assistant.tools_for(False, importing=True)}
        self.assertTrue(assistant_tools.NEW_ADD_TOOLS - {"shopping_from_meal_plan", "save_ritual"} <= offered)
        self.assertIn("get_section", offered)
        self.assertFalse(assistant_tools.NEW_CHANGE_TOOLS & offered)

    async def test_recording_offers_only_pay_and_done(self):
        from app import entry_actions
        from app.os import assistant_tools

        names = {t["name"] for t in entry_actions.TOOLS_FOR_ENTRY}
        self.assertTrue({"update_recurring_payment", "update_maintenance_item"} <= names)
        self.assertFalse((assistant_tools.NEW_ADD_TOOLS | assistant_tools.NEW_CHANGE_TOOLS | {"get_section"})
                         - {"update_recurring_payment", "update_maintenance_item"} & names)

    async def test_recording_refuses_other_actions(self):
        from app.os import assistant_tools

        p = await self.post("/api/recurring-payments", {"name": "Rent", "amount": 500})
        ctx = self.ctx("manual")
        ctx.recording = True
        out = await assistant_tools.run("update_recurring_payment", {"id": p["id"], "action": "pause"}, ctx)
        self.assertEqual(out["content"], "Error: from a recording, only paying a recurring payment or marking maintenance done is possible.")
        out = await assistant_tools.run("update_recurring_payment", {"id": p["id"], "action": "pay"}, ctx)
        self.assertEqual((out.get("isError"), out["action"]["status"], out["action"]["kind"]), (None, "pending", "complete"))

    async def test_maintenance_done_card_is_a_completion(self):
        from app.os import assistant_tools

        job = await self.post("/api/maintenance", {"title": "Oil", "rrule": "FREQ=YEARLY"})
        out = await assistant_tools.run("update_maintenance_item", {"id": job["id"], "done": True, "cost": 40}, self.ctx("manual"))
        self.assertEqual((out["action"]["status"], out["action"]["kind"]), ("pending", "complete"))

    async def test_prompt_keeps_the_new_lines(self):
        from app.os import assistant

        for line in ("plan_meal", "log_food is only for food already eaten", "never call add_transaction for it as well",
                     "never ask for or repeat a document number", "find it with get_section or search"):
            self.assertIn(line, assistant.SYSTEM)

    async def test_every_tool_has_a_schema_and_a_handler(self):
        from app.os import assistant, assistant_tools

        self.assertEqual({t["name"] for t in assistant.TOOLS}, set(assistant_tools._TOOLS))
