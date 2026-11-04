"""/api/wellness (port of vournalOS HealthService and its WellnessController): the
calorie target from the profile, food / exercise / water / weight logs, and the day's
balance. Everything optional is behind a toggle on the profile.

Bodies are checked rule by rule as class-validator did, so a wrong type gets every
message NestJS gave. POST /estimate (the AI food estimate) comes with the AI section.
"""

from datetime import datetime
from typing import ClassVar

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, model_validator

from app.db import db
from app.os import finance_utils as fu
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.jsparse import _JS_SPACE
from app.os.common.query import _query_int, unknown_params
from app.os.foods_catalog import ACTIVITIES, activity_met, catalog_food, search_catalog
from app.os.health_calc import _js_round, _round1, age_on, bmi, compute_targets, exercise_calories, sum_food, weeks_to_goal
from app.os.profile import ensure_os_profile
from app.os.transactions import as_decimal

router = nest_router("/wellness")

MEALS = ("BREAKFAST", "LUNCH", "DINNER", "SNACK")
SEXES = ("MALE", "FEMALE")
ACTIVITY_LEVELS = ("SEDENTARY", "LIGHT", "MODERATE", "ACTIVE", "VERY_ACTIVE")
GOALS = ("LOSE", "MAINTAIN", "GAIN")
FALLBACK_WEIGHT_KG = 70  # exercise calories until the user logs a weight

_LOG_FIELDS = ("id", "date", "meal", "name", "servings", "serving", "calories", "protein", "carbs", "fat", "foodItemId", "catalogId", "source", "createdAt")
_ITEM_FIELDS = ("id", "name", "serving", "calories", "protein", "carbs", "fat", "isFavorite", "useCount", "lastUsedAt")
_MACROS = ("protein", "carbs", "fat")


def _num(value) -> float | None:
    return None if value is None else float(value)


def _trim(value):
    return value.strip(_JS_SPACE) if isinstance(value, str) else value


# ----- bodies ------------------------------------------------------------------------
# (field, optional, trimmed, rule): IsOptional skips null and absent; a required field
# runs its rules on whatever is there, absent included.


def _number(minimum, maximum, places=None):
    return lambda v, f: validators.number_rules(v, f, minimum, maximum, places)


def _text(maximum, minimum=None):
    return lambda v, f: validators.text_rules(v, f, maximum, minimum)


def _choice(choices):
    return lambda v, f: validators.choice_rules(v, f, choices)


def _date(v, f):
    return [] if isinstance(v, str) and dates.is_iso_date_string(v) else [f"{f} must be a valid ISO 8601 date string"]


def _hhmm(v, f):
    return [] if isinstance(v, str) and validators.HHMM.match(v) else [f"{f} must be a time like 08:30"]


_MACRO_RULES = tuple((m, True, False, _number(0, 1000, 1)) for m in _MACROS)


class _Body(BaseModel):
    """Fields are plain values; RULES checks them before anything else runs."""

    model_config = ConfigDict(extra="forbid")
    RULES: ClassVar[tuple] = ()

    @model_validator(mode="before")
    @classmethod
    def _check(cls, data):
        if not isinstance(data, dict):
            return data
        names = [r[0] for r in cls.RULES]
        problems = [f"property {k} should not exist" for k in data if k not in names]
        data = {k: _trim(v) if any(r[0] == k and r[2] for r in cls.RULES) else v for k, v in data.items()}
        for field, optional, _trimmed, rule in cls.RULES:
            value = data.get(field)
            if optional and value is None:
                continue
            problems += rule(value, field)
        if problems:
            raise ValueError("\n".join(problems))
        return data

    def sent(self, field: str) -> bool:
        return field in self.model_fields_set


