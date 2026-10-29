"""Plus grants and revocations — the only code that writes plan/planExpiresAt.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.entitlements import account_token_for, effective_plan, grant_plus, revoke_plus


def _in(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


class Entitlements(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db

        self.db = db
        await db.connect()
        self.user = await db.user.create(
            data={"email": f"ent-{uuid.uuid4().hex}@example.test"}
        )

    async def asyncTearDown(self):
        await self.db.user.delete_many(where={"id": self.user.id})
        await self.db.disconnect()

    async def _reload(self):
        return await self.db.user.find_unique(where={"id": self.user.id})

    async def test_grant_sets_plus_until_expiry(self):
        expires = _in(30)
        await grant_plus(self.db, self.user.id, expires, "apple", "orig-1")
        user = await self._reload()
        self.assertEqual(effective_plan(user), "plus")
        self.assertEqual(user.planSource, "apple")
        self.assertEqual(user.appleOriginalTransactionId, "orig-1")
        self.assertAlmostEqual(
            user.planExpiresAt.timestamp(), expires.timestamp(), delta=1
        )

    async def test_later_expiry_wins(self):
        later = _in(40)
        await grant_plus(self.db, self.user.id, later, "paddle", "sub_1")
        await grant_plus(self.db, self.user.id, _in(30), "apple", "orig-1")
        user = await self._reload()
        self.assertAlmostEqual(user.planExpiresAt.timestamp(), later.timestamp(), delta=1)
        self.assertEqual(user.planSource, "paddle")
        self.assertEqual(user.paddleSubscriptionId, "sub_1")
        self.assertEqual(user.appleOriginalTransactionId, "orig-1")

    async def test_revoke_only_ends_its_own_source(self):
        await grant_plus(self.db, self.user.id, _in(30), "paddle", "sub_1")
        await revoke_plus(self.db, self.user.id, "apple")
        self.assertEqual(effective_plan(await self._reload()), "plus")
        await revoke_plus(self.db, self.user.id, "paddle")
        self.assertEqual(effective_plan(await self._reload()), "free")

    async def test_account_token_is_stable_uuid(self):
        first = await account_token_for(self.db, self.user.id)
        second = await account_token_for(self.db, self.user.id)
        self.assertEqual(first, second)
        self.assertEqual(uuid.UUID(first).version, 4)


if __name__ == "__main__":
    unittest.main()
