"""Calorie maths — pure (port of vournalOS health/health.calc.ts), tested by
tests/vournal_os/test_health_math.py (its spec's cases). Resting burn: Mifflin–St Jeor;
daily burn: resting × activity factor; 1 kg of body weight ≈ 7700 kcal."""

import math

from app.os.common.jsparse import js_number

ACTIVITY_FACTORS = {"SEDENTARY": 1.2, "LIGHT": 1.375, "MODERATE": 1.55, "ACTIVE": 1.725, "VERY_ACTIVE": 1.9}
KCAL_PER_KG = 7700
CALORIE_FLOOR = {"MALE": 1500, "FEMALE": 1200, "UNKNOWN": 1350}  # lowest target suggested without an override


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _round10(n: float) -> int:
    return _js_round(n / 10) * 10


def _round1(n: float) -> float:
    return _js_round(n * 10) / 10


def _parts(text: str) -> list[float]:
    """split("-").map(Number), padded with NaN as missing array items read."""
    parts = [js_number(x) for x in text.split("-")]
    return (parts + [math.nan] * 3)[:3]


def age_on(birth_date: str, today: str) -> int | float:
    """Whole years on today ("YYYY-MM-DD"); parts are read as JavaScript read them."""
    by, bm, bd = _parts(birth_date)
    ty, tm, td = _parts(today)
    age = ty - by - (1 if tm < bm or (tm == bm and td < bd) else 0)
    return int(age) if math.isfinite(age) else age


def resting_calories(sex: str | None, weight_kg: float, height_cm: float, age: int) -> int:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    # no sex given: halfway between the two formulas
    return _js_round(base + (5 if sex == "MALE" else -161 if sex == "FEMALE" else -78))


def compute_targets(p: dict) -> dict:
    missing = [k for k, v in (("weight", p["weightKg"]), ("height", p["heightCm"])) if not v]
    if p["age"] is None:
        missing.append("age")
    floor = CALORIE_FLOOR[p["sex"] or "UNKNOWN"]
    weight = p["weightKg"]
    if p["waterGoalMl"] is not None:
        water = p["waterGoalMl"]
    else:
        water = min(4000, max(1500, _js_round(weight * 35 / 50) * 50)) if weight else 2000
    rate = min(1, max(0, p["weeklyRateKg"]))
    daily_delta = 0 if p["goal"] == "MAINTAIN" else _js_round((-1 if p["goal"] == "LOSE" else 1) * rate * KCAL_PER_KG / 7)
    resting = maintenance = None
    calories = p["calorieTargetOverride"]
    floored = False
    if not missing:
        resting = resting_calories(p["sex"], weight, p["heightCm"], p["age"])
        maintenance = _round10(resting * ACTIVITY_FACTORS[p["activityLevel"]])
        if calories is None:
            raw = maintenance + daily_delta
            floored = raw < floor
            calories = _round10(max(floor, raw))
    protein = carbs = fat = None
    if calories is not None:
        # protein by body weight (more while losing or building), fat 30% of calories, carbs the rest
        protein = _js_round(weight * (1.4 if p["goal"] == "MAINTAIN" else 1.8)) if weight else _js_round(calories * 0.2 / 4)
        fat = _js_round(calories * 0.3 / 9)
        carbs = max(0, _js_round((calories - protein * 4 - fat * 9) / 4))
    return {"missing": missing, "resting": resting, "maintenance": maintenance, "calories": calories, "dailyDelta": daily_delta,
            "floored": floored, "floor": floor, "protein": protein, "carbs": carbs, "fat": fat, "waterMl": water}


def exercise_calories(met: float, weight_kg: float, minutes: float) -> int:
    """MET × kg × hours."""
    return _js_round(met * weight_kg * (minutes / 60))


def bmi(weight_kg: float, height_cm: float) -> float:
    return _round1(weight_kg / (height_cm / 100) ** 2)


def weeks_to_goal(current: float, target: float | None, rate: float) -> int | None:
    if target is None or rate <= 0:
        return None
    diff = abs(current - target)
    return 0 if diff < 0.05 else math.ceil(diff / rate)


def sum_food(logs: list[dict]) -> dict:
    t = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    for log in logs:
        t = {"calories": t["calories"] + log["calories"], "protein": t["protein"] + (log["protein"] or 0),
             "carbs": t["carbs"] + (log["carbs"] or 0), "fat": t["fat"] + (log["fat"] or 0)}
    return {"calories": _js_round(t["calories"]), "protein": _round1(t["protein"]), "carbs": _round1(t["carbs"]), "fat": _round1(t["fat"])}
