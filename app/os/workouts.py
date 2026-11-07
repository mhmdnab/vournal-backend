"""/api/workouts — workout plans and the sessions logged against them (Health section).

- A plan has days (e.g. Push / Pull / Legs, each optionally pinned to a weekday) and each day
  has target exercises: sets × reps (text: "10", "8-12", "AMRAP") at a weight.
- A session is one workout on a day. Started from a plan day, it is pre-filled with one set row
  per target set; the user then logs what they actually did (reps, weight, done).
- Volume = Σ reps × weight over done sets. Estimated 1RM uses Epley: w × (1 + reps / 30).
- Exercises are either from the built-in catalog (exerciseId) or the user's own (name only).
"""

import re
from datetime import datetime, timezone

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.currency import as_decimal, round_money, to_number
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.query import unknown_params
from app.os.habit_streaks import day_num, day_str, week_start_of, weekday_of
from app.os.habits import _ctx, _dstr

router = nest_router("/workouts")

# [id, English, Arabic, muscle group]
CATALOG: list[tuple[str, str, str, str]] = [
    ("bench-press", "Bench press", "ضغط البنش", "chest"),
    ("incline-bench-press", "Incline bench press", "ضغط بنش مائل", "chest"),
    ("dumbbell-press", "Dumbbell press", "ضغط بالدمبل", "chest"),
    ("chest-fly", "Chest fly", "تفتيح صدر", "chest"),
    ("push-up", "Push-up", "ضغط", "chest"),
    ("dips", "Dips", "متوازي", "chest"),
    ("deadlift", "Deadlift", "رفعة ميتة", "back"),
    ("pull-up", "Pull-up", "عقلة", "back"),
    ("lat-pulldown", "Lat pulldown", "سحب أمامي", "back"),
    ("barbell-row", "Barbell row", "تجديف بالبار", "back"),
    ("dumbbell-row", "Dumbbell row", "تجديف بالدمبل", "back"),
    ("seated-cable-row", "Seated cable row", "تجديف بالكابل جالساً", "back"),
    ("squat", "Squat", "سكوات", "legs"),
    ("front-squat", "Front squat", "سكوات أمامي", "legs"),
    ("leg-press", "Leg press", "ضغط الأرجل", "legs"),
    ("lunge", "Lunge", "طعنات", "legs"),
    ("romanian-deadlift", "Romanian deadlift", "رفعة ميتة رومانية", "legs"),
    ("leg-curl", "Leg curl", "ثني الأرجل", "legs"),
    ("leg-extension", "Leg extension", "مد الأرجل", "legs"),
    ("calf-raise", "Calf raise", "رفع السمانة", "legs"),
    ("hip-thrust", "Hip thrust", "دفع الورك", "legs"),
    ("overhead-press", "Overhead press", "ضغط كتف", "shoulders"),
    ("dumbbell-shoulder-press", "Dumbbell shoulder press", "ضغط كتف بالدمبل", "shoulders"),
    ("lateral-raise", "Lateral raise", "رفرفة جانبي", "shoulders"),
    ("front-raise", "Front raise", "رفرفة أمامي", "shoulders"),
    ("face-pull", "Face pull", "سحب للوجه", "shoulders"),
    ("shrug", "Shrug", "ترابيس", "shoulders"),
    ("bicep-curl", "Bicep curl", "بايسبس", "arms"),
    ("hammer-curl", "Hammer curl", "بايسبس هامر", "arms"),
    ("tricep-pushdown", "Tricep pushdown", "ترايسبس بالكابل", "arms"),
    ("skull-crusher", "Skull crusher", "ترايسبس بالبار", "arms"),
    ("overhead-tricep-extension", "Overhead tricep extension", "ترايسبس خلف الرأس", "arms"),
    ("plank", "Plank", "بلانك", "core"),
    ("crunch", "Crunch", "طحن البطن", "core"),
    ("hanging-leg-raise", "Hanging leg raise", "رفع الأرجل معلّقاً", "core"),
    ("russian-twist", "Russian twist", "الالتواء الروسي", "core"),
    ("ab-wheel", "Ab wheel", "عجلة البطن", "core"),
    ("running", "Running", "جري", "cardio"),
    ("treadmill", "Treadmill", "مشاية", "cardio"),
    ("cycling", "Cycling", "دراجة", "cardio"),
    ("rowing-machine", "Rowing machine", "آلة التجديف", "cardio"),
    ("jump-rope", "Jump rope", "نط الحبل", "cardio"),
    ("burpee", "Burpee", "بيربي", "full-body"),
    ("kettlebell-swing", "Kettlebell swing", "مرجحة الكيتل بل", "full-body"),
    ("clean-and-press", "Clean and press", "كلين أند برس", "full-body"),
]
CATALOG_IDS = {c[0] for c in CATALOG}
MUSCLES = ("chest", "back", "legs", "shoulders", "arms", "core", "cardio", "full-body")

