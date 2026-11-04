"""GET /api/users/me/export: everything the account owns, the app's journal too, with
documents decrypted for their owner, and no secret and nothing of another account."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase


class Export(OsTestCase):
    async def test_export_holds_the_account_and_nothing_secret(self):
        client, other = await self.signed_in(), await self.signed_in()
        await client.post("/api/tasks", json={"title": "Pay rent"})
        await client.post("/api/documents", json={"name": "Passport", "type": "PASSPORT", "documentNumber": "P1234567", "notes": "in the safe"})
        await client.post("/api/people", json={"name": "Sara"})
        await other.post("/api/tasks", json={"title": "Not mine"})
        entry = await self.db.entry.create(data={"userId": client.user.id, "rawTranscript": "Called Sara about rent", "summary": "Rent",
                                                 "emotions": ["calm"], "audioUrl": "audio/secret-key.m4a"})
        await self.db.todo.create(data={"entryId": entry.id, "userId": client.user.id, "text": "Send rent"})

        response = await client.get("/api/users/me/export")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('attachment; filename="life-os-export-', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        data = body["data"]
        self.assertEqual((body["formatVersion"], body["user"]["email"]), (1, client.user.email))
        self.assertEqual([t["title"] for t in data["tasks"]], ["Pay rent"])
        self.assertNotIn("user", data["tasks"][0])  # rows, not their relations
        doc = data["documents"][0]
        self.assertEqual((doc["documentNumber"], doc["notes"]), ("P1234567", "in the safe"))
        self.assertNotIn("documentNumberEnc", doc)
        self.assertEqual([p["name"] for p in data["people"]], ["Sara"])
        self.assertEqual([e["rawTranscript"] for e in data["journalEntries"]], ["Called Sara about rent"])
        self.assertEqual([t["text"] for t in data["appTodos"]], ["Send rent"])
        text = json.dumps(body)
        for secret in ("passwordHash", "refreshToken", "secret-key", "Not mine", "audioUrl", "appleRefreshToken", "twoFactorSecret"):
            self.assertNotIn(secret, text)

    async def test_export_is_throttled(self):
        client = await self.signed_in()
        codes = [(await client.get("/api/users/me/export")).status_code for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])

    async def test_an_account_without_plus_can_still_take_its_data(self):
        """Owner's call (4 Nov 2026): when Plus lapses, the export stays open; the rest of /api doesn't."""
        lapsed = await self.make_user(planExpiresAt=datetime.now(timezone.utc) - timedelta(days=1))
        await self.db.task.create(data={"userId": lapsed.id, "title": "Made while on Plus"})
        for user in (lapsed, await self.make_user(plan="free")):
            client = await self.signed_in(user)
            response = await client.get("/api/users/me/export")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["user"]["email"], user.email)
            self.assertEqual((await client.get("/api/tasks")).status_code, 403)
        self.assertEqual([t["title"] for t in response.json()["data"]["tasks"]], [])
        lapsed_client = await self.signed_in(lapsed)
        self.assertEqual([t["title"] for t in (await lapsed_client.get("/api/users/me/export")).json()["data"]["tasks"]], ["Made while on Plus"])


if __name__ == "__main__":
    unittest.main()