class ProfileBody(_Body):
    RULES = (
        ("sex", True, False, _choice(SEXES)),
        ("birthDate", True, False, _date),
        ("heightCm", True, False, _number(100, 250, 1)),
        ("weightKg", True, False, _number(25, 350, 2)),
        ("activityLevel", True, False, _choice(ACTIVITY_LEVELS)),
        ("goal", True, False, _choice(GOALS)),
        ("targetWeightKg", True, False, _number(25, 350, 1)),
        ("weeklyRateKg", True, False, _number(0.1, 1, 2)),
        ("calorieTargetOverride", True, False, _number(800, 6000)),
        *((flag, True, False, validators.bool_rules) for flag in (
            "trackProtein", "trackCarbs", "trackFat", "trackWater", "trackExercise", "trackWeight", "addExerciseCalories")),
        ("waterGoalMl", True, False, _number(500, 8000)),
        ("logReminderTime", True, False, _hhmm),
    )
    sex: object = None
    birthDate: object = None
    heightCm: object = None
    weightKg: object = None
    activityLevel: object = None
    goal: object = None
    targetWeightKg: object = None
    weeklyRateKg: object = None
    calorieTargetOverride: object = None
    trackProtein: object = None
    trackCarbs: object = None
    trackFat: object = None
    trackWater: object = None
    trackExercise: object = None
    trackWeight: object = None
    addExerciseCalories: object = None
    waterGoalMl: object = None
    logReminderTime: object = None


class _MacroBody(_Body):
    protein: object = None
    carbs: object = None
    fat: object = None


class FoodLogBody(_MacroBody):
    RULES = (
        ("date", True, False, _date),
        ("meal", True, False, _choice(MEALS)),
        ("catalogId", True, False, validators.string_rules),
        ("foodItemId", True, False, validators.string_rules),
        ("name", True, True, _text(120)),
        ("serving", True, True, _text(60)),
        ("calories", True, False, _number(0, 10000)),
        ("servings", True, False, _number(0.1, 50, 2)),
        ("remember", True, False, validators.bool_rules),
        ("source", True, False, _choice(("manual", "ai"))),
        *_MACRO_RULES,
    )
    date: object = None
    meal: object = None
    catalogId: object = None
    foodItemId: object = None
    name: object = None
    serving: object = None
    calories: object = None
    servings: object = None
    remember: object = None
    source: object = None


class FoodLogUpdateBody(_MacroBody):
    RULES = (
        ("meal", True, False, _choice(MEALS)),
        ("date", True, False, _date),
        ("servings", True, False, _number(0.1, 50, 2)),
        ("calories", True, False, _number(0, 10000)),
        ("name", True, True, _text(120)),
        *_MACRO_RULES,
    )
    meal: object = None
    date: object = None
    servings: object = None
    calories: object = None
    name: object = None


class FoodItemBody(_MacroBody):
    RULES = (
        ("name", False, True, _text(120, 1)),
        ("serving", True, True, _text(60)),
        ("calories", False, False, _number(0, 10000)),
        ("isFavorite", True, False, validators.bool_rules),
        *_MACRO_RULES,
    )
    name: object = None
    serving: object = None
    calories: object = None
    isFavorite: object = None


class FoodItemUpdateBody(FoodItemBody):
    RULES = (
        ("name", True, True, _text(120, 1)),
        ("serving", True, True, _text(60)),
        ("calories", True, False, _number(0, 10000)),
        ("isFavorite", True, False, validators.bool_rules),
        *_MACRO_RULES,
    )


class ExerciseBody(_Body):
    RULES = (
        ("date", True, False, _date),
        ("activity", False, False, _text(40)),
        ("name", True, True, _text(120)),
        ("durationMin", False, False, _number(1, 600)),
        ("calories", True, False, _number(0, 5000)),
    )
    date: object = None
    activity: object = None
    name: object = None
    durationMin: object = None
    calories: object = None


class WeightBody(_Body):
    RULES = (
        ("date", True, False, _date),
        ("weightKg", False, False, _number(25, 350, 2)),
        ("note", True, True, _text(200)),
    )
    date: object = None
    weightKg: object = None
    note: object = None


