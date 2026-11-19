"""The assistant's trip, itinerary and packing tools (design 2026-11-18)."""

from vournal_os.assistant_life_base import LifeBase


class TripTools(LifeBase):
    async def trip(self, **data):
        return await self.post("/api/trips", {"name": "Paris", "destination": "Paris", "startDate": "2026-12-12", "endDate": "2026-12-18", **data})

    async def test_create_trip_and_undo(self):
        a = await self.act("create_trip", {"name": "Paris", "destination": "Paris", "start_date": "2026-12-12", "end_date": "2026-12-18",
                                           "budget": 1500, "transport": "Flight MEA"})
        self.assertEqual((a["kind"], a["entityType"], a["label"], a["link"]), ("create", "trip", "Paris", f"/travel?id={a['entityId']}"))
        t = await self.db.trip.find_unique(where={"id": a["entityId"]})
        self.assertEqual((t.startDate.date().isoformat(), t.endDate.date().isoformat(), float(t.budget), t.transportation, t.status),
                         ("2026-12-12", "2026-12-18", 1500.0, "Flight MEA", "PLANNING"))
        await self.undo(a)
        self.assertIsNone(await self.db.trip.find_unique(where={"id": a["entityId"]}))

    async def test_end_before_start_is_the_routes_error(self):
        text = await self.act_error("create_trip", {"name": "X", "start_date": "2026-12-12", "end_date": "2026-12-01"})
        self.assertIn("endDate must be on or after startDate", text)

    async def test_update_trip_status_and_undo(self):
        t = await self.trip()
        a = await self.act("update_trip", {"id": t["id"], "status": "BOOKED", "end_date": "2026-12-20"})
        self.assertEqual((a["kind"], a["entityType"]), ("update", "trip"))
        row = await self.db.trip.find_unique(where={"id": t["id"]})
        self.assertEqual((row.status, row.endDate.date().isoformat()), ("BOOKED", "2026-12-20"))
        await self.undo(a)
        row = await self.db.trip.find_unique(where={"id": t["id"]})
        self.assertEqual((row.status, row.endDate.date().isoformat()), ("PLANNING", "2026-12-18"))

    async def test_add_itinerary_item_with_local_time(self):
        t = await self.trip()
        a = await self.act("add_trip_item", {"trip_id": t["id"], "list": "itinerary", "title": "Flight", "type": "TRANSPORT",
                                             "start": "2026-12-12T09:30", "confirmation_code": "ABC123", "cost": 300})
        self.assertEqual((a["kind"], a["entityType"], a["link"]), ("create", "trip_item", f"/travel/{t['id']}"))
        item = await self.db.tripitem.find_unique(where={"id": a["entityId"]})
        # 09:30 in Beirut (UTC+2 in December) is 07:30 UTC
        self.assertEqual((item.title, item.type, item.startAt.isoformat()[:16], item.confirmationCode, float(item.cost)),
                         ("Flight", "TRANSPORT", "2026-12-12T07:30", "ABC123", 300.0))
        await self.undo(a)
        self.assertIsNone(await self.db.tripitem.find_unique(where={"id": a["entityId"]}))

    async def test_add_itinerary_item_date_only_lands_on_that_local_day(self):
        t = await self.trip()
        a = await self.act("add_trip_item", {"trip_id": t["id"], "list": "itinerary", "title": "Louvre", "start": "2026-12-13"})
        item = await self.db.tripitem.find_unique(where={"id": a["entityId"]})
        self.assertEqual(item.startAt.isoformat()[:10], "2026-12-13")

    async def test_add_packing_item(self):
        t = await self.trip()
        a = await self.act("add_trip_item", {"trip_id": t["id"], "list": "packing", "title": "Charger", "quantity": 2, "group": "Tech"})
        self.assertEqual((a["kind"], a["entityType"]), ("create", "packing_item"))
        p = await self.db.packingitem.find_unique(where={"id": a["entityId"]})
        self.assertEqual((p.name, p.quantity, p.group, p.isPacked), ("Charger", 2, "Tech", False))

    async def test_tick_packing_item_and_undo(self):
        t = await self.trip()
        p = await self.post(f"/api/trips/{t['id']}/packing", {"name": "Passport"})
        a = await self.act("update_trip_item", {"trip_id": t["id"], "list": "packing", "id": p["id"], "done": True})
        self.assertEqual((a["kind"], a["entityType"]), ("complete", "packing_item"))
        self.assertTrue((await self.db.packingitem.find_unique(where={"id": p["id"]})).isPacked)
        await self.undo(a)
        self.assertFalse((await self.db.packingitem.find_unique(where={"id": p["id"]})).isPacked)

    async def test_update_itinerary_item_and_undo(self):
        t = await self.trip()
        i = await self.post(f"/api/trips/{t['id']}/items", {"title": "Hotel", "type": "ACCOMMODATION", "location": "Old town"})
        a = await self.act("update_trip_item", {"trip_id": t["id"], "list": "itinerary", "id": i["id"], "location": "Le Marais", "done": True})
        row = await self.db.tripitem.find_unique(where={"id": i["id"]})
        self.assertEqual((row.location, row.isDone), ("Le Marais", True))
        await self.undo(a)
        row = await self.db.tripitem.find_unique(where={"id": i["id"]})
        self.assertEqual((row.location, row.isDone), ("Old town", False))

    async def test_packing_field_on_itinerary_is_an_error(self):
        t = await self.trip()
        text = await self.act_error("add_trip_item", {"trip_id": t["id"], "list": "itinerary", "title": "X", "quantity": 2})
        self.assertIn('"quantity"', text)
        text = await self.act_error("add_trip_item", {"trip_id": t["id"], "list": "packing", "title": "X", "start": "2026-12-12"})
        self.assertIn('"start"', text)

    async def test_item_of_someone_elses_trip_is_not_found(self):
        other = await self.signed_in()
        theirs = (await other.post("/api/trips", json={"name": "Theirs", "startDate": "2026-12-01"})).json()
        self.assertIn("Trip not found", await self.act_error("add_trip_item", {"trip_id": theirs["id"], "list": "packing", "title": "X"}))

    async def test_delete_trip_asks_when_it_has_items(self):
        t = await self.trip()
        await self.post(f"/api/trips/{t['id']}/packing", {"name": "Passport"})
        a = await self.delete("trip", t["id"], "Paris")
        self.assertEqual((a["status"], a["link"]), ("pending", f"/travel?id={t['id']}"))
        done = await self.approve(a)
        self.assertIsNone(await self.db.trip.find_unique(where={"id": t["id"]}))
        await self.undo(done)
        self.assertEqual(await self.db.packingitem.count(where={"tripId": t["id"]}), 1)

    async def test_delete_packing_item(self):
        t = await self.trip()
        p = await self.post(f"/api/trips/{t['id']}/packing", {"name": "Socks"})
        a = await self.delete("packing_item", p["id"], "Socks")
        self.assertEqual(a["status"], "done")
        self.assertIsNone(await self.db.packingitem.find_unique(where={"id": p["id"]}))

    async def test_travel_section_off_refuses(self):
        await self.client.patch("/api/settings", json={"enabledSections": ["tasks"]})
        self.assertEqual(await self.act_error("create_trip", {"name": "X"}), "Error: The Travel section is turned off in Settings.")
