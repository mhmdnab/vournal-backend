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

    async def test_lifetime_grant_is_never_shortened(self):
        # A manual grant with no expiry (planExpiresAt NULL) is lifetime Plus.
        await self.db.user.update(
            where={"id": self.user.id},
            data={"plan": "plus", "planExpiresAt": None, "planSource": "manual"},
        )
        await grant_plus(self.db, self.user.id, _in(0.01), "apple", "orig-1")
        user = await self._reload()
        self.assertIsNone(user.planExpiresAt)
        self.assertEqual(effective_plan(user), "plus")

    async def test_revoke_only_matching_subscription(self):
        await grant_plus(self.db, self.user.id, _in(30), "paddle", "sub_1")
        await revoke_plus(self.db, self.user.id, "paddle", "sub_2")
        self.assertEqual(effective_plan(await self._reload()), "plus")
        await revoke_plus(self.db, self.user.id, "paddle", "sub_1")
        self.assertEqual(effective_plan(await self._reload()), "free")

    async def test_original_transaction_moves_to_new_owner(self):
        # Two vournal accounts on one Apple ID: Apple reuses the original
        # transaction id when the second account subscribes after a lapse.
        other = await self.db.user.create(
            data={"email": f"ent-{uuid.uuid4().hex}@example.test"}
        )
        try:
            await grant_plus(self.db, other.id, _in(-1), "apple", "orig-shared")
            await grant_plus(self.db, self.user.id, _in(30), "apple", "orig-shared")
            user = await self._reload()
            self.assertEqual(user.appleOriginalTransactionId, "orig-shared")
            self.assertEqual(effective_plan(user), "plus")
            moved = await self.db.user.find_unique(where={"id": other.id})
            self.assertIsNone(moved.appleOriginalTransactionId)
        finally:
            await self.db.user.delete_many(where={"id": other.id})

    async def test_purchase_older_than_a_refund_is_ignored(self):
        bought = datetime.now(timezone.utc) - timedelta(minutes=5)
        await grant_plus(self.db, self.user.id, _in(30), "apple", "orig-1", purchased_at=bought)
        await revoke_plus(self.db, self.user.id, "apple", "orig-1", refunded=True)
        await grant_plus(self.db, self.user.id, _in(30), "apple", "orig-1", purchased_at=bought)
        self.assertEqual(effective_plan(await self._reload()), "free")
        # A genuinely new purchase after the refund still counts.
        await grant_plus(
            self.db, self.user.id, _in(30), "apple", "orig-1",
            purchased_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        self.assertEqual(effective_plan(await self._reload()), "plus")


if __name__ == "__main__":
    unittest.main()
