"""Shopping: the expense messages (vournalOS's shopping.messages.spec.ts), plural rules
against Node's Intl.PluralRules, list access with households, the default list."""

import unittest

from vournal_os.support import OsTestCase


class Messages(unittest.TestCase):
    def test_english_output(self):
        from app.os.shopping import expense_notes, expense_title

        self.assertEqual(expense_title("en", "Grocery"), "Shopping: Grocery")
        self.assertEqual(expense_notes("en", 1, "Grocery"), '1 item from shopping list "Grocery"')
        self.assertEqual(expense_notes("en", 4, "Grocery"), '4 items from shopping list "Grocery"')

    def test_arabic_plural_forms_and_western_digits(self):
        from app.os.shopping import expense_notes, expense_title

        self.assertEqual(expense_title("ar", "البقالة"), "تسوّق: البقالة")
        self.assertEqual(expense_notes("ar", 1, "البقالة"), "غرض واحد من قائمة التسوّق «البقالة»")
        self.assertEqual(expense_notes("ar", 2, "البقالة"), "غرضان من قائمة التسوّق «البقالة»")
        self.assertEqual(expense_notes("ar", 5, "البقالة"), "5 أغراض من قائمة التسوّق «البقالة»")
        self.assertEqual(expense_notes("ar", 12, "البقالة"), "12 غرضًا من قائمة التسوّق «البقالة»")

    def test_plural_categories_match_intl(self):
        from app.os.common.i18n import plural_category

        # printed by Node: [0,1,2,3,10,11,99,100,101,102,103,111,1000].map(n => new Intl.PluralRules('ar').select(n))
        ar = ["zero", "one", "two", "few", "few", "many", "many", "other", "other", "other", "few", "many", "other"]
        numbers = [0, 1, 2, 3, 10, 11, 99, 100, 101, 102, 103, 111, 1000]
        self.assertEqual([plural_category("ar", n) for n in numbers], ar)
        self.assertEqual([plural_category("en", n) for n in (0, 1, 2)], ["other", "one", "other"])


class Shopping(OsTestCase):
    async def test_lists_are_private_unless_shared_with_the_household(self):
        owner, member, stranger = await self.signed_in(), await self.signed_in(), await self.signed_in()
        made = (await owner.post("/api/shopping/lists", json={"name": "Weekly"})).json()
        self.assertEqual((await member.get(f"/api/shopping/lists/{made['id']}")).status_code, 404)
        not_yet = await owner.patch(f"/api/shopping/lists/{made['id']}", json={"shared": True})
        self.assertEqual(not_yet.json()["message"], "Create or join a household first to share a list")
        house = await self.db.household.create(data={"name": "Home"})
        for c in (owner, member):
            await self.db.householdmember.create(data={"householdId": house.id, "userId": c.user.id, "role": "MEMBER"})
        shared = (await owner.patch(f"/api/shopping/lists/{made['id']}", json={"shared": True})).json()
        self.assertTrue(shared["isShared"])
        seen = (await member.get(f"/api/shopping/lists/{made['id']}")).json()
        self.assertFalse(seen["isOwner"])
        item = await member.post("/api/shopping/items", json={"listId": made["id"], "name": "Milk"})
        self.assertEqual(item.status_code, 201, item.text)
        self.assertEqual((await member.patch(f"/api/shopping/lists/{made['id']}", json={"name": "Mine"})).status_code, 404)
        self.assertEqual((await stranger.get(f"/api/shopping/items/{item.json()['id']}")).status_code, 404)

    async def test_an_item_without_a_list_lands_in_a_grocery_list(self):
        client = await self.signed_in()
        await self.db.shoppinglist.delete_many(where={"userId": client.user.id})
        await client.patch("/api/settings", json={"language": "ar"})
        item = (await client.post("/api/shopping/items", json={"name": "خبز"})).json()
        lists = (await client.get("/api/shopping/lists")).json()["items"]
        self.assertEqual([(l["name"], l["id"]) for l in lists], [("البقالة", item["listId"])])


if __name__ == "__main__":
    unittest.main()
