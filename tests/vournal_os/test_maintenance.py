"""Maintenance items take their attachments with them: the rows and the files in R2
(NestJS kept the bytes in the row, so deleting the rows was enough there)."""

import unittest

from vournal_os.support import OsTestCase


class Maintenance(OsTestCase):
    async def test_deleting_an_item_deletes_its_files(self):
        client = await self.signed_in()
        item = (await client.post("/api/maintenance", json={"title": "Boiler", "rrule": "FREQ=YEARLY"})).json()
        other = (await client.post("/api/maintenance", json={"title": "Gutters", "rrule": "FREQ=YEARLY"})).json()
        for entity, name in ((item, "invoice.pdf"), (item, "photo.png"), (other, "kept.pdf")):
            uploaded = await client.post("/api/attachments", data={"entityType": "MAINTENANCE", "entityId": entity["id"]},
                                         files={"file": (name, b"%PDF-1.4 x", "application/pdf") if name.endswith("pdf") else (name, b"\x89PNG\r\n\x1a\n", "image/png")})
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(len(self.objects), 3)
        self.assertEqual((await client.delete(f"/api/maintenance/{item['id']}")).json(), {"ok": True})
        self.assertEqual(await self.db.attachment.count(where={"entityId": item["id"]}), 0)
        self.assertEqual(len(self.objects), 1)  # only the other item's file is left
        self.assertEqual(await self.db.attachment.count(where={"entityId": other["id"]}), 1)


if __name__ == "__main__":
    unittest.main()
