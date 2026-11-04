"""Documents: the number and notes are stored encrypted with the app's field key and
only leave the server decrypted for their owner; lists mask the number."""

import unittest

from vournal_os.support import OsTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x02" * 32


class Documents(OsTestCase):
    async def test_number_and_notes_are_encrypted_at_rest(self):
        from app import mfa

        client = await self.signed_in()
        made = await client.post("/api/documents", json={"name": "Passport", "type": "PASSPORT", "documentNumber": " P1234567 ", "notes": "kept in the safe"})
        self.assertEqual(made.status_code, 201, made.text)
        doc = made.json()
        self.assertEqual((doc["documentNumber"], doc["notes"], doc["documentNumberMasked"]), ("P1234567", "kept in the safe", False))
        row = await self.db.document.find_unique(where={"id": doc["id"]})
        self.assertNotIn("P1234567", row.documentNumberEnc)
        self.assertEqual(mfa.decrypt(row.documentNumberEnc), "P1234567")
        listed = (await client.get("/api/documents")).json()["items"][0]
        self.assertEqual((listed["documentNumber"], listed["documentNumberMasked"], listed["notes"], listed["hasNotes"]), ("••••4567", True, None, True))

    async def test_an_unreadable_value_is_null_not_a_crash(self):
        client = await self.signed_in()
        await self.db.document.create(data={"userId": client.user.id, "name": "Old", "documentNumberEnc": "not-a-token", "notesEnc": "nope"})
        listed = await client.get("/api/documents")
        self.assertEqual(listed.status_code, 200, listed.text)
        item = listed.json()["items"][0]
        self.assertEqual((item["documentNumber"], item["hasDocumentNumber"]), (None, True))
        one = (await client.get(f"/api/documents/{item['id']}")).json()
        self.assertIsNone(one["notes"])

    async def test_other_users_documents_are_404_and_delete_takes_the_files(self):
        owner, other = await self.signed_in(), await self.signed_in()
        doc = (await owner.post("/api/documents", json={"name": "Lease"})).json()
        for method in ("GET", "PATCH", "DELETE"):
            response = await other.request(method, f"/api/documents/{doc['id']}", json={} if method == "PATCH" else None)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Document not found"))
        await owner.post("/api/attachments", data={"entityType": "DOCUMENT", "entityId": doc["id"]}, files={"file": ("lease.png", PNG, "image/png")})
        self.assertEqual(len(self.objects), 1)
        self.assertEqual((await owner.delete(f"/api/documents/{doc['id']}")).json(), {"ok": True})
        self.assertEqual(self.objects, {})


if __name__ == "__main__":
    unittest.main()
