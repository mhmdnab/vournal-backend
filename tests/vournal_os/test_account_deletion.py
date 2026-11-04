"""Deleting an account end to end (spec "Proving nothing broke"): an account with app data
and OS data in every section is deleted from the web, and every row of every table that
points at the user (userId, createdById, ...) is gone, with the files and recordings in R2 and
the household it was alone in. The columns are read from the schema (relations to User, and
plain userId / ...ById columns), so a new table with one of those is covered too; child
tables without a user column go with their parent's cascade."""

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from prisma import Json

from vournal_os.support import PASSWORD, OsTestCase

SCHEMA = Path(__file__).resolve().parents[2] / "prisma" / "schema.prisma"


def user_columns() -> list[tuple[str, str]]:
    """(Prisma client name, column) for every column that points at a user, read from the
    schema's relations (userId, createdById, ...)."""
    models = re.findall(r"^model (\w+) \{(.*?)^\}", SCHEMA.read_text(), re.S | re.M)
    relation = re.compile(r"^\s+\w+\s+User\??\s+@relation\([^)]*fields:\s*\[(\w+)\]", re.M)
    # and plain ones with no relation (nothing cascades there: e.g. HouseholdInvite.createdById)
    plain = re.compile(r"^\s+(userId|\w+ById)\s+String\??\s", re.M)
    return sorted({(name.lower(), column) for name, body in models for column in relation.findall(body) + plain.findall(body)})


def day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