class WaterBody(_Body):
    RULES = (("date", True, False, _date), ("addMl", False, False, _number(-5000, 5000)))
    date: object = None
    addMl: object = None


# ----- helpers -----------------------------------------------------------------------


async def _context(user) -> dict:
    settings = await ensure_os_profile(user)
    tz = settings.timezone
    return {"tz": tz, "lang": to_lang(settings.language), "today": dates.format_date_only(dates.today_date_only(tz))}


def _day_only(date, today: str) -> datetime:
    return dates.parse_date_only(date if date is not None else today) or dates.parse_date_only(today)


def _meal_for_now(tz: str) -> str:
    """Breakfast before 11:00, lunch until 16:00, dinner from 18:00 to 23:00, otherwise a snack."""
    h = dates._local(dates._now(), tz).hour
    return "BREAKFAST" if 5 <= h < 11 else "LUNCH" if 12 <= h < 16 else "DINNER" if 18 <= h < 23 else "SNACK"


def _activity_name(id: str, lang: str) -> str:
    a = next((x for x in ACTIVITIES if x[0] == id), None)
    return (a[2] if lang == "ar" else a[1]) if a else id


def _food_view(row, lang: str) -> dict:
    """Built-in foods are shown in the current language, whatever language they were logged in."""
    out = {f: getattr(row, f) for f in _LOG_FIELDS}
    if row.catalogId and (c := catalog_food(row.catalogId, lang)):
        out.update(name=c["name"], serving=c["serving"])
    out.update(date=dates.format_date_only(row.date), servings=float(row.servings), **{m: _num(getattr(row, m)) for m in _MACROS})
    return out


def _item_view(row) -> dict:
    return {**{f: getattr(row, f) for f in _ITEM_FIELDS}, **{m: _num(getattr(row, m)) for m in _MACROS}}


def _profile_view(row, latest, today: str) -> dict:
    p = {
        "sex": row.sex if row else None,
        "birthDate": dates.format_date_only(row.birthDate) if row and row.birthDate else None,
        "heightCm": _num(row.heightCm) if row else None,
        "activityLevel": row.activityLevel if row else "LIGHT",
        "goal": row.goal if row else "MAINTAIN",
        "targetWeightKg": _num(row.targetWeightKg) if row else None,
        "weeklyRateKg": float(row.weeklyRateKg) if row else 0.5,
        "calorieTargetOverride": row.calorieTargetOverride if row else None,
        "trackProtein": row.trackProtein if row else False,
        "trackCarbs": row.trackCarbs if row else False,
        "trackFat": row.trackFat if row else False,
        "trackWater": row.trackWater if row else True,
        "trackExercise": row.trackExercise if row else True,
        "trackWeight": row.trackWeight if row else True,
        "addExerciseCalories": row.addExerciseCalories if row else True,
        "waterGoalMl": row.waterGoalMl if row else None,
        "logReminderTime": row.logReminderTime if row else "20:00",
    }
    weight = float(latest.weightKg) if latest else None
    age = age_on(p["birthDate"], today) if p["birthDate"] else None
    targets = compute_targets({
        "sex": p["sex"], "weightKg": weight, "heightCm": p["heightCm"], "age": age, "activityLevel": p["activityLevel"],
        "goal": p["goal"], "weeklyRateKg": p["weeklyRateKg"], "calorieTargetOverride": p["calorieTargetOverride"],
        "waterGoalMl": p["waterGoalMl"],
    })
    return {
        "exists": row is not None,
        "profile": p,
        "currentWeightKg": weight,
        "weightDate": dates.format_date_only(latest.date) if latest else None,
        "age": age,
        "bmi": bmi(weight, p["heightCm"]) if weight and p["heightCm"] else None,
        "targets": targets,
        "weeksToGoal": weeks_to_goal(weight, p["targetWeightKg"], p["weeklyRateKg"]) if weight is not None and p["goal"] != "MAINTAIN" else None,
    }


