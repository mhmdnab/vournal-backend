"""Goals: ownership (milestones are reached only through their goal), progress by
delta or value, reaching the target completes the goal and ticks value milestones."""

import unittest

from vournal_os.support import OsTestCase


class Goals(OsTestCase):
    async def test_other_users_goals_and_milestones_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        goal = (await owner.post("/api/goals", json={"name": "Run"})).json()
        milestone = (await owner.post(f"/api/goals/{goal['id']}/milestones", json={"title": "5k"})).json()
        for method, path, message in (
            ("GET", f"/api/goals/{goal['id']}", "Goal not found"), ("PATCH", f"/api/goals/{goal['id']}", "Goal not found"),
            ("POST", f"/api/goals/{goal['id']}/progress", "Goal not found"), ("DELETE", f"/api/goals/{goal['id']}", "Goal not found"),
            ("GET", f"/api/goals/{goal['id']}/milestones", "Goal not found"),
            ("PATCH", f"/api/goals/{goal['id']}/milestones/{milestone['id']}", "Milestone not found"),
            ("POST", f"/api/goals/{goal['id']}/milestones/{milestone['id']}/toggle", "Milestone not found"),
        ):
            body = {"delta": 1} if path.endswith("progress") else ({} if method in ("PATCH", "POST") else None)
            response = await other.request(method, path, json=body)
            self.assertEqual((response.status_code, response.json()["message"]), (404, message), f"{method} {path}")

    async def test_progress_completes_and_ticks_milestones(self):
        client = await self.signed_in()
        goal = (await client.post("/api/goals", json={"name": "Books", "targetValue": 10, "unit": "books"})).json()
        await client.post(f"/api/goals/{goal['id']}/milestones", json={"title": "Half", "targetValue": 5})
        both = await client.post(f"/api/goals/{goal['id']}/progress", json={"delta": 1, "value": 2})
        self.assertEqual((both.status_code, both.json()["message"]), (400, 'Send exactly one of "delta" or "value"'))
        half = (await client.post(f"/api/goals/{goal['id']}/progress", json={"value": 6})).json()
        self.assertEqual((half["progressPercent"], half["milestones"][0]["completed"], half["status"]), (60, True, "ACTIVE"))
        done = (await client.post(f"/api/goals/{goal['id']}/progress", json={"delta": 4})).json()
        self.assertEqual((done["status"], done["progressPercent"]), ("COMPLETED", 100))
        back = (await client.post(f"/api/goals/{goal['id']}/progress", json={"delta": -3})).json()
        self.assertEqual(back["status"], "ACTIVE")

    async def test_a_linked_goal_follows_its_account(self):
        client = await self.signed_in()
        account = (await client.post("/api/accounts", json={"name": "Savings", "initialBalance": 400})).json()
        personal = await client.post("/api/goals", json={"name": "x", "linkedAccountId": account["id"]})
        self.assertEqual(personal.json()["message"], "Only financial goals can be linked to an account")
        goal = (await client.post("/api/goals", json={"name": "Car", "type": "FINANCIAL", "targetValue": 1000, "linkedAccountId": account["id"]})).json()
        self.assertEqual((goal["current"], goal["progressPercent"], goal["displayCurrency"]), (400, 40, "USD"))
        refused = await client.post(f"/api/goals/{goal['id']}/progress", json={"delta": 5})
        self.assertEqual(refused.status_code, 400)


if __name__ == "__main__":
    unittest.main()
