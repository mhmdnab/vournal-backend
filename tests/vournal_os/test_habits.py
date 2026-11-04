"""Habits never touch another user's rows; check-ins count per local day and never in
the future; a custom schedule needs at least one day."""

import unittest

from vournal_os.support import OsTestCase


class Habits(OsTestCase):
    async def test_other_users_habits_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        habit = (await owner.post("/api/habits", json={"name": "Water"})).json()
        for method, suffix in (("GET", ""), ("PATCH", ""), ("DELETE", ""), ("POST", "/check"), ("DELETE", "/check"), ("GET", "/history"), ("POST", "/archive")):
            response = await other.request(method, f"/api/habits/{habit['id']}{suffix}", json={} if method in ("PATCH",) or suffix == "/check" and method == "POST" else None)
            self.assertEqual((response.status_code, response.json()["message"]), (404, "Habit not found"), f"{method} {suffix}")
        self.assertEqual((await other.post("/api/habits/reorder", json={"items": [{"id": habit["id"], "sortOrder": 3}]})).status_code, 404)

    async def test_check_ins(self):
        client = await self.signed_in()
        habit = (await client.post("/api/habits", json={"name": "Water", "targetPerDay": 2})).json()
        once = (await client.post(f"/api/habits/{habit['id']}/check", json={})).json()
        self.assertEqual((once["todayCount"], once["doneToday"]), (1, False))
        twice = (await client.post(f"/api/habits/{habit['id']}/check", json={})).json()
        self.assertEqual((twice["todayCount"], twice["doneToday"], twice["currentStreak"]), (2, True, 1))
        future = await client.post(f"/api/habits/{habit['id']}/check", json={"date": "2999-01-01"})
        self.assertEqual((future.status_code, future.json()["message"]), (400, "Cannot check a habit in the future"))
        down = (await client.delete(f"/api/habits/{habit['id']}/check")).json()
        self.assertEqual(down["todayCount"], 1)
        cleared = (await client.delete(f"/api/habits/{habit['id']}/check?all=true")).json()
        self.assertEqual(cleared["todayCount"], 0)

    async def test_custom_needs_a_day(self):
        client = await self.signed_in()
        response = await client.post("/api/habits", json={"name": "Gym", "frequency": "CUSTOM", "daysOfWeek": []})
        self.assertEqual((response.status_code, response.json()["message"]), (400, "Pick at least one day of the week for a custom schedule"))


if __name__ == "__main__":
    unittest.main()