async def _profile(user, ctx: dict | None = None) -> dict:
    ctx = ctx or await _context(user)
    row = await db.healthprofile.find_unique(where={"userId": user.id})
    latest = await db.weightlog.find_first(where={"userId": user.id}, order={"date": "desc"})
    return _profile_view(row, latest, ctx["today"])


async def _day(user, date=None) -> dict:
    ctx = await _context(user)
    day = _day_only(date, ctx["today"])
    prof = await _profile(user, ctx)
    foods = await db.foodlog.find_many(where={"userId": user.id, "date": day}, order={"createdAt": "asc"})
    exercise = await db.exerciselog.find_many(where={"userId": user.id, "date": day}, order={"createdAt": "asc"})
    water = await db.waterlog.find_unique(where={"userId_date": {"userId": user.id, "date": day}})
    items = [_food_view(f, ctx["lang"]) for f in foods]
    totals = sum_food(items)
    p = prof["profile"]
    exercise_cals = sum(e.calories for e in exercise)
    counted = exercise_cals if p["trackExercise"] and p["addExerciseCalories"] else 0
    target = prof["targets"]["calories"]
    allowed = None if target is None else target + counted
    meals = []
    for meal in MEALS:
        listed = [i for i in items if i["meal"] == meal]
        meals.append({"meal": meal, "calories": sum(i["calories"] for i in listed), "items": listed})
    return {
        "date": dates.format_date_only(day),
        "isToday": day == dates.parse_date_only(ctx["today"]),
        "profile": prof,
        "target": target,
        "allowed": allowed,
        "eaten": totals["calories"],
        "exercise": exercise_cals,
        "remaining": None if allowed is None else allowed - totals["calories"],
        "totals": totals,
        "meals": meals,
        "exerciseLogs": [
            {"id": e.id, "activity": e.activity, "name": e.name if e.name is not None else _activity_name(e.activity, ctx["lang"]),
             "durationMin": e.durationMin, "calories": e.calories}
            for e in exercise
        ],
        "water": {"ml": water.ml if water else 0, "goalMl": prof["targets"]["waterMl"]},
    }


async def _save_weight(user_id: str, weight_kg, today: str, date=None, note=None, note_sent: bool = False) -> None:
    if weight_kg is None:
        raise invalid_data()  # Prisma refused null on the required column
    day = _day_only(date, today)
    update = {"weightKg": as_decimal(weight_kg), **({"note": note} if note_sent else {})}
    await db.weightlog.upsert(
        where={"userId_date": {"userId": user_id, "date": day}},
        data={"create": {"userId": user_id, "date": day, "weightKg": as_decimal(weight_kg), "note": note}, "update": update},
    )
    await log_activity(user_id, "weight.logged", weightKg=weight_kg)


def _js_int(value) -> int:
    """A validated JSON integer (1.0 included) as int."""
    return int(value)


# ----- profile and the day -------------------------------------------------------------

_PROFILE_DECIMALS = ("heightCm", "targetWeightKg", "weeklyRateKg")
_PROFILE_REQUIRED = ("activityLevel", "goal", "weeklyRateKg", "trackProtein", "trackCarbs", "trackFat", "trackWater",
                     "trackExercise", "trackWeight", "addExerciseCalories")


@router.get("/profile")
async def profile(user=Depends(plus_user)):
    return await _profile(user)


