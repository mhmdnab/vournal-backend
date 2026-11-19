"""The assistant's document and home maintenance tools (design 2026-11-18). A document's number and
notes never reach a tool or the model (owner decision 2026-11-18). Maintenance done with a cost
adds the expense to Finance; its Undo removes the log entry and the expense and puts the dates back."""

import json

from vournal_os.assistant_life_base import LifeBase


class HomeTools(LifeBase):
    async def item(self, **data):
        return await self.post("/api/maintenance", {"title": "Oil change", "area": "CAR", "rrule": "FREQ=MONTHLY;INTERVAL=6",
                                                    "lastDoneAt": "2026-05-01", "nextDueAt": "2026-11-01", **data})

    async def test_add_document_and_undo(self):
        a = await self.act("add_document", {"name": "Passport", "type": "PASSPORT", "expiry_date": "2030-03-01", "issuer": "Lebanon"})
        self.assertEqual((a["kind"], a["entityType"], a["label"], a["link"]), ("create", "document", "Passport", f"/documents?id={a['entityId']}"))
        d = await self.db.document.find_unique(where={"id": a["entityId"]})
        self.assertEqual((d.type, d.issuer, d.expiryDate.date().isoformat()), ("PASSPORT", "Lebanon", "2030-03-01"))
        await self.undo(a)
        self.assertIsNone(await self.db.document.find_unique(where={"id": a["entityId"]}))

    async def test_document_number_in_input_is_dropped(self):
        out = await self.result("add_document", {"name": "Passport", "document_number": "X123", "documentNumber": "X123", "notes": "box"})
        row = await self.db.document.find_unique(where={"id": out["id"]})
        self.assertEqual((row.documentNumberEnc, row.notesEnc), (None, None))

    async def test_document_results_never_carry_number_or_notes(self):
        d = await self.post("/api/documents", {"name": "ID card", "documentNumber": "SECRET99", "notes": "drawer"})
        from app.os import assistant_tools

        out = await assistant_tools.run("update_document", {"id": d["id"], "expiry_date": "2031-01-01"}, self.ctx())
        self.assertFalse(out.get("isError"), out)
        for leak in ("SECRET99", "drawer", "documentNumber", "notes", "hasNotes"):
            self.assertNotIn(leak, out["content"])
        self.assertNotIn("SECRET99", json.dumps(out["action"]))

    async def test_update_document_and_undo(self):
        d = await self.post("/api/documents", {"name": "Licence", "expiryDate": "2027-01-01"})
        a = await self.act("update_document", {"id": d["id"], "expiry_date": "2032-01-01", "holder_name": "Mhmd"})
        row = await self.db.document.find_unique(where={"id": d["id"]})
        self.assertEqual((row.expiryDate.date().isoformat(), row.holderName), ("2032-01-01", "Mhmd"))
        await self.undo(a)
        row = await self.db.document.find_unique(where={"id": d["id"]})
        self.assertEqual((row.expiryDate.date().isoformat(), row.holderName), ("2027-01-01", None))

    async def test_expiry_before_issue_is_the_routes_error(self):
        text = await self.act_error("add_document", {"name": "X", "issue_date": "2030-01-01", "expiry_date": "2029-01-01"})
        self.assertIn("expiryDate must be on or after issueDate", text)

    async def test_no_tool_schema_has_document_number_or_notes(self):
        from app.os import assistant

        for t in assistant.TOOLS:
            if "document" in t["name"]:
                self.assertFalse({"document_number", "documentNumber", "notes", "note"} & set(t["input_schema"]["properties"]), t["name"])

    async def test_search_never_returns_document_number(self):
        await self.post("/api/documents", {"name": "Passport", "documentNumber": "N0PE77", "notes": "n0tes"})
        from app.os import assistant_tools

        out = await assistant_tools.run("search", {"query": "Passport"}, self.ctx())
        self.assertIn("Passport", out["content"])
        self.assertNotIn("N0PE77", out["content"])
        self.assertNotIn("n0tes", out["content"])

    async def test_add_maintenance_item_and_undo(self):
        a = await self.act("add_maintenance_item", {"title": "Clean AC filters", "area": "HOME", "repeat": {"every": "month", "interval": 3},
                                                     "estimated_cost": 20})
        self.assertEqual((a["kind"], a["entityType"], a["link"]), ("create", "maintenance_item", f"/maintenance?id={a['entityId']}"))
        m = await self.db.maintenanceitem.find_unique(where={"id": a["entityId"]})
        self.assertEqual((m.rrule, m.area, float(m.estimatedCost)), ("FREQ=MONTHLY;INTERVAL=3", "HOME", 20.0))
        await self.undo(a)
        self.assertIsNone(await self.db.maintenanceitem.find_unique(where={"id": a["entityId"]}))

    async def test_add_maintenance_item_needs_repeat(self):
        self.assertIn("repeat", await self.act_error("add_maintenance_item", {"title": "X"}))

    async def test_done_with_cost_adds_one_expense_and_undo_removes_both(self):
        m = await self.item()
        a = await self.act("update_maintenance_item", {"id": m["id"], "done": True, "cost": 80})
        self.assertEqual((a["kind"], a["entityType"]), ("complete", "maintenance_item"))
        self.assertEqual(await self.db.transaction.count(where={"userId": self.uid, "type": "EXPENSE"}), 1)
        tx = await self.db.transaction.find_first(where={"userId": self.uid})
        self.assertEqual((float(tx.amount), tx.title), (80.0, "Oil change"))
        row = await self.db.maintenanceitem.find_unique(where={"id": m["id"]})
        self.assertNotEqual(row.nextDueAt.date().isoformat(), "2026-11-01")
        await self.undo(a)
        self.assertEqual(await self.db.transaction.count(where={"userId": self.uid, "type": "EXPENSE"}), 0)
        self.assertEqual(await self.db.maintenancelog.count(where={"itemId": m["id"]}), 0)
        row = await self.db.maintenanceitem.find_unique(where={"id": m["id"]})
        self.assertEqual((row.lastDoneAt.date().isoformat(), row.nextDueAt.date().isoformat()), ("2026-05-01", "2026-11-01"))

    async def test_done_without_cost_adds_no_expense(self):
        m = await self.item()
        await self.act("update_maintenance_item", {"id": m["id"], "done": True})
        self.assertEqual(await self.db.transaction.count(where={"userId": self.uid}), 0)
        self.assertEqual(await self.db.maintenancelog.count(where={"itemId": m["id"]}), 1)

    async def test_done_undo_after_a_later_done_keeps_the_later_dates(self):
        m = await self.item()
        first = await self.act("update_maintenance_item", {"id": m["id"], "done": True, "date": "2026-11-10", "cost": 50})
        await self.act("update_maintenance_item", {"id": m["id"], "done": True, "date": "2026-11-15"})
        later = await self.db.maintenanceitem.find_unique(where={"id": m["id"]})
        await self.undo(first)
        row = await self.db.maintenanceitem.find_unique(where={"id": m["id"]})
        self.assertEqual((row.lastDoneAt, row.nextDueAt), (later.lastDoneAt, later.nextDueAt))
        self.assertEqual(await self.db.maintenancelog.count(where={"itemId": m["id"]}), 1)
        self.assertEqual(await self.db.transaction.count(where={"userId": self.uid}), 0)

    async def test_done_with_fields_is_an_error(self):
        m = await self.item()
        self.assertIn("not both", await self.act_error("update_maintenance_item", {"id": m["id"], "done": True, "title": "X"}))

    async def test_update_maintenance_fields_and_undo(self):
        m = await self.item(estimatedCost=60)
        a = await self.act("update_maintenance_item", {"id": m["id"], "estimated_cost": 75, "next_due": "2026-12-01"})
        row = await self.db.maintenanceitem.find_unique(where={"id": m["id"]})
        self.assertEqual((float(row.estimatedCost), row.nextDueAt.date().isoformat()), (75.0, "2026-12-01"))
        await self.undo(a)
        row = await self.db.maintenanceitem.find_unique(where={"id": m["id"]})
        self.assertEqual((float(row.estimatedCost), row.nextDueAt.date().isoformat()), (60.0, "2026-11-01"))

    async def test_delete_document_and_maintenance_item(self):
        d = await self.post("/api/documents", {"name": "Old card"})
        a = await self.delete("document", d["id"], "Old card")
        self.assertEqual((a["status"], a["entityType"]), ("done", "document"))
        await self.undo(a)
        self.assertIsNotNone(await self.db.document.find_unique(where={"id": d["id"]}))
        m = await self.item()
        a = await self.delete("maintenance_item", m["id"], "Oil change")
        self.assertEqual(a["status"], "done")
        self.assertIsNone(await self.db.maintenanceitem.find_unique(where={"id": m["id"]}))

    async def test_maintenance_section_off_refuses(self):
        await self.client.patch("/api/settings", json={"enabledSections": ["tasks"]})
        text = await self.act_error("add_maintenance_item", {"title": "X", "repeat": {"every": "year"}})
        self.assertEqual(text, "Error: The Maintenance section is turned off in Settings.")
