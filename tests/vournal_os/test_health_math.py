"""Health maths and the foods catalogue, ported from vournalOS's health.calc.spec.ts and
foods.catalog.spec.ts, plus golden searches from its own searchCatalog (golden/foods.json)."""

import json
import pathlib
import unittest

from app.os.foods_catalog import CATALOG_SIZE, activity_met, catalog_food, search_catalog
from app.os.health_calc import age_on, bmi, compute_targets, exercise_calories, resting_calories, sum_food, weeks_to_goal

GOLDEN = json.loads((pathlib.Path(__file__).parent / "golden" / "foods.json").read_text())
BASE = {"sex": "MALE", "weightKg": 90, "heightCm": 180, "age": 30, "activityLevel": "LIGHT", "goal": "LOSE",
        "weeklyRateKg": 0.5, "calorieTargetOverride": None, "waterGoalMl": None}


def subset(d: dict, keys: dict) -> dict:
    return {k: d[k] for k in keys}


class HealthMath(unittest.TestCase):
    def test_age(self):
        self.assertEqual((age_on("1996-11-03", "2026-11-02"), age_on("1996-11-02", "2026-11-02")), (29, 30))

    def test_age_reads_parts_as_javascript_does(self):
        """A birth date the ISO check let through but without three dashed parts:
        "20000101" is one huge year, a missing day compares as NaN (never earlier)."""
        self.assertEqual(age_on("20000101", "2026-11-02"), 2026 - 20000101)
        self.assertEqual(age_on("2010-11", "2026-11-02"), 16)
        self.assertEqual(age_on("+2000-01-01", "2026-11-02"), 26)

    def test_mifflin_st_jeor(self):
        self.assertEqual(resting_calories("MALE", 90, 180, 30), 1880)
        self.assertEqual(resting_calories("FEMALE", 60, 165, 28), 1330)
        self.assertEqual(resting_calories(None, 90, 180, 30), 1797)

    def test_goal_to_daily_target(self):
        want = {"missing": [], "resting": 1880, "maintenance": 2590, "dailyDelta": -550, "calories": 2040, "floored": False}
        self.assertEqual(subset(compute_targets(BASE), want), want)
        self.assertEqual(compute_targets({**BASE, "goal": "MAINTAIN"})["calories"], 2590)
        self.assertEqual(compute_targets({**BASE, "goal": "GAIN", "weeklyRateKg": 0.25})["calories"], 2870)

    def test_safe_floor(self):
        t = compute_targets({**BASE, "sex": "FEMALE", "weightKg": 55, "heightCm": 158, "age": 40, "activityLevel": "SEDENTARY", "weeklyRateKg": 1})
        self.assertEqual(subset(t, {"calories": 0, "floored": 0, "floor": 0}), {"calories": 1200, "floored": True, "floor": 1200})
        t = compute_targets({**BASE, "calorieTargetOverride": 1100})
        self.assertEqual((t["calories"], t["floored"]), (1100, False))

    def test_macros_and_water(self):
        t = compute_targets(BASE)
        self.assertEqual((t["protein"], t["fat"], t["carbs"], t["waterMl"]), (162, 68, 195, 3150))
        self.assertEqual(compute_targets({**BASE, "waterGoalMl": 2500})["waterMl"], 2500)

    def test_missing(self):
        t = compute_targets({**BASE, "heightCm": None, "age": None})
        self.assertEqual((t["missing"], t["calories"], t["maintenance"]), (["height", "age"], None, None))
        self.assertEqual(compute_targets({**BASE, "weightKg": None, "heightCm": None, "age": None, "calorieTargetOverride": 1800})["calories"], 1800)

    def test_helpers(self):
        self.assertEqual(exercise_calories(3.5, 80, 30), 140)
        self.assertEqual(bmi(90, 180), 27.8)
        self.assertEqual((weeks_to_goal(90, 80, 0.5), weeks_to_goal(80, 80, 0.5), weeks_to_goal(90, None, 0.5)), (20, 0, None))
        logs = [{"calories": 280, "protein": 8, "carbs": 30, "fat": 14.5}, {"calories": 95, "protein": None, "carbs": 25, "fat": None}]
        self.assertEqual(sum_food(logs), {"calories": 375, "protein": 8, "carbs": 55, "fat": 14.5})


class Foods(unittest.TestCase):
    def test_unique_ids_and_sane_numbers(self):
        self.assertGreater(CATALOG_SIZE, 100)
        ids = {f["id"] for q in ("a", "e", "i", "o", "u", "ا", "ي", "ب") for f in search_catalog(q, "en", 500)}
        for id in ids:
            f = catalog_food(id, "en")
            self.assertGreaterEqual(f["calories"], 0)
            from_macros = f["protein"] * 4 + f["carbs"] * 4 + f["fat"] * 9
            if from_macros > 50 and id not in ("arak", "beer", "wine"):
                self.assertLess(abs(from_macros - f["calories"]) / f["calories"], 0.3, id)

    def test_finds_food_in_english_arabic_and_arabizi(self):
        self.assertEqual(search_catalog("manoushe", "en")[0]["id"], "manoushe-zaatar")
        self.assertEqual(search_catalog("man2oushe jebne", "en")[0]["id"], "manoushe-cheese")
        first = search_catalog("منقوشة", "ar")[0]
        self.assertEqual((first["name"], first["serving"]), ("منقوشة زعتر", "قطعة"))
        self.assertEqual(search_catalog("اكعكة", "ar"), [])
        self.assertEqual(search_catalog("شاورما دجاج", "ar")[0]["id"], "shawarma-chicken")
        self.assertEqual(search_catalog("banana", "ar")[0]["name"], "موزة")
        self.assertEqual(search_catalog("أفوكادو", "en")[0]["id"], "avocado")
        self.assertEqual(search_catalog("   ", "en"), [])

    def test_every_golden_search(self):
        for case in GOLDEN:
            with self.subTest(q=case["q"], lang=case["lang"]):
                got = search_catalog(case["q"], case["lang"], case.get("limit", 20))
                self.assertEqual([f["id"] for f in got], case["results"])

    def test_activity_mets(self):
        self.assertEqual((activity_met("running"), activity_met("nope")), (9.8, 4))


if __name__ == "__main__":
    unittest.main()