@router.put("/profile")
async def update_profile(body: ProfileBody, user=Depends(plus_user)):
    ctx = await _context(user)
    if body.birthDate:
        age = age_on(body.birthDate[:10], ctx["today"])
        if age < 13 or age > 110:
            raise nest_error(400, "Check the birth date (13 to 110 years old)")
    if body.goal == "GAIN" and body.weeklyRateKg is not None and body.weeklyRateKg > 0.5:
        raise nest_error(400, "Gaining more than 0.5 kg a week is not recommended")
    data = {}
    for field in body.model_fields_set - {"weightKg", "birthDate"}:
        value = getattr(body, field)
        if value is None and field in _PROFILE_REQUIRED:
            raise invalid_data()
        if field in _PROFILE_DECIMALS and value is not None:
            value = as_decimal(value)
        elif field in ("calorieTargetOverride", "waterGoalMl") and value is not None:
            value = _js_int(value)
        data[field] = value
    if body.sent("birthDate"):
        data["birthDate"] = dates.parse_date_only(body.birthDate)
    await db.healthprofile.upsert(where={"userId": user.id}, data={"create": {**data, "userId": user.id}, "update": data})
    if body.sent("weightKg"):
        await _save_weight(user.id, body.weightKg, ctx["today"])
    return await _profile(user)


@router.get("/day")
async def day(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("date",))
    date = request.query_params.get("date")
    if date is not None and not dates.is_iso_date_string(date):
        errors.append("date must be a valid ISO 8601 date string")
    if errors:
        raise nest_error(400, errors)
    return await _day(user, date)


def _days_query(request: Request, default: int) -> int:
    errors = unknown_params(request, ("days",))
    days = _query_int(request, "days", errors, maximum=365, minimum=7)
    if errors:
        raise nest_error(400, errors)
    return default if days is None else days


@router.get("/history")
async def history(request: Request, user=Depends(plus_user)):
    """Daily calories in / out for the last `days` days, plus averages over the days that have food logged."""
    days = _days_query(request, 30)
    ctx = await _context(user)
    today = dates.parse_date_only(ctx["today"])
    start = dates.add_days_date_only(today, -(days - 1))
    where = {"userId": user.id, "date": {"gte": start, "lte": today}}
    food = await db.foodlog.group_by(by=["date"], where=where, sum={"calories": True, "protein": True})
    exercise = await db.exerciselog.group_by(by=["date"], where=where, sum={"calories": True})
    water = await db.waterlog.find_many(where=where)
    prof = await _profile(user, ctx)

    def key(value) -> str:
        return dates.format_date_only(fu.group_instant(value))

    eaten = {key(f["date"]): int((f.get("_sum") or {}).get("calories") or 0) for f in food}
    burned = {key(e["date"]): int((e.get("_sum") or {}).get("calories") or 0) for e in exercise}
    drank = {dates.format_date_only(w.date): w.ml for w in water}
    series = []
    for i in range(days):
        d = dates.format_date_only(dates.add_days_date_only(start, i))
        series.append({"date": d, "eaten": eaten.get(d, 0), "exercise": burned.get(d, 0), "waterMl": drank.get(d, 0), "logged": d in eaten})
    logged = [s for s in series if s["logged"]]

    def avg(xs: list[int]) -> int | None:
        return _js_round(sum(xs) / len(xs)) if xs else None

    # consecutive days with food logged, ending today (or yesterday when today is still empty)
    streak = 0
    for i in range(len(series) - 1, -1, -1):
        if series[i]["logged"]:
            streak += 1
        elif i == len(series) - 1:
            continue
        else:
            break
    target, maintenance = prof["targets"]["calories"], prof["targets"]["maintenance"]
    avg_eaten = avg([s["eaten"] for s in logged])
    add_exercise = prof["profile"]["addExerciseCalories"]
    return {
        "days": series,
        "target": target,
        "maintenance": maintenance,
        "averageEaten": avg_eaten,
        "averageExercise": avg([s["exercise"] for s in logged]),
        "loggedDays": len(logged),
        "streak": streak,
        "onTargetDays": 0 if target is None else sum(1 for s in logged if s["eaten"] <= target + (s["exercise"] if add_exercise else 0)),
        # at the average intake, the expected change per week (kg; negative = losing)
        "projectedWeeklyChangeKg": _round1(((avg_eaten - maintenance) * 7) / 7700) if avg_eaten is not None and maintenance is not None else None,
    }


# ----- food ----------------------------------------------------------------------------


