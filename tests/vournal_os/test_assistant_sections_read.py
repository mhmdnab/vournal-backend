"""get_section (design 2026-11-18, the assistant in every section): one read tool for the meal
plan, recipes, budgets, recurring payments, documents, maintenance, trips, study, work and rituals.
Compact JSON with ids; a document's number and notes never; the rituals' reflective writing only
with the journal permission; a section turned off in Settings refuses."""

import json
from datetime import datetime, timedelta, timezone

from vournal_os.test_assistant import ChatBase

ALL_SECTIONS = ["calendar", "tasks", "projects", "finance", "goals", "habits", "maintenance", "documents", "shopping", "meals",
                "health", "notes", "study", "work", "travel", "people", "reminders", "reports"]


class SectionsBase(ChatBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        r = await self.client.patch("/api/settings", json={"enabledSections": ALL_SECTIONS})
        self.assertEqual(r.status_code, 200, r.text)

    def ctx(self, journal=False):
        from app.os import assistant_tools

        c = assistant_tools.Ctx(self.client.user, "Asia/Beirut", assistant_tools.local_day(datetime.now(timezone.utc), "Asia/Beirut"), "auto")
        c.journal = journal
        return c

    async def read_text(self, journal=False, **args) -> str:
        from app.os import assistant_tools

        return (await assistant_tools.run("get_section", args, self.ctx(journal)))["content"]

    async def read(self, journal=False, **args) -> dict:
        text = await self.read_text(journal, **args)
        self.assertFalse(text.startswith("Error"), text)
        return json.loads(text)

    async def post(self, path, body, method="post"):
        r = await getattr(self.client, method)(path, json=body)
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()

    def today(self) -> str:
        from app.os import assistant_tools

        return assistant_tools.local_day(datetime.now(timezone.utc), "Asia/Beirut")

    def day(self, offset: int) -> str:
        return (datetime.fromisoformat(self.today()) + timedelta(days=offset)).date().isoformat()


class GetSection(SectionsBase):
    async def test_meal_plan_defaults_to_this_week_and_lists_entries(self):
        await self.post("/api/meal-plan", {"date": self.today(), "mealType": "DINNER", "title": "Pasta", "servings": 2})
        await self.post("/api/meal-plan", {"date": self.day(40), "title": "Far away"})
        out = await self.read(section="meal_plan")
        self.assertEqual([e["title"] for e in out["items"]], ["Pasta"])
        self.assertEqual(set(out["items"][0]), {"id", "date", "meal", "title", "recipeId", "servings"})
        self.assertEqual((out["items"][0]["date"], out["items"][0]["meal"]), (self.today(), "DINNER"))

    async def test_meal_plan_range_capped_at_31_days(self):
        out = await self.read(section="meal_plan", **{"from": "2026-01-01", "to": "2026-12-31"})
        self.assertEqual((out["from"], out["to"]), ("2026-01-01", "2026-01-31"))

    async def test_recipes_query_and_one_recipe_with_ingredients(self):
        r = await self.post("/api/recipes", {"name": "Lentil soup", "servings": 4, "instructions": "Boil.",
                                             "ingredients": [{"name": "lentils", "quantity": 200, "unit": "g"}]})
        await self.post("/api/recipes", {"name": "Tabbouleh"})
        out = await self.read(section="recipes", query="lentil")
        self.assertEqual([x["name"] for x in out["items"]], ["Lentil soup"])
        self.assertEqual(set(out["items"][0]), {"id", "name", "servings", "isFavorite"})
        one = await self.read(section="recipes", id=r["id"])
        self.assertEqual((one["name"], one["instructions"]), ("Lentil soup", "Boil."))
        self.assertEqual(one["ingredients"], [{"name": "lentils", "quantity": 200, "unit": "g"}])

    async def test_budgets_show_spent_and_left(self):
        await self.post("/api/budgets", {"amount": 300, "name": "Groceries"})
        await self.post("/api/transactions", {"type": "EXPENSE", "amount": 50, "title": "Market"})
        out = await self.read(section="budgets")
        b = out["items"][0]
        self.assertEqual(set(b), {"id", "name", "amount", "currency", "period", "category", "spent", "remaining", "percentUsed"})
        self.assertEqual((b["name"], b["amount"], b["spent"], b["remaining"]), ("Groceries", 300, 50, 250))

    async def test_recurring_payments_fields(self):
        await self.post("/api/recurring-payments", {"name": "Netflix", "amount": 12, "kind": "SUBSCRIPTION", "rrule": "FREQ=MONTHLY"})
        p = (await self.read(section="recurring_payments"))["items"][0]
        self.assertEqual(set(p), {"id", "name", "amount", "currency", "kind", "cycle", "nextDate", "status"})
        self.assertEqual((p["name"], p["amount"], p["kind"], p["status"]), ("Netflix", 12, "SUBSCRIPTION", "ACTIVE"))

    async def test_documents_never_include_number_or_notes(self):
        await self.post("/api/documents", {"name": "Passport", "type": "PASSPORT", "documentNumber": "X123456", "notes": "safe box",
                                           "expiryDate": self.day(200)})
        text = await self.read_text(section="documents")
        for leak in ("X123456", "safe box", "documentNumber", "notes", "hasNotes", "hasDocumentNumber"):
            self.assertNotIn(leak, text)
        d = json.loads(text)["items"][0]
        self.assertEqual(set(d), {"id", "name", "type", "issuer", "holderName", "issueDate", "expiryDate", "daysRemaining", "status"})
        self.assertEqual((d["name"], d["expiryDate"]), ("Passport", self.day(200)))

    async def test_maintenance_fields(self):
        await self.post("/api/maintenance", {"title": "Oil change", "area": "CAR", "rrule": "FREQ=MONTHLY;INTERVAL=6", "estimatedCost": 60})
        m = (await self.read(section="maintenance"))["items"][0]
        self.assertEqual(set(m), {"id", "title", "area", "cycle", "nextDueAt", "lastDoneAt", "estimatedCost", "currency"})
        self.assertEqual((m["title"], m["area"], m["estimatedCost"]), ("Oil change", "CAR", 60))

    async def test_trips_and_one_trip_with_itinerary_and_packing(self):
        t = await self.post("/api/trips", {"name": "Paris", "destination": "Paris", "startDate": self.day(20), "endDate": self.day(25)})
        await self.post(f"/api/trips/{t['id']}/items", {"title": "Flight", "type": "TRANSPORT"})
        await self.post(f"/api/trips/{t['id']}/packing", {"name": "Passport"})
        out = await self.read(section="trips")
        self.assertEqual(set(out["items"][0]), {"id", "name", "destination", "startDate", "endDate", "status", "budget", "currency"})
        one = await self.read(section="trips", id=t["id"])
        self.assertEqual([i["title"] for i in one["itinerary"]], ["Flight"])
        self.assertEqual([p["name"] for p in one["packing"]], ["Passport"])
        self.assertEqual(set(one["packing"][0]), {"id", "name", "quantity", "group", "isPacked"})

    async def test_study_courses_assessments_sessions(self):
        c = await self.post("/api/study/courses", {"name": "Statistics", "code": "STA101"})
        due = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        await self.post("/api/study/assessments", {"courseId": c["id"], "title": "Midterm", "type": "EXAM", "dueAt": due, "weight": 40})
        await self.post("/api/study/sessions", {"durationMinutes": 45, "courseId": c["id"], "topic": "Chapter 2"})
        out = await self.read(section="study")
        self.assertEqual([x["name"] for x in out["courses"]], ["Statistics"])
        self.assertEqual([(a["title"], a["weight"]) for a in out["upcoming"]], [("Midterm", 40)])
        self.assertEqual([(s["minutes"], s["topic"]) for s in out["sessions"]], [(45, "Chapter 2")])

    async def test_work_reads_the_overview(self):
        await self.post("/api/tasks", {"title": "Quarterly report", "lifeArea": "WORK", "dueAt": self.today()})
        await self.post("/api/tasks", {"title": "Buy socks"})
        out = await self.read(section="work")
        titles = [t["title"] for t in out["tasks"]]
        self.assertIn("Quarterly report", titles)
        self.assertNotIn("Buy socks", titles)
        self.assertEqual(set(out), {"tasks", "meetings", "projects", "deadlines"})

    async def seed_rituals(self):
        await self.post("/api/rituals/day", {"plan": True, "intention": "Be calm", "win": "Ran", "mood": 4, "note": "n"}, method="put")
        await self.post("/api/rituals/week", {"date": self.today(), "priorities": ["A"], "wentWell": "w", "improve": "i"}, method="put")

    async def test_rituals_without_journal_permission_hide_reflections(self):
        await self.seed_rituals()
        out = await self.read(section="rituals")
        for field in ("intention", "win", "mood", "note"):
            self.assertNotIn(field, out["day"])
        for field in ("wentWell", "improve"):
            self.assertNotIn(field, out["week"])
        self.assertEqual((out["day"]["planned"], out["week"]["priorities"]), (True, ["A"]))

    async def test_rituals_with_journal_permission_show_reflections(self):
        await self.seed_rituals()
        out = await self.read(journal=True, section="rituals")
        self.assertEqual((out["day"]["intention"], out["day"]["win"], out["day"]["mood"]), ("Be calm", "Ran", 4))
        self.assertEqual((out["week"]["wentWell"], out["week"]["improve"]), ("w", "i"))

    async def test_section_turned_off_refuses(self):
        await self.client.patch("/api/settings", json={"enabledSections": ["tasks"]})
        self.assertEqual(await self.read_text(section="trips"), "Error: The Travel section is turned off in Settings.")
        # rituals have no switch (ruling R2)
        self.assertIn("day", await self.read(section="rituals"))

    async def test_lists_are_capped_at_30(self):
        for n in range(35):
            await self.db.recipe.create(data={"userId": self.client.user.id, "name": f"Recipe {n:02d}"})
        out = await self.read(section="recipes")
        self.assertEqual((len(out["items"]), out["more"]), (30, True))

    async def test_unknown_section_is_an_error(self):
        self.assertTrue((await self.read_text(section="garden")).startswith("Error: section must be one of"))


class Registry(SectionsBase):
    async def test_register_adds_tool_kind_and_snapshot_model(self):
        from app.os import assistant_tools

        async def handler(i, ctx):
            return {"content": "ok"}

        kind = assistant_tools.Kind(model="Recipe", find=None, label=lambda r: "", link=lambda r: "", delete=None, restore=None)
        assistant_tools.register({"x_tool": handler}, {"x_kind": kind})
        self.addCleanup(lambda: (assistant_tools._TOOLS.pop("x_tool"), assistant_tools.KINDS.pop("x_kind")))
        self.assertIs(assistant_tools._TOOLS["x_tool"], handler)
        self.assertIs(assistant_tools.KINDS["x_kind"], kind)
        self.assertEqual(assistant_tools.model_of("x_kind"), "Recipe")
        self.assertEqual(assistant_tools.model_of("task"), "Task")
