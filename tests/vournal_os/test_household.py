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

    async def test_join_is_throttled_per_user(self):
        client = await self.signed_in()
        answers = [(await client.post("/api/household/join", json={"code": "ABCD-EFGH"})).status_code for _ in range(11)]
        self.assertEqual(answers, [400] * 10 + [429])
        other = await self.signed_in()  # someone else, from the same address, is not blocked
        self.assertEqual((await other.post("/api/household/join", json={"code": "ABCD-EFGH"})).status_code, 400)


if __name__ == "__main__":
    unittest.main()
