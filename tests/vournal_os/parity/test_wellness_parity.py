"""Wellness answers like NestJS did: the profile and its targets, the day's balance,
built-in, remembered and manual foods in both languages, exercise, water, weight and
history. The API is metric; pounds and feet are the dashboard's own conversion."""

import unittest

from vournal_os.parity.harness import ParityCase


def _log_ids(day: dict) -> list[str]:
    return [i["id"] for m in day["meals"] for i in m["items"]]


class WellnessParity(ParityCase):
    maxDiff = None

    async def pair(self, method, paths, body=None):
        """Same request to both; `paths` may be a (nest, python) pair for ids. Returns both bodies."""
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}/wellness{p}", json=body))
        self.assertParity(*out)
        return tuple(r.json() for r in out)

    async def test_profile_targets(self):
        await self.pair("GET", "/profile")
        await self.pair("PUT", "/profile", {"sex": "FEMALE", "birthDate": "1994-03-09", "heightCm": 165.5, "weightKg": 68.25,
                                            "activityLevel": "LIGHT", "goal": "LOSE", "targetWeightKg": 60, "weeklyRateKg": 0.75})
        for body in (
            {"goal": "GAIN", "weeklyRateKg": 0.25, "calorieTargetOverride": 2100, "waterGoalMl": 2500, "trackProtein": True},
            {"calorieTargetOverride": None, "waterGoalMl": None, "logReminderTime": None, "sex": None},
            {"goal": "MAINTAIN", "birthDate": "2010-11", "heightCm": None},
            {"birthDate": "1960-02-29T10:00:00Z", "heightCm": 190},
            {"birthDate": None, "logReminderTime": "07:45"},
            {"weightKg": 81},
            {},
        ):
            with self.subTest(body=body):
                await self.pair("PUT", "/profile", body)
        for body in ({"birthDate": "2020-01-01"}, {"birthDate": "1900-01-01"}, {"birthDate": "20000101"}, {"birthDate": "2026"},
                     {"goal": "GAIN", "weeklyRateKg": 0.6}, {"activityLevel": None}, {"trackWater": None}, {"weightKg": None},
                     {"heightCm": 99}, {"heightCm": 170.25}, {"sex": "X"}, {"logReminderTime": "25:00"}, {"bogus": 1}):
            with self.subTest(body=body):
                await self.pair("PUT", "/profile", body)
        await self.pair("GET", "/profile")

    async def test_food_day_and_languages(self):
        await self.pair("PUT", "/profile", {"sex": "MALE", "birthDate": "1988-07-01", "heightCm": 178, "weightKg": 90, "goal": "LOSE"})
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "BREAKFAST", "catalogId": "manoushe-zaatar", "servings": 1.5})
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "LUNCH", "name": " Chicken bowl ", "serving": " 1 bowl ",
                                               "calories": 640, "protein": 42.5, "carbs": 60, "fat": 18.3, "servings": 0.5})
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "LUNCH", "name": "chicken BOWL", "calories": 600})
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "DINNER", "name": "AI plate", "calories": 900, "source": "ai"})
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "SNACK", "name": "Once", "calories": 80, "remember": False})
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "SNACK", "calories": None})
        foods = await self.pair("GET", "/foods")
        nest_item, py_item = (f["items"][0]["id"] for f in foods)
        # an empty catalogId and a null foodItemId fall through to a manual entry
        await self.pair("POST", "/food-logs", {"date": "2026-11-04", "meal": "DINNER", "foodItemId": None, "catalogId": "", "calories": 10})
        out = []
        for client, prefix, food_id in ((self.nest, "", nest_item), (self.py, "/api", py_item)):
            out.append(await client.post(f"{prefix}/wellness/food-logs", json={"date": "2026-11-04", "foodItemId": food_id, "servings": 2, "meal": "DINNER"}))
        self.assertParity(*out)
        day = tuple(r.json() for r in out)
        logs = [_log_ids(d) for d in day]
        for body in ({"servings": 3}, {"servings": 3}, {"calories": 777, "protein": None, "name": "  "}, {"meal": "SNACK", "date": "2026-11-03"},
                     {"meal": None, "date": None, "name": None}, {"calories": None}, {"servings": None}, {"date": "2026"}, {"carbs": 1.25}):
            with self.subTest(body=body):
                await self.pair("PATCH", (f"/food-logs/{logs[0][0]}", f"/food-logs/{logs[1][0]}"), body)
        for query in ("?date=2026-11-04", "?date=2026-11-03", "?date=2026-11-04T23:00:00Z", "?date=2026-02-30", "?date=nope", "?bogus=1", ""):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/wellness/day{query}"))
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        self.assertParity(*await self.both("GET", "/wellness/day?date=2026-11-04"))
        self.assertParity(*await self.both("GET", "/wellness/activities"))
        for q in ("", "?q=", "?q=%20", "?q=hum", "?q=حمص", "?q=chicken", "?q=" + "x" * 101, "?q=a&q=b", "?x=1"):
            with self.subTest(q=q):
                self.assertParity(*await self.both("GET", f"/wellness/foods/search{q}"))
        await self.pair("DELETE", (f"/food-logs/{logs[0][1]}", f"/food-logs/{logs[1][1]}"))
        await self.pair("DELETE", (f"/food-logs/{logs[0][1]}", f"/food-logs/{logs[1][1]}"))
        for body in ({}, {"name": "x"}, {"catalogId": "nope"}, {"calories": -1}, {"calories": 1.5}, {"servings": 0.123, "calories": 1},
                     {"meal": "BRUNCH", "calories": 1}, {"source": "catalog", "calories": 1}, {"date": "x", "calories": 1}, {"name": 5, "calories": 1}):
            with self.subTest(body=body):
                await self.pair("POST", "/food-logs", body)

    async def test_my_foods(self):
        created = await self.pair("POST", "/foods", {"name": " Labneh wrap ", "serving": " ", "calories": 320, "protein": 14, "isFavorite": True})
        await self.pair("POST", "/foods", {"name": "Apple", "calories": 95})
        ids = tuple(c["id"] for c in created)
        for body in ({"calories": 300, "fat": 9.5}, {}, {"serving": None, "isFavorite": False}, {"name": None}, {"isFavorite": None}, {"name": " "}):
            with self.subTest(body=body):
                await self.pair("PATCH", (f"/foods/{ids[0]}", f"/foods/{ids[1]}"), body)
        await self.pair("GET", "/foods")
        self.assertParity(*await self.both("GET", "/wellness/foods/search?q=LAB"))
        for body in ({}, {"name": "x"}, {"name": "x", "calories": 10001}, {"name": "x" * 121, "calories": 1}, {"name": "x", "calories": 1, "isFavorite": None}):
            with self.subTest(body=body):
                await self.pair("POST", "/foods", body)
        await self.pair("DELETE", (f"/foods/{ids[0]}", f"/foods/{ids[1]}"))
        await self.pair("DELETE", (f"/foods/{ids[0]}", f"/foods/{ids[1]}"))
        await self.pair("PATCH", "/foods/cnosuchfoodxxxxxxxxxxxxxx", {"calories": 1})

    async def test_exercise_water_weight_history(self):
        await self.pair("POST", "/exercise", {"date": "2026-11-04", "activity": "cycling", "durationMin": 45})
        await self.pair("PUT", "/weight", {"date": "2026-10-01", "weightKg": 88.4, "note": " start "})
        await self.pair("PUT", "/weight", {"date": "2026-11-02", "weightKg": 86.95})
        await self.pair("PUT", "/weight", {"date": "2026-11-02", "weightKg": 86.9, "note": None})
        await self.pair("PUT", "/weight", {"weightKg": 86.5})
        await self.pair("POST", "/exercise", {"date": "2026-11-04", "activity": "made-up", "durationMin": 30, "name": " Climbing "})
        day = await self.pair("POST", "/exercise", {"date": "2026-11-04", "activity": "gym", "durationMin": 60, "calories": 410, "name": "  "})
        ex = tuple(d["exerciseLogs"][0]["id"] for d in day)
        await self.pair("DELETE", (f"/exercise/{ex[0]}", f"/exercise/{ex[1]}"))
        await self.pair("DELETE", (f"/exercise/{ex[0]}", f"/exercise/{ex[1]}"))
        for body in ({"date": "2026-11-04", "addMl": 750}, {"date": "2026-11-04", "addMl": -5000}, {"addMl": 5000}, {"addMl": 5001}, {"addMl": 1.5}, {}):
            with self.subTest(body=body):
                await self.pair("POST", "/water", body)
        weights = await self.pair("GET", "/weight")
        for query in ("?days=7", "?days=365", "?days=6", "?days=abc", "?days=", "?bogus=1"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/wellness/weight{query}"))
        await self.pair("PUT", "/profile", {"targetWeightKg": 80, "goal": "LOSE"})
        w = tuple(x["items"][0]["id"] for x in weights)
        await self.pair("DELETE", (f"/weight/{w[0]}", f"/weight/{w[1]}"))
        await self.pair("DELETE", (f"/weight/{w[0]}", f"/weight/{w[1]}"))
        for body in ({}, {"weightKg": 24}, {"weightKg": 80.123}, {"weightKg": 80, "note": "x" * 201}, {"weightKg": "80"}):
            with self.subTest(body=body):
                await self.pair("PUT", "/weight", body)
        for body in ({}, {"activity": "run", "durationMin": 0}, {"activity": "x" * 41, "durationMin": 10}, {"activity": "run", "durationMin": 10, "calories": 5001}):
            with self.subTest(body=body):
                await self.pair("POST", "/exercise", body)
        for query in ("", "?days=7", "?days=365", "?days=366", "?days=7.5"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/wellness/history{query}"))

    async def test_history_with_recent_days(self):
        await self.pair("PUT", "/profile", {"sex": "MALE", "birthDate": "1990-01-01", "heightCm": 175, "weightKg": 80, "addExerciseCalories": False})
        from app.os.common import dates

        today = dates.today_date_only("UTC")
        for back, calories in ((0, 2600), (1, 1800), (2, 2100), (5, 1500)):
            day = dates.format_date_only(dates.add_days_date_only(today, -back))
            await self.pair("POST", "/food-logs", {"date": day, "calories": calories, "protein": 50.5, "remember": False})
            await self.pair("POST", "/exercise", {"date": day, "activity": "walking", "durationMin": 40})
            await self.pair("POST", "/water", {"date": day, "addMl": 1200})
        for query in ("?days=7", "?days=30"):
            with self.subTest(query=query):
                self.assertParity(*await self.both("GET", f"/wellness/history{query}"))


if __name__ == "__main__":
    unittest.main()
