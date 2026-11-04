"""Households: an account deleted while in one leaves it settled (NestJS deleteAccount),
and joining is throttled per user."""

import unittest

from vournal_os.support import OsTestCase


class Household(OsTestCase):
    async def household_of_three(self):
        owner, sam, lina = await self.signed_in(), await self.signed_in(), await self.signed_in()
        await owner.post("/api/household", json={"name": "Home"})
        for member in (sam, lina):
            code = (await owner.post("/api/household/invite")).json()["code"]
            self.assertEqual((await member.post("/api/household/join", json={"code": code})).status_code, 200)
        return owner, sam, lina

    async def test_deleting_the_owners_account_hands_the_household_on(self):
        from app.auth import delete_account

        owner, sam, lina = await self.household_of_three()
        household_id = (await self.db.householdmember.find_unique(where={"userId": owner.user.id})).householdId
        await delete_account(owner.user)
        roles = {m.userId: m.role for m in await self.db.householdmember.find_many(where={"householdId": household_id})}
        self.assertEqual(roles, {sam.user.id: "OWNER", lina.user.id: "MEMBER"})
        await delete_account(sam.user)
        await delete_account(lina.user)
        self.assertIsNone(await self.db.household.find_unique(where={"id": household_id}))

    async def test_a_removed_member_cannot_come_back_with_their_own_code(self):
        from app.auth import delete_account

        owner, sam, lina = await self.household_of_three()
        sams_code = (await sam.post("/api/household/invite")).json()["code"]
        linas_code = (await lina.post("/api/household/invite")).json()["code"]
        await owner.delete(f"/api/household/members/{sam.user.id}")
        again = await sam.post("/api/household/join", json={"code": sams_code})
        self.assertEqual((again.status_code, again.json()["message"]), (400, "This invite code is not valid or has expired"))
        await delete_account(lina.user)  # a deleted account's codes stop too
        stranger = await self.signed_in()
        self.assertEqual((await stranger.post("/api/household/join", json={"code": linas_code})).status_code, 400)

    async def test_deleting_an_account_outside_a_household_opens_no_transaction(self):
        """Build 8's DELETE /auth/me: the cascade must not run inside a 5-second transaction
        for an account that was never in a household."""
        from unittest.mock import patch

        from app.auth import delete_account

        user = await self.make_user()
        with patch.object(type(self.db), "tx", side_effect=AssertionError("transaction opened")):
            await delete_account(user)
        self.assertIsNone(await self.db.user.find_unique(where={"id": user.id}))

    async def test_join_is_throttled_per_user(self):
        client = await self.signed_in()
        answers = [(await client.post("/api/household/join", json={"code": "ABCD-EFGH"})).status_code for _ in range(11)]
        self.assertEqual(answers, [400] * 10 + [429])
        other = await self.signed_in()  # someone else, from the same address, is not blocked
        self.assertEqual((await other.post("/api/household/join", json={"code": "ABCD-EFGH"})).status_code, 400)


if __name__ == "__main__":
    unittest.main()