@router.get("/foods/search")
async def search_foods(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("q",))
    values = request.query_params.getlist("q")
    if values:
        errors += validators.text_rules(values[0] if len(values) == 1 else values, "q", 100)
    if errors:
        raise nest_error(400, errors)
    ctx = await _context(user)
    query = _trim(values[0]) if values else ""
    if not query:
        recent = await db.fooditem.find_many(where={"userId": user.id, "lastUsedAt": {"not": None}}, order={"lastUsedAt": "desc"}, take=12)
        favorites = await db.fooditem.find_many(where={"userId": user.id, "isFavorite": True}, order={"name": "asc"}, take=30)
        recent_catalog = await db.foodlog.find_many(
            where={"userId": user.id, "catalogId": {"not": None}}, order={"createdAt": "desc"}, distinct=["catalogId"], take=12
        )
        catalog = [catalog_food(r.catalogId, ctx["lang"]) for r in recent_catalog]
        return {
            "mine": [_item_view(f) for f in [*favorites, *(r for r in recent if not r.isFavorite)]],
            "catalog": [c for c in catalog if c],
        }
    mine = await db.fooditem.find_many(
        where={"userId": user.id, "name": {"contains": query, "mode": "insensitive"}},
        order=[{"isFavorite": "desc"}, {"useCount": "desc"}],
        take=15,
    )
    return {"mine": [_item_view(f) for f in mine], "catalog": search_catalog(query, ctx["lang"], 25)}


@router.get("/foods")
async def my_foods(user=Depends(plus_user)):
    rows = await db.fooditem.find_many(where={"userId": user.id}, order=[{"isFavorite": "desc"}, {"name": "asc"}], take=500)
    return {"items": [_item_view(f) for f in rows], "total": len(rows)}


def _item_data(body: FoodItemBody) -> dict:
    data = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field in ("name", "calories", "isFavorite"):
            raise invalid_data()
        data[field] = as_decimal(value) if field in _MACROS and value is not None else _js_int(value) if field == "calories" else value
    return data


@router.post("/foods", status_code=201)
async def create_food_item(body: FoodItemBody, user=Depends(plus_user)):
    row = await db.fooditem.create(data={"userId": user.id, **_item_data(body)})
    return _item_view(row)


async def _owned_item(user_id: str, id: str):
    row = await db.fooditem.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Food not found")
    return row


@router.patch("/foods/{id}")
async def update_food_item(id: str, body: FoodItemUpdateBody, user=Depends(plus_user)):
    row = await _owned_item(user.id, id)
    # Prisma ran the update (and moved updatedAt) even with nothing to change
    updated = await db.fooditem.update(where={"id": row.id}, data=_item_data(body) or {"name": row.name})
    return _item_view(updated)


@router.delete("/foods/{id}")
async def remove_food_item(id: str, user=Depends(plus_user)):
    """Past logs keep their numbers (they were copied when logged)."""
    row = await _owned_item(user.id, id)
    await db.fooditem.delete(where={"id": row.id})
    return {"ok": True}