class AccountDeletion(OsTestCase):
    async def fill(self, client) -> None:
        """Something in every section, through the routes the dashboard uses."""

        async def post(path, body, expect=(200, 201)):
            r = await client.post(f"/api{path}", json=body)
            self.assertIn(r.status_code, expect, (path, r.text))
            return r.json()

        uid = client.user.id
        food = next(c for c in (await client.get("/api/categories?scope=EXPENSE")).json()["items"] if c["name"] == "Food")
        account = await post("/accounts", {"name": "Visa", "type": "CREDIT_CARD", "currency": "USD"})
        await post("/transactions", {"type": "EXPENSE", "title": "Lunch", "amount": 12, "categoryId": food["id"], "accountId": account["id"]})
        await post("/budgets", {"amount": 100, "categoryId": food["id"]})
        await post("/recurring-payments", {"name": "Rent", "amount": 700, "startDate": day(-30), "nextDate": day(2)})
        debt = await post("/debts", {"direction": "I_OWE", "title": "Loan", "counterparty": "Rami", "principal": 300})
        await post(f"/debts/{debt['id']}/payments", {"amount": 50})
        project = await post("/projects", {"name": "Launch"})
        task = await post("/tasks", {"title": "Slides", "projectId": project["id"], "dueAt": day(1)})
        await post("/events", {"title": "Sync", "startAt": f"{day(1)}T09:00:00.000Z", "endAt": f"{day(1)}T10:00:00.000Z"})
        await post("/reminders", {"title": "Pills", "remindAt": f"{day(1)}T08:00:00.000Z"})
        await post("/notes", {"title": "Ideas", "content": "a book"})
        await post("/documents", {"name": "Passport", "type": "PASSPORT", "expiryDate": day(300), "documentNumber": "P123"})
        habit = await post("/habits", {"name": "Read", "startDate": day(-3)})
        await post(f"/habits/{habit['id']}/check", {"date": day(-1)})
        goal = await post("/goals", {"name": "Marathon", "type": "FITNESS", "targetValue": 40})
        await post(f"/goals/{goal['id']}/milestones", {"title": "10 km"})
        shop = await post("/shopping/lists", {"name": "Home"})
        await post("/shopping/items", {"name": "Milk", "listId": shop["id"]})
        recipe = await post("/recipes", {"name": "Soup", "servings": 2})
        await post("/meal-plan", {"date": day(0), "mealType": "DINNER", "recipeId": recipe["id"]})
        person = await post("/people", {"name": "Sara", "birthday": "1995-05-01"})
        await post(f"/people/{person['id']}/gifts", {"title": "Scarf"})
        await post("/important-dates", {"title": "Exam", "date": day(10)})
        course = await post("/study/courses", {"name": "Maths"})
        await post("/study/sessions", {"courseId": course["id"], "durationMinutes": 30, "topic": "Algebra"})
        trip = await post("/trips", {"name": "Rome", "destination": "Rome", "startDate": day(20), "endDate": day(25)})
        await post(f"/trips/{trip['id']}/items", {"type": "ACTIVITY", "title": "Colosseum", "startAt": f"{day(21)}T09:00:00.000Z"})
        await post(f"/trips/{trip['id']}/packing", {"name": "Passport"})
        await post(f"/trips/{trip['id']}/share", {})
        await post("/maintenance", {"title": "Oil change", "area": "CAR", "rrule": "FREQ=MONTHLY;INTERVAL=6", "nextDueAt": day(5)})
        assert (await client.put("/api/wellness/profile", json={"sex": "MALE", "birthDate": "1995-05-01", "heightCm": 178, "weightKg": 80,
                                                                 "goal": "LOSE", "activityLevel": "MODERATE"})).status_code == 200
        await post("/wellness/food-logs", {"name": "Rice", "calories": 200})
        await post("/wellness/water", {"addMl": 500})
        await post("/push/subscribe", {"endpoint": "https://push.example/sub/delete-me", "keys": {"p256dh": "k", "auth": "a"}})
        await post("/household", {"name": "Home"})
        self.household = (await self.db.householdmember.find_unique(where={"userId": uid})).householdId
        await post("/household/invite", {"email": "someone@example.test"})
        upload = await client.post("/api/attachments", data={"entityType": "TASK", "entityId": task["id"]},
                                   files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")})
        self.assertEqual(upload.status_code, 201, upload.text)
        await client.post("/api/notifications/sync")
        # the app's own data and the chat (made directly: no model is called)
        entry = await self.db.entry.create(data={"userId": uid, "rawTranscript": "Called Sara", "summary": "A call", "status": "DONE",
                                                 "audioUrl": f"audio/{uid}/call.m4a"})
        await self.db.todo.create(data={"entryId": entry.id, "userId": uid, "text": "Buy milk"})
        conversation = await self.db.assistantconversation.create(data={"userId": uid, "title": "Hi"})
        await self.db.assistantmessage.create(data={"conversationId": conversation.id, "role": "user", "content": Json([{"type": "text", "text": "hi"}])})
        await self.db.activitylog.create(data={"userId": uid, "action": "ai.chat"})

    async def test_every_row_and_file_goes(self):
        client = await self.signed_in()
        await client.patch("/api/settings", json={"timezone": "Asia/Beirut", "aiEnabled": True})
        await self.fill(client)
        uid = client.user.id
        columns = user_columns()
        before = {(t, c): await getattr(self.db, t).count(where={c: uid}) for t, c in columns}
        filled = sorted(f"{t}.{c}" for (t, c), n in before.items() if n)
        self.assertGreaterEqual(len(filled), 35, filled)  # the account really holds something almost everywhere
        self.assertEqual(len(self.objects), 1)

        import app.auth

        with patch.object(app.auth, "delete_audio", AsyncMock()) as delete_audio:
            response = await client.request("DELETE", "/api/users/me", json={"password": PASSWORD})
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))
        # the recordings in R2 (removed through delete_audio) and the household (no user column: its only member left)
        self.assertEqual([c.args[0] for c in delete_audio.await_args_list], [f"audio/{uid}/call.m4a"])
        self.assertIsNone(await self.db.household.find_unique(where={"id": self.household}))

        left = {f"{t}.{c}": n for t, c in columns if (n := await getattr(self.db, t).count(where={c: uid}))}
        self.assertEqual(left, {})
        self.assertIsNone(await self.db.user.find_unique(where={"id": uid}))
        self.assertEqual(self.objects, {})
        self.assertEqual((await client.get("/api/auth/me")).status_code, 401)  # its cookies no longer sign anyone in


if __name__ == "__main__":
    unittest.main()