_PLAN_FIELDS = ("id", "name", "description", "isActive", "createdAt", "updatedAt")
_PLAN_INCLUDE = {"days": {"order_by": [{"sortOrder": "asc"}, {"id": "asc"}],
                          "include": {"exercises": {"order_by": [{"sortOrder": "asc"}, {"id": "asc"}]}}}}
_SESSION_FIELDS = ("id", "planId", "planDayId", "name", "durationMin", "notes", "completedAt", "createdAt", "updatedAt")
_SESSION_INCLUDE = {"sets": {"order_by": [{"sortOrder": "asc"}, {"id": "asc"}]}, "plan": True}
_LEADING_INT = re.compile(r"^\s*(\d{1,4})")


def catalog_view() -> list[dict]:
    return [{"id": i, "name": {"en": en, "ar": ar}, "muscle": m} for i, en, ar, m in CATALOG]


def _kg(value) -> float | None:
    return None if value is None else round_money(to_number(value))


# ----- views ---------------------------------------------------------------------------


def plan_view(row) -> dict:
    out = {f: getattr(row, f) for f in _PLAN_FIELDS}
    out["days"] = [
        {"id": d.id, "name": d.name, "weekday": d.weekday, "sortOrder": d.sortOrder,
         "exercises": [{"id": e.id, "exerciseId": e.exerciseId, "name": e.name, "sets": e.sets, "reps": e.reps,
                        "weightKg": _kg(e.weightKg), "restSec": e.restSec, "notes": e.notes, "sortOrder": e.sortOrder}
                       for e in (d.exercises or [])]}
        for d in (row.days or [])
    ]
    return out


def _volume(sets) -> float:
    return round_money(sum((s.reps or 0) * to_number(s.weightKg or 0) for s in sets if s.done))


def session_view(row) -> dict:
    out = {f: getattr(row, f) for f in _SESSION_FIELDS}
    sets = row.sets or []
    out.update(
        date=_dstr(row.date),
        planName=row.plan.name if row.plan else None,
        sets=[{"id": s.id, "exerciseId": s.exerciseId, "name": s.name, "reps": s.reps, "weightKg": _kg(s.weightKg),
               "done": s.done, "sortOrder": s.sortOrder} for s in sets],
        volumeKg=_volume(sets),
        setCount=len(sets),
        doneSets=sum(1 for s in sets if s.done),
        exerciseCount=len({(s.exerciseId or s.name.lower()) for s in sets}),
    )
    return out


# ----- bodies --------------------------------------------------------------------------


def _exercise_id(v):
    if v is not None and v not in CATALOG_IDS:
        raise ValueError("exerciseId must be a catalog exercise id")
    return v


class PlanExercise(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exerciseId: StrictStr | None = None
    name: StrictStr
    sets: StrictInt = Field(default=3, ge=1, le=20)
    reps: StrictStr | None = None
    weightKg: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1000)
    restSec: StrictInt | None = Field(default=None, ge=0, le=3600)
    notes: StrictStr | None = Field(default=None, max_length=500)

    @field_validator("exerciseId")
    @classmethod
    def _eid(cls, v):
        return _exercise_id(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("reps")
    @classmethod
    def _reps(cls, v):
        return validators.trimmed(v, "reps", 0, 20) or None


class PlanDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    weekday: StrictInt | None = Field(default=None, ge=0, le=6)
    exercises: list[PlanExercise] = Field(default_factory=list, max_length=40)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 60)


class _PlanFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=2000)
    isActive: StrictBool | None = None
    days: list[PlanDay] | None = Field(default=None, max_length=14)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)


class PlanCreateBody(_PlanFields):
    name: StrictStr


class PlanUpdateBody(_PlanFields):
    pass


class SessionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exerciseId: StrictStr | None = None
    name: StrictStr
    reps: StrictInt | None = Field(default=None, ge=0, le=1000)
    weightKg: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1000)
    done: StrictBool = False

    @field_validator("exerciseId")
    @classmethod
    def _eid(cls, v):
        return _exercise_id(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)


class _SessionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    date: StrictStr | None = None
    durationMin: StrictInt | None = Field(default=None, ge=0, le=1440)
    notes: StrictStr | None = Field(default=None, max_length=2000)
    completed: StrictBool | None = None
    sets: list[SessionSet] | None = Field(default=None, max_length=300)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v is not None and (not dates.DATE_ONLY_RE.match(v) or not dates.is_iso_date_string(v)):
            raise ValueError("date must be YYYY-MM-DD")
        return v


class SessionCreateBody(_SessionFields):
    planDayId: StrictStr | None = None


class SessionUpdateBody(_SessionFields):
    pass


def _day_rows(days: list[PlanDay]) -> list[dict]:
    return [
        {"name": d.name, "weekday": d.weekday, "sortOrder": n,
         "exercises": {"create": [
             {"exerciseId": e.exerciseId, "name": e.name, "sets": e.sets, "reps": e.reps,
              "weightKg": None if e.weightKg is None else as_decimal(e.weightKg), "restSec": e.restSec,
              "notes": e.notes or None, "sortOrder": m}
             for m, e in enumerate(d.exercises)]}}
        for n, d in enumerate(days)
    ]


def _set_rows(sets: list[SessionSet]) -> list[dict]:
    return [{"exerciseId": s.exerciseId, "name": s.name, "reps": s.reps,
             "weightKg": None if s.weightKg is None else as_decimal(s.weightKg), "done": s.done, "sortOrder": n}
            for n, s in enumerate(sets)]


def _sets_from_day(day) -> list[dict]:
    rows = []
    for e in day.exercises or []:
        m = _LEADING_INT.match(e.reps or "")
        for _ in range(e.sets):
            rows.append({"exerciseId": e.exerciseId, "name": e.name, "reps": int(m[1]) if m else None,
                         "weightKg": e.weightKg, "done": False, "sortOrder": len(rows)})
    return rows


# ----- catalog -------------------------------------------------------------------------


@router.get("/exercises")
async def exercises(user=Depends(plus_user)):
    return {"items": catalog_view(), "muscles": list(MUSCLES)}


# ----- plans ---------------------------------------------------------------------------


async def _owned_plan(user_id: str, id: str):
    row = await db.workoutplan.find_first(where={"id": id, "userId": user_id}, include=_PLAN_INCLUDE)
    if row is None:
        raise nest_error(404, "Workout plan not found")
    return row


async def create_plan(user, body: PlanCreateBody) -> dict:
    data = {"userId": user.id, "name": body.name, "description": body.description or None,
            "isActive": True if body.isActive is None else body.isActive}
    if body.days:
        data["days"] = {"create": _day_rows(body.days)}
    row = await db.workoutplan.create(data=data, include=_PLAN_INCLUDE)
    await log_activity(user.id, "workout_plan.created", entity_id=row.id, name=row.name)
    return plan_view(row)


@router.get("/plans")
async def list_plans(user=Depends(plus_user)):
    rows = await db.workoutplan.find_many(where={"userId": user.id}, include=_PLAN_INCLUDE,
                                          order=[{"isActive": "desc"}, {"updatedAt": "desc"}])
    return {"items": [plan_view(r) for r in rows], "total": len(rows)}


@router.get("/plans/{id}")
async def get_plan(id: str, user=Depends(plus_user)):
    return plan_view(await _owned_plan(user.id, id))


@router.post("/plans", status_code=201)
async def post_plan(body: PlanCreateBody, user=Depends(plus_user)):
    return await create_plan(user, body)