@router.post("/food-logs", status_code=201)
async def add_food(body: FoodLogBody, user=Depends(plus_user)):
    ctx = await _context(user)
    date = _day_only(body.date, ctx["today"])
    servings = body.servings if body.servings is not None else 1
    meal = body.meal or _meal_for_now(ctx["tz"])
    link: dict
    if body.catalogId:
        c = catalog_food(body.catalogId, ctx["lang"])
        if not c:
            raise nest_error(404, "Food not found")
        base = {k: c[k] for k in ("name", "serving", "calories", "protein", "carbs", "fat")}
        link = {"catalogId": c["id"], "source": "catalog"}
    elif body.foodItemId:
        f = await db.fooditem.find_first(where={"id": body.foodItemId, "userId": user.id})
        if f is None:
            raise nest_error(404, "Food not found")
        base = {"name": f.name, "serving": f.serving, "calories": f.calories, **{m: _num(getattr(f, m)) for m in _MACROS}}
        link = {"foodItemId": f.id, "source": "food"}
    else:
        if not body.sent("calories"):
            raise nest_error(400, "Enter the calories, or pick a food")
        base = {"name": _trim(body.name) or None, "serving": _trim(body.serving) or None, "calories": body.calories,
                **{m: getattr(body, m) for m in _MACROS}}
        link = {"source": body.source or "manual"}
        # remember named foods so they can be picked next time (AI items are totals, not servings: not remembered)
        if base["name"] and body.remember is not False and body.source != "ai":
            if base["calories"] is None:
                raise invalid_data()  # Prisma refused null calories on the food
            fields = {"calories": _js_int(base["calories"]), "serving": base["serving"],
                      **{m: None if base[m] is None else as_decimal(base[m]) for m in _MACROS}}
            existing = await db.fooditem.find_first(where={"userId": user.id, "name": {"equals": base["name"], "mode": "insensitive"}})
            if existing:
                item = await db.fooditem.update(where={"id": existing.id}, data=fields)
            else:
                item = await db.fooditem.create(data={"userId": user.id, "name": base["name"], **fields})
            link = {"foodItemId": item.id, "source": "manual"}

    def scale(v):
        return None if v is None else as_decimal(_round1(v * servings))

    data = {
        "userId": user.id, "date": date, "meal": meal, "name": base["name"], "servings": as_decimal(servings), "serving": base["serving"],
        "calories": _js_round((base["calories"] or 0) * servings),  # JavaScript's null × n is 0
        **{m: scale(base[m]) for m in _MACROS}, "source": link["source"],
    }
    for k in ("catalogId", "foodItemId"):
        if link.get(k):
            data[k] = link[k]
    log = await db.foodlog.create(data=data)
    if link.get("foodItemId"):
        await db.fooditem.update(where={"id": link["foodItemId"]}, data={"useCount": {"increment": 1}, "lastUsedAt": dates._now()})
    await log_activity(user.id, "food.logged", entity_id=log.id, title=base["name"], calories=log.calories)
    return await _day(user, dates.format_date_only(date))


async def _owned_log(user_id: str, id: str):
    row = await db.foodlog.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Food log not found")
    return row


@router.patch("/food-logs/{id}")
async def update_food(id: str, body: FoodLogUpdateBody, user=Depends(plus_user)):
    log = await _owned_log(user.id, id)
    data: dict = {}
    if body.meal:
        data["meal"] = body.meal
    if body.date:
        data["date"] = dates.parse_date_only(body.date)
        if data["date"] is None:  # "2026" is an ISO date, but not a day: Prisma refused null
            raise invalid_data()
    if body.sent("name"):
        data["name"] = _trim(body.name) or None
    if body.sent("servings") and body.servings != float(log.servings):
        if body.servings is None:
            raise invalid_data()
        k = body.servings / float(log.servings)
        data.update(servings=as_decimal(body.servings), calories=_js_round(log.calories * k),
                    **{m: None if getattr(log, m) is None else as_decimal(_round1(float(getattr(log, m)) * k)) for m in _MACROS})
    if body.sent("calories"):
        if body.calories is None:
            raise invalid_data()
        data["calories"] = _js_int(body.calories)
    for m in _MACROS:
        if body.sent(m):
            data[m] = None if getattr(body, m) is None else as_decimal(getattr(body, m))
    updated = await db.foodlog.update(where={"id": log.id}, data=data) if data else log
    return await _day(user, dates.format_date_only(updated.date))


@router.delete("/food-logs/{id}")
async def remove_food(id: str, user=Depends(plus_user)):
    log = await _owned_log(user.id, id)
    await db.foodlog.delete(where={"id": log.id})
    return await _day(user, dates.format_date_only(log.date))


# ----- exercise, water, weight -----------------------------------------------------------


