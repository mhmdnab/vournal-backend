"""Documents answer like NestJS did (each side encrypts with its own key; the answers
are compared decrypted): masks, expiry status and windows, filters, validation."""

import unittest
from datetime import datetime, timedelta, timezone

from vournal_os.parity.harness import ParityCase


def day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


class DocumentsParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def make(self, body):
        nest, py = await self.both("POST", "/documents", body)
        self.assertParity(nest, py)
        return nest.json().get("id"), py.json().get("id")

    async def on_both(self, method, ids, json=None):
        kwargs = {} if json is None else {"json": json}
        return await self.nest.request(method, f"/documents/{ids[0]}", **kwargs), await self.py.request(method, f"/api/documents/{ids[1]}", **kwargs)

    async def test_documents(self):
        passport = await self.make({"name": " Passport ", "type": "PASSPORT", "documentNumber": " P12345678 ", "notes": "safe", "issueDate": day(-3000), "expiryDate": day(20)})
        await self.make({"name": "Licence", "type": "DRIVERS_LICENSE", "documentNumber": "AB12", "expiryDate": day(-5), "issuer": "MoI"})
        await self.make({"name": "Warranty", "type": "WARRANTY", "documentNumber": "W-99812", "expiryDate": day(200), "reminderDaysBefore": 365})
        await self.make({"name": "Will", "notes": "   ", "documentNumber": ""})
        for query in ("", "?type=PASSPORT,WARRANTY", "?status=expiringSoon", "?status=expired,noExpiry", "?q=moi", "?status=lost", "?type=PLANET", "?bogus=1"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/documents{query}"))
        self.assertParity(*await self.on_both("GET", passport))
        for body in ({"documentNumber": None, "notes": "moved"}, {"expiryDate": None}, {"type": None, "reminderDaysBefore": None},
                     {"name": None}, {"issueDate": day(10), "expiryDate": day(5)}, {"holderName": "  "}):
            with self.subTest(body=body):
                self.assertParity(*await self.on_both("PATCH", passport, body))
        self.assertParity(*await self.on_both("DELETE", passport))
        self.assertParity(*await self.on_both("GET", passport))

    async def test_validation(self):
        for body in ({}, {"name": ""}, {"name": "x", "type": "DIARY"}, {"name": "x", "documentNumber": "x" * 101}, {"name": "x", "reminderDaysBefore": 731},
                     {"name": "x", "expiryDate": "2026-13-01"}, {"name": "x", "issueDate": day(5), "expiryDate": day(1)}, {"name": "x", "extra": 1}):
            with self.subTest(body=body):
                self.assertParity(*await self.both("POST", "/documents", body))


if __name__ == "__main__":
    unittest.main()