@router.patch("/plans/{id}")
async def update_plan(id: str, body: PlanUpdateBody, user=Depends(plus_user)):
    existing = await _owned_plan(user.id, id)
    sent = body.model_fields_set
    if ("name" in sent and body.name is None) or ("isActive" in sent and body.isActive is None):
        raise invalid_data()
    data: dict = {f: getattr(body, f) for f in ("name", "isActive") if f in sent}
    if "description" in sent:
        data["description"] = body.description or None
    async with db.tx() as tx:
        if body.days is not None:  # replaces the whole set (sessions keep their sets; their planDayId goes null)
            await tx.workoutplanday.delete_many(where={"planId": existing.id})
            for row in _day_rows(body.days):
                await tx.workoutplanday.create(data={**row, "planId": existing.id})
        await tx.workoutplan.update(where={"id": existing.id}, data=data or {"name": existing.name})
    await log_activity(user.id, "workout_plan.updated", entity_id=id, name=data.get("name", existing.name))
    return plan_view(await _owned_plan(user.id, id))


@router.delete("/plans/{id}")
async def remove_plan(id: str, user=Depends(plus_user)):
    existing = await _owned_plan(user.id, id)
    await db.workoutplan.delete(where={"id": existing.id})
    await log_activity(user.id, "workout_plan.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


# ----- sessions ------------------------------------------------------------------------


async def _owned_session(user_id: str, id: str):
    row = await db.workoutsession.find_first(where={"id": id, "userId": user_id}, include=_SESSION_INCLUDE)
    if row is None:
        raise nest_error(404, "Workout not found")
    return row


@router.get("/sessions")
async def list_sessions(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, ("from", "to", "limit"))
    for key in ("from", "to"):
        if q.get(key) is not None and not (dates.DATE_ONLY_RE.match(q.get(key)) and dates.is_iso_date_string(q.get(key))):
            errors.append(f"{key} must be YYYY-MM-DD")
    limit = 50
    if q.get("limit") is not None:
        if not q.get("limit").isdigit() or not 1 <= int(q.get("limit")) <= 200:
            errors.append("limit must be an integer between 1 and 200")
        else:
            limit = int(q.get("limit"))
    if errors:
        raise nest_error(400, errors)
    where: dict = {"userId": user.id}
    if q.get("from") or q.get("to"):
        where["date"] = {**({"gte": dates.parse_date_only(q.get("from"))} if q.get("from") else {}),
                         **({"lte": dates.parse_date_only(q.get("to"))} if q.get("to") else {})}
    rows = await db.workoutsession.find_many(where=where, include=_SESSION_INCLUDE, order=[{"date": "desc"}, {"createdAt": "desc"}], take=limit)
    return {"items": [session_view(r) for r in rows], "total": len(rows)}


@router.get("/sessions/{id}")
async def get_session(id: str, user=Depends(plus_user)):
    return session_view(await _owned_session(user.id, id))


@router.post("/sessions", status_code=201)
async def create_session(body: SessionCreateBody, user=Depends(plus_user)):
    ctx = await _ctx(user)
    day = None
    if body.planDayId is not None:
        day = await db.workoutplanday.find_first(where={"id": body.planDayId, "plan": {"is": {"userId": user.id}}},
                                                 include={"exercises": {"order_by": [{"sortOrder": "asc"}, {"id": "asc"}]}, "plan": True})
        if day is None:
            raise nest_error(404, "Workout plan day not found")
    sets = _set_rows(body.sets) if body.sets is not None else (_sets_from_day(day) if day else [])
    name = body.name or (f"{day.plan.name} · {day.name}" if day else "Workout")
    row = await db.workoutsession.create(data={
        "userId": user.id, "name": name[:120], "date": dates.parse_date_only(body.date or ctx.today),
        "planId": day.planId if day else None, "planDayId": day.id if day else None,
        "durationMin": body.durationMin, "notes": body.notes or None,
        "completedAt": datetime.now(timezone.utc) if body.completed else None,
        **({"sets": {"create": sets}} if sets else {}),
    }, include=_SESSION_INCLUDE)
    await log_activity(user.id, "workout.logged", entity_id=row.id, name=row.name)
    return session_view(row)


@router.patch("/sessions/{id}")
async def update_session(id: str, body: SessionUpdateBody, user=Depends(plus_user)):
    existing = await _owned_session(user.id, id)
    sent = body.model_fields_set
    if any(f in sent and getattr(body, f) is None for f in ("name", "date", "completed")):
        raise invalid_data()
    data: dict = {f: getattr(body, f) for f in ("name", "durationMin") if f in sent}
    if "notes" in sent:
        data["notes"] = body.notes or None
    if "date" in sent:
        data["date"] = dates.parse_date_only(body.date)
    if "completed" in sent:
        data["completedAt"] = (existing.completedAt or datetime.now(timezone.utc)) if body.completed else None
    async with db.tx() as tx:
        if body.sets is not None:
            await tx.workoutset.delete_many(where={"sessionId": existing.id})
            if body.sets:
                await tx.workoutset.create_many(data=[{**r, "sessionId": existing.id} for r in _set_rows(body.sets)])
        await tx.workoutsession.update(where={"id": existing.id}, data=data or {"name": existing.name})
    return session_view(await _owned_session(user.id, id))


@router.delete("/sessions/{id}")
async def remove_session(id: str, user=Depends(plus_user)):
    existing = await _owned_session(user.id, id)
    await db.workoutsession.delete(where={"id": existing.id})
    await log_activity(user.id, "workout.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


# ----- progress ------------------------------------------------------------------------


def compute_stats(sessions: list[dict], today: str, week_starts_on: int, weeks: int = 12) -> dict:
    """Pure: sessions are session_view dicts (newest first or any order)."""
    t = day_num(today)
    this_week = week_start_of(t, week_starts_on)
    first_week = this_week - 7 * (weeks - 1)
    series = {first_week + 7 * i: {"weekStart": day_str(first_week + 7 * i), "sessions": 0, "volumeKg": 0.0} for i in range(weeks)}
    exercises: dict[str, dict] = {}
    for s in sorted(sessions, key=lambda s: s["date"]):
        n = day_num(s["date"])
        ws = week_start_of(n, week_starts_on)
        if ws in series:
            series[ws]["sessions"] += 1
            series[ws]["volumeKg"] = round_money(series[ws]["volumeKg"] + s["volumeKg"])
        per_session: dict[str, float] = {}
        for st in s["sets"]:
            if not st["done"]:
                continue
            key = st["exerciseId"] or st["name"].strip().lower()
            e = exercises.setdefault(key, {"key": key, "exerciseId": st["exerciseId"], "name": st["name"], "sets": 0,
                                           "bestWeightKg": None, "best1RmKg": None, "lastDate": None, "history": []})
            e["sets"] += 1
            e["lastDate"] = s["date"]
            w, r = st["weightKg"], st["reps"]
            if w is not None:
                e["bestWeightKg"] = w if e["bestWeightKg"] is None else max(e["bestWeightKg"], w)
                per_session[key] = max(per_session.get(key, 0), w)
                if r:
                    est = round_money(w * (1 + r / 30))
                    e["best1RmKg"] = est if e["best1RmKg"] is None else max(e["best1RmKg"], est)
        for key, w in per_session.items():
            exercises[key]["history"].append({"date": s["date"], "weightKg": w})
    streak = 0  # consecutive weeks with at least one session, back from this week (this week pending)
    week = this_week
    weeks_with = {week_start_of(day_num(s["date"]), week_starts_on) for s in sessions}
    if this_week not in weeks_with:
        week -= 7
    while week in weeks_with:
        streak += 1
        week -= 7
    return {
        "totalSessions": len(sessions),
        "thisWeekSessions": sum(1 for s in sessions if week_start_of(day_num(s["date"]), week_starts_on) == this_week),
        "weekStreak": streak,
        "weekly": list(series.values()),
        "exercises": sorted(exercises.values(), key=lambda e: (-e["sets"], e["name"]))[:30],
    }


@router.get("/stats")
async def stats(user=Depends(plus_user)):
    ctx = await _ctx(user)
    rows = await db.workoutsession.find_many(where={"userId": user.id}, include=_SESSION_INCLUDE, order={"date": "desc"}, take=1000)
    out = compute_stats([session_view(r) for r in rows], ctx.today, ctx.week_starts_on)
    weekday = weekday_of(day_num(ctx.today))
    today_plan = None
    plans = await db.workoutplan.find_many(where={"userId": user.id, "isActive": True}, include=_PLAN_INCLUDE, order={"updatedAt": "desc"})
    for p in plans:
        day = next((d for d in (p.days or []) if d.weekday == weekday), None)
        if day:
            today_plan = {"planId": p.id, "planName": p.name, "dayId": day.id, "dayName": day.name, "exerciseCount": len(day.exercises or [])}
            break
    done_today = next((session_view(r) for r in rows if _dstr(r.date) == ctx.today), None)
    out.update(today=ctx.today, todayPlan=today_plan, todaySession=done_today)
    return out