@router.get("/activities")
async def activities(user=Depends(plus_user)):
    lang = to_lang((await ensure_os_profile(user)).language)
    return {"items": [{"id": id, "name": ar if lang == "ar" else en, "met": met} for id, en, ar, met in ACTIVITIES]}


@router.post("/exercise", status_code=201)
async def add_exercise(body: ExerciseBody, user=Depends(plus_user)):
    ctx = await _context(user)
    date = _day_only(body.date, ctx["today"])
    latest = await db.weightlog.find_first(where={"userId": user.id}, order={"date": "desc"})
    weight = float(latest.weightKg) if latest else FALLBACK_WEIGHT_KG
    calories = _js_int(body.calories) if body.calories is not None else exercise_calories(activity_met(body.activity), weight, body.durationMin)
    row = await db.exerciselog.create(data={
        "userId": user.id, "date": date, "activity": body.activity, "name": _trim(body.name) or None,
        "durationMin": _js_int(body.durationMin), "calories": calories,
    })
    await log_activity(user.id, "exercise.logged", entity_id=row.id, title=row.name if row.name is not None else body.activity, calories=calories)
    return await _day(user, dates.format_date_only(date))


@router.delete("/exercise/{id}")
async def remove_exercise(id: str, user=Depends(plus_user)):
    row = await db.exerciselog.find_first(where={"id": id, "userId": user.id})
    if row is None:
        raise nest_error(404, "Exercise not found")
    await db.exerciselog.delete(where={"id": row.id})
    return await _day(user, dates.format_date_only(row.date))


@router.post("/water")
async def add_water(body: WaterBody, user=Depends(plus_user)):
    ctx = await _context(user)
    date = _day_only(body.date, ctx["today"])
    key = {"userId_date": {"userId": user.id, "date": date}}
    async with db.tx() as tx:
        cur = await tx.waterlog.find_unique(where=key)
        ml = max(0, min(20000, (cur.ml if cur else 0) + _js_int(body.addMl)))
        await tx.waterlog.upsert(where=key, data={"create": {"userId": user.id, "date": date, "ml": ml}, "update": {"ml": ml}})
    return await _day(user, dates.format_date_only(date))


async def _weights(user, days: int) -> dict:
    ctx = await _context(user)
    start = dates.add_days_date_only(dates.parse_date_only(ctx["today"]), -days)
    rows = await db.weightlog.find_many(where={"userId": user.id, "date": {"gte": start}}, order={"date": "asc"})
    first = await db.weightlog.find_first(where={"userId": user.id}, order={"date": "asc"})
    prof = await _profile(user, ctx)
    items = [{"id": r.id, "date": dates.format_date_only(r.date), "weightKg": float(r.weightKg), "note": r.note} for r in rows]
    current = prof["currentWeightKg"]
    begin = float(first.weightKg) if first else None
    target = prof["profile"]["targetWeightKg"]
    return {
        "items": items,
        "stats": {
            "start": begin,
            "startDate": dates.format_date_only(first.date) if first else None,
            "current": current,
            "change": _round1(current - begin) if current is not None and begin is not None else None,
            "target": target,
            "toGo": _round1(target - current) if current is not None and target is not None else None,
            "lowest": min(i["weightKg"] for i in items) if items else None,
        },
        "weeksToGoal": prof["weeksToGoal"],
    }


@router.get("/weight")
async def weights(request: Request, user=Depends(plus_user)):
    return await _weights(user, _days_query(request, 180))


@router.put("/weight")
async def log_weight(body: WeightBody, user=Depends(plus_user)):
    ctx = await _context(user)
    await _save_weight(user.id, body.weightKg, ctx["today"], body.date, body.note, body.sent("note"))
    return await _weights(user, 365)


@router.delete("/weight/{id}")
async def remove_weight(id: str, user=Depends(plus_user)):
    row = await db.weightlog.find_first(where={"id": id, "userId": user.id})
    if row is None:
        raise nest_error(404, "Weight entry not found")
    await db.weightlog.delete(where={"id": row.id})
    return await _weights(user, 365)
