"""Budgets never touch another user's rows or use another user's categories."""

import unittest

from vournal_os.support import OsTestCase


class Budgets(OsTestCase):
    async def test_other_users_rows_are_404(self):
        client = await self.signed_in()
        other = await self.make_user()
        theirs = await self.db.budget.create(data={"userId": other.id, "amount": "100", "currency": "USD"})
        for method, body in (("GET", None), ("PATCH", {"amount": 5}), ("DELETE", None)):
            response = await client.request(method, f"/api/budgets/{theirs.id}", json=body)
            self.assertEqual(response.status_code, 404, method)
            self.assertEqual(response.json()["message"], "Budget not found")
        their_category = await self.db.category.create(data={"userId": other.id, "scope": "EXPENSE", "name": "Theirs"})
        response = await client.post("/api/budgets", json={"amount": 50, "categoryId": their_category.id})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "Category not found")


if __name__ == "__main__":
    unittest.main()
