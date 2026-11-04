"""Wellness: the day's balance, remembered foods, water limits, ownership of every log,
and class-validator's wording for the number and text rules."""

import unittest

from vournal_os.support import OsTestCase


class Wellness(OsTestCase):
    async def test_day_balance_with_catalog_manual_and_exercise(self):
        client = await self.signed_in()
        profile = await client.put("/api/wellness/profile", json={
            "sex": "MALE", "birthDate": "1990-06-15", "heightCm": 180, "activityLevel": "MODERATE", "goal": "LOSE", "weeklyRateKg": 0.5})
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["targets"]["missing"], ["weight"])
        await client.put("/api/wellness/weight", json={"weightKg": 82.4, "date": "2026-11-01"})
        await client.post("/api/wellness/food-logs", json={"date": "2026-11-04", "meal": "BREAKFAST", "catalogId": "egg", "servings": 2})
        day = (await client.post("/api/wellness/food-logs", json={
            "date": "2026-11-04", "meal": "LUNCH", "name": " Grandma's stew ", "calories": 450, "protein": 30.5})).json()
        day = (await client.post("/api/wellness/exercise", json={"date": "2026-11-04", "activity": "walking", "durationMin": 30})).json()
        self.assertEqual(day["date"], "2026-11-04")
        eaten = sum(meal["calories"] for meal in day["meals"])
        self.assertEqual(day["eaten"], eaten)
        self.assertEqual(day["allowed"], day["target"] + day["exercise"])
        self.assertEqual(day["remaining"], day["allowed"] - eaten)
        lunch = next(m for m in day["meals"] if m["meal"] == "LUNCH")["items"][0]
        self.assertEqual((lunch["name"], lunch["source"], lunch["protein"]), ("Grandma's stew", "manual", 30.5))
        mine = (await client.get("/api/wellness/foods")).json()
        self.assertEqual([(f["name"], f["useCount"]) for f in mine["items"]], [("Grandma's stew", 1)])
        # walking is 3.5 MET: 3.5 × 82.4 kg × 0.5 h
        self.assertEqual(day["exerciseLogs"][0]["calories"], round(3.5 * 82.4 * 0.5))

    async def test_exercise_without_a_weight_uses_70_kg(self):
        client = await self.signed_in()
        day = (await client.post("/api/wellness/exercise", json={"activity": "running", "durationMin": 60})).json()
        self.assertEqual(day["exerciseLogs"][0]["calories"], round(9.8 * 70))

    async def test_water_stays_between_0_and_20000(self):
        client = await self.signed_in()
        for add, total in ((5000, 5000), (5000, 10000), (5000, 15000), (5000, 20000), (5000, 20000), (-5000, 15000)):
            response = await client.post("/api/wellness/water", json={"date": "2026-11-04", "addMl": add})
            self.assertEqual((response.status_code, response.json()["water"]["ml"]), (200, total))
        empty = await client.post("/api/wellness/water", json={"date": "2026-11-05", "addMl": -300})
        self.assertEqual(empty.json()["water"]["ml"], 0)

    async def test_other_users_logs_are_404(self):
        owner, other = await self.signed_in(), await self.signed_in()
        day = (await owner.post("/api/wellness/food-logs", json={"date": "2026-11-04", "name": "Toast", "calories": 120})).json()
        log = day["meals"][3]["items"][0] if day["meals"][3]["items"] else next(i for m in day["meals"] for i in m["items"])
        food = (await owner.get("/api/wellness/foods")).json()["items"][0]
        exercise = (await owner.post("/api/wellness/exercise", json={"activity": "yoga", "durationMin": 20})).json()["exerciseLogs"][0]
        weight = (await owner.put("/api/wellness/weight", json={"weightKg": 70})).json()["items"][0]
        for method, path, body, message in (
            ("PATCH", f"/food-logs/{log['id']}", {"calories": 1}, "Food log not found"),
            ("DELETE", f"/food-logs/{log['id']}", None, "Food log not found"),
            ("PATCH", f"/foods/{food['id']}", {"calories": 1}, "Food not found"),
            ("DELETE", f"/foods/{food['id']}", None, "Food not found"),
            ("DELETE", f"/exercise/{exercise['id']}", None, "Exercise not found"),
            ("DELETE", f"/weight/{weight['id']}", None, "Weight entry not found"),
            ("POST", "/food-logs", {"foodItemId": food["id"]}, "Food not found"),
            ("POST", "/food-logs", {"catalogId": "no-such-food"}, "Food not found"),
        ):
            with self.subTest(method=method, path=path):
                response = await other.request(method, f"/api/wellness{path}", json=body)
                self.assertEqual((response.status_code, response.json()["message"]), (404, message))
        self.assertEqual(await self.db.foodlog.count(where={"id": log["id"]}), 1)

    async def test_rules_and_messages(self):
        client = await self.signed_in()
        for method, path, body, message in (
            ("PUT", "/profile", {"birthDate": "2020-01-01"}, "Check the birth date (13 to 110 years old)"),
            ("PUT", "/profile", {"goal": "GAIN", "weeklyRateKg": 0.75}, "Gaining more than 0.5 kg a week is not recommended"),
            ("POST", "/food-logs", {"name": "Mystery"}, "Enter the calories, or pick a food"),
            ("POST", "/foods", {}, ["name must be longer than or equal to 1 characters", "name must be a string",
                                    "calories must not be greater than 10000", "calories must not be less than 0", "calories must be an integer number"]),
            ("POST", "/foods", {"name": "x", "calories": 10, "protein": 1.25, "fat": -1},
             ["protein must be a number conforming to the specified constraints", "fat must not be less than 0"]),
            ("POST", "/water", {"addMl": "10"}, ["addMl must not be greater than 5000", "addMl must not be less than -5000", "addMl must be an integer number"]),
            ("PUT", "/profile", {"logReminderTime": "8:30", "sex": "OTHER"},
             ["sex must be one of the following values: MALE, FEMALE", "logReminderTime must be a time like 08:30"]),
            ("PUT", "/weight", {"weightKg": 80, "note": 5}, ["note must be shorter than or equal to 200 characters", "note must be a string"]),
        ):
            with self.subTest(path=path, body=body):
                response = await client.request(method, f"/api/wellness{path}", json=body)
                self.assertEqual((response.status_code, response.json()["message"]), (400, message))

    async def test_null_on_a_required_column_is_invalid_data(self):
        client = await self.signed_in()
        day = (await client.post("/api/wellness/food-logs", json={"date": "2026-11-04", "calories": 100, "remember": False})).json()
        log = next(i for m in day["meals"] for i in m["items"])
        response = await client.patch(f"/api/wellness/food-logs/{log['id']}", json={"calories": None})
        self.assertEqual((response.status_code, response.json()["message"]), (400, "Invalid data"))

    async def test_servings_rescale_a_log(self):
        client = await self.signed_in()
        day = (await client.post("/api/wellness/food-logs", json={"date": "2026-11-04", "calories": 333, "protein": 10.3, "remember": False})).json()
        log = next(i for m in day["meals"] for i in m["items"])
        day = (await client.patch(f"/api/wellness/food-logs/{log['id']}", json={"servings": 1.5})).json()
        log = next(i for m in day["meals"] for i in m["items"])
        self.assertEqual((log["servings"], log["calories"], log["protein"]), (1.5, 500, 15.5))

    async def test_history_streak_ignores_an_empty_today(self):
        client = await self.signed_in()
        from app.os.common import dates

        today = dates.today_date_only("UTC")
        for back in (1, 2, 4):
            day = dates.format_date_only(dates.add_days_date_only(today, -back))
            await client.post("/api/wellness/food-logs", json={"date": day, "calories": 1000, "remember": False})
        history = (await client.get("/api/wellness/history?days=7")).json()
        self.assertEqual((history["streak"], history["loggedDays"], history["averageEaten"], len(history["days"])), (2, 3, 1000, 7))


if __name__ == "__main__":
    unittest.main()
