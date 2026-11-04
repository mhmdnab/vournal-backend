"""/api/habits (port of vournalOS HabitsService). Streaks, completion and schedules come
from habit_streaks; check-ins are per local day ("YYYY-MM-DD" in the user's timezone)."""

import math
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.ownership import assert_many_owned, assert_owned
from app.os.common.query import _query_bool, unknown_params
from app.os.habit_streaks import (
    completion_between, compute_habit, compute_streaks, count_map, day_num, day_status, day_str, effective_start,
    is_scheduled_weekday, weekday_of,
)
from app.os.profile import ensure_os_profile

router = nest_router("/habits")

MAX_CHECK_COUNT = 50
MAX_RANGE_DAYS = 731
FREQUENCIES = ("DAILY", "WEEKLY", "CUSTOM")
FrequencyName = Literal[FREQUENCIES]  # type: ignore[valid-type]
_FIELDS = (
    "id", "name", "description", "icon", "color", "frequency", "daysOfWeek", "timesPerWeek", "targetPerDay", "unit",
    "reminderTime", "startDate", "isArchived", "categoryId", "goalId", "sortOrder", "createdAt", "updatedAt",
)
_INCLUDE = {"category": True, "goal": True}


class Ctx:
    def __init__(self, settings):
        self.tz = settings.timezone
        self.week_starts_on = settings.weekStartsOn
        self.today = dates.local_date_string(dates._now(), settings.timezone)


async def _ctx(user) -> Ctx:
    return Ctx(await ensure_os_profile(user))


def _round1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def _dstr(value) -> str:
    return dates.format_date_only(value)


def _pick(row, *fields):
    return None if row is None else {f: getattr(row, f) for f in fields}


def schedule_of(h) -> dict:
    return {
        "frequency": h.frequency, "daysOfWeek": list(h.daysOfWeek or []), "timesPerWeek": h.timesPerWeek,
        "targetPerDay": h.targetPerDay, "startDate": _dstr(h.startDate),
    }


def view(row, logs: list[dict], ctx: Ctx) -> dict:
    out = {f: getattr(row, f) for f in _FIELDS}
    out.update(category=_pick(row.category, "id", "name", "color", "icon"), goal=_pick(row.goal, "id", "name", "type", "color", "icon"))
    out.update(compute_habit({**schedule_of(row), "isArchived": row.isArchived}, logs, ctx.today, ctx.week_starts_on))
    return out


async def logs_by_habit(user_id: str, habit_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if habit_ids:
        for log in await db.habitlog.find_many(where={"userId": user_id, "habitId": {"in": habit_ids}}, order={"date": "asc"}):
            out.setdefault(log.habitId, []).append({"id": log.id, "date": _dstr(log.date), "count": log.count, "note": log.note})
    return out


def _date_problems(value, field: str) -> list[str]:
    """@IsDateString() @Matches(/^\\d{4}-\\d{2}-\\d{2}$/, { message: "<field> must be YYYY-MM-DD" })."""
    problems = []
    if not isinstance(value, str) or not dates.DATE_ONLY_RE.match(value):
        problems.append(f"{field} must be YYYY-MM-DD")
    if not dates.is_iso_date_string(value):
        problems.append(f"{field} must be a valid ISO 8601 date string")
    return problems


def _days_of_week_problems(v) -> list[str]:
    problems = []
    if not isinstance(v, list):
        return ["each value in daysOfWeek must not be greater than 6", "each value in daysOfWeek must not be less than 0",
                "each value in daysOfWeek must be an integer number", "All daysOfWeek's elements must be unique",
                "daysOfWeek must contain no more than 7 elements", "daysOfWeek must be an array"]
    numbers = [d for d in v if isinstance(d, (int, float)) and not isinstance(d, bool)]
    if any(not (d <= 6) for d in v if d in numbers) or len(numbers) != len(v):
        problems.append("each value in daysOfWeek must not be greater than 6")
    if any(not (d >= 0) for d in numbers) or len(numbers) != len(v):
        problems.append("each value in daysOfWeek must not be less than 0")
    if len(numbers) != len(v) or any(not float(d).is_integer() for d in numbers):
        problems.append("each value in daysOfWeek must be an integer number")
    if len(set(map(repr, v))) != len(v):
        problems.append("All daysOfWeek's elements must be unique")
    if len(v) > 7:
        problems.append("daysOfWeek must contain no more than 7 elements")
    return problems


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=2000)
    icon: StrictStr | None = None
    color: StrictStr | None = None
    frequency: FrequencyName | None = None
    daysOfWeek: object = None
    timesPerWeek: StrictInt | None = Field(default=None, ge=1, le=7)
    targetPerDay: StrictInt | None = Field(default=None, ge=1, le=50)
    unit: StrictStr | None = Field(default=None, max_length=30)
    reminderTime: StrictStr | None = None
    startDate: object = None
    categoryId: StrictStr | None = None
    goalId: StrictStr | None = None
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        if v is not None and len(v) > 50:
            raise ValueError("icon must be shorter than or equal to 50 characters")
        return validators.icon(v)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)

    @field_validator("daysOfWeek")
    @classmethod
    def _days(cls, v):
        if v is not None and (problems := _days_of_week_problems(v)):
            raise ValueError("\n".join(problems))
        return v

    @field_validator("reminderTime")
    @classmethod
    def _time(cls, v):
        if v is not None and not validators.HHMM.match(v):
            raise ValueError("reminderTime must be a time like 08:30")
        return v

    @field_validator("startDate")
    @classmethod
    def _start(cls, v):
        if v is not None and (problems := _date_problems(v, "startDate")):
            raise ValueError("\n".join(problems))
        return v


class CreateBody(_Fields):
    name: StrictStr


class UpdateBody(_Fields):
    isArchived: StrictBool | None = None


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: object = None
    count: StrictInt | None = Field(default=None, ge=0, le=50)
    note: StrictStr | None = Field(default=None, max_length=500)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v is not None and (problems := _date_problems(v, "date")):
            raise ValueError("\n".join(problems))
        return v


class ReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1)
    sortOrder: StrictInt = Field(ge=0)


class ReorderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReorderItem] = Field(min_length=1, max_length=500)


def _normalise_schedule(frequency: str, days_of_week, times_per_week) -> dict:
    if frequency == "CUSTOM":
        days = sorted(set(days_of_week or []))
        if not days:
            raise nest_error(400, "Pick at least one day of the week for a custom schedule")
        return {"daysOfWeek": days, "timesPerWeek": None}
    if frequency == "WEEKLY":
        return {"daysOfWeek": [], "timesPerWeek": times_per_week if times_per_week is not None else 1}
    return {"daysOfWeek": [], "timesPerWeek": None}


def _valid_date(value: str | None, ctx: Ctx):
    """A day (default: the user's today), never in the future."""
    text = value if value is not None else ctx.today
    day = dates.parse_date_only(text)
    if day is None:
        raise nest_error(400, "date must be YYYY-MM-DD")
    if text > ctx.today:
        raise nest_error(400, "Cannot check a habit in the future")
    return day


def _range(q: dict, ctx: Ctx, default_span: int) -> tuple[str, str]:
    to = q.get("to") or ctx.today
    start = q.get("from") or day_str(day_num(to) - default_span)
    if start > to:
        raise nest_error(400, "from must be before to")
    if day_num(to) - day_num(start) > MAX_RANGE_DAYS:
        raise nest_error(400, "Range too large (max 2 years)")
    return start, to


def _range_errors(request: Request, errors: list) -> dict:
    q = request.query_params
    for field in ("from", "to"):
        if q.get(field) is not None:
            errors.extend(_date_problems(q.get(field), field))
    return {"from": q.get("from"), "to": q.get("to")}


async def _owned(user_id: str, id: str):
    row = await db.habit.find_first(where={"id": id, "userId": user_id}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Habit not found")
    return row


async def _get(user, id: str) -> dict:
    ctx = await _ctx(user)
    row = await _owned(user.id, id)
    return view(row, (await logs_by_habit(user.id, [id])).get(id, []), ctx)


@router.get("")
async def list_habits(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("goalId", "archived", "today"))
    archived = _query_bool(request, "archived", errors)
    today_only = _query_bool(request, "today", errors)
    if errors:
        raise nest_error(400, errors)
    ctx = await _ctx(user)
    where: dict = {"userId": user.id, "isArchived": bool(archived)}
    if request.query_params.get("goalId"):
        where["goalId"] = request.query_params.get("goalId")
    rows = await db.habit.find_many(where=where, include=_INCLUDE, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    logs = await logs_by_habit(user.id, [r.id for r in rows])
    items = [view(r, logs.get(r.id, []), ctx) for r in rows]
    if today_only:
        items = [h for h in items if h["isScheduledToday"]]
    return {"items": items, "total": len(items)}


@router.get("/stats")
async def stats(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("from", "to", "includeArchived", "habitId"))
    q = _range_errors(request, errors)
    include_archived = _query_bool(request, "includeArchived", errors)
    if errors:
        raise nest_error(400, errors)
    ctx = await _ctx(user)
    start, end = _range(q, ctx, 89)
    where: dict = {"userId": user.id, **({} if include_archived else {"isArchived": False})}
    if request.query_params.get("habitId"):
        where["id"] = request.query_params.get("habitId")
    rows = await db.habit.find_many(where=where, include=_INCLUDE, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    logs_map = await logs_by_habit(user.id, [r.id for r in rows])
    today, from_n, to_n = day_num(ctx.today), day_num(start), day_num(end)
    day_agg = {n: {"done": 0, "expected": 0, "count": 0} for n in range(from_n, to_n + 1)}
    achieved_sum = expected_sum = 0
    habits = []
    for h in rows:
        schedule = schedule_of(h)
        counts = count_map(logs_map.get(h.id, []))
        first = effective_start(schedule, counts)
        completion = completion_between(schedule, counts, from_n, to_n, today, ctx.week_starts_on)
        achieved_sum += completion["achieved"]
        expected_sum += completion["expected"]
        streaks = compute_streaks(schedule, counts, today, ctx.week_starts_on)
        done_days = 0
        for n in range(max(from_n, first), min(to_n, today) + 1):
            count = counts.get(n, 0)
            done = count >= max(1, h.targetPerDay)
            agg = day_agg[n]
            agg["count"] += count
            done_days += done
            if h.frequency == "WEEKLY":
                if done:
                    agg["done"] += 1
                    agg["expected"] += 1
            elif is_scheduled_weekday(schedule, n):
                agg["expected"] += 1
                agg["done"] += done
        habits.append({
            "id": h.id, "name": h.name, "icon": h.icon, "color": h.color, "frequency": h.frequency, "isArchived": h.isArchived,
            "targetPerDay": h.targetPerDay, "unit": h.unit, "achieved": completion["achieved"], "expected": completion["expected"],
            "completionRate": completion["rate"], "doneDays": done_days, "currentStreak": streaks["current"],
            "bestStreak": streaks["best"], "streakUnit": "week" if h.frequency == "WEEKLY" else "day",
        })
    days = [
        {"date": day_str(n), "done": a["done"], "expected": a["expected"], "count": a["count"],
         "rate": _round1(a["done"] / a["expected"] * 100) if a["expected"] > 0 else None}
        for n, a in day_agg.items()
    ]
    weekday = [{"weekday": i, "done": 0, "expected": 0, "rate": None} for i in range(7)]
    perfect = active = 0
    for n, a in day_agg.items():
        if n > today:
            continue
        if a["done"] > 0:
            active += 1
        if n == today and a["done"] < a["expected"]:
            continue  # pending
        if a["expected"] > 0 and a["done"] >= a["expected"]:
            perfect += 1
        w = weekday[weekday_of(n)]
        w["done"] += a["done"]
        w["expected"] += a["expected"]
    for w in weekday:
        w["rate"] = _round1(w["done"] / w["expected"] * 100) if w["expected"] > 0 else None
    today_agg = day_agg.get(today)
    return {
        "from": start,
        "to": end,
        "today": ctx.today,
        "weekStartsOn": ctx.week_starts_on,
        "overall": {
            "completionRate": _round1(achieved_sum / expected_sum * 100) if expected_sum > 0 else None,
            "achieved": achieved_sum,
            "expected": expected_sum,
            "habitCount": len(rows),
            "perfectDays": perfect,
            "activeDays": active,
            "bestCurrentStreak": max((h["currentStreak"] for h in habits), default=0),
            "bestStreak": max((h["bestStreak"] for h in habits), default=0),
            "today": {"done": today_agg["done"], "expected": today_agg["expected"]} if today_agg else None,
        },
        "habits": habits,
        "days": days,
        "byWeekday": weekday,
    }


@router.post("/reorder")
async def reorder(body: ReorderBody, user=Depends(plus_user)):
    await assert_many_owned("habit", [i.id for i in body.items], user.id)
    async with db.batch_() as batch:  # one atomic request, as NestJS's $transaction([...])
        for item in body.items:
            batch.habit.update_many(where={"id": item.id, "userId": user.id}, data={"sortOrder": item.sortOrder})
    return {"ok": True}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    await assert_owned("category", body.categoryId, user.id)
    await assert_owned("goal", body.goalId, user.id)
    ctx = await _ctx(user)
    frequency = body.frequency or "DAILY"
    schedule = _normalise_schedule(frequency, body.daysOfWeek, body.timesPerWeek)
    sort_order = body.sortOrder
    if sort_order is None:
        top = await db.habit.find_first(where={"userId": user.id}, order={"sortOrder": "desc"})
        sort_order = (top.sortOrder if top else -1) + 1
    row = await db.habit.create(
        data={
            "userId": user.id, "name": body.name, "description": body.description, "icon": body.icon, "color": body.color,
            "frequency": frequency, "daysOfWeek": schedule["daysOfWeek"], "timesPerWeek": schedule["timesPerWeek"],
            "targetPerDay": body.targetPerDay or 1, "unit": body.unit, "reminderTime": body.reminderTime,
            "startDate": dates.parse_date_only(body.startDate or ctx.today), "categoryId": body.categoryId, "goalId": body.goalId,
            "sortOrder": sort_order,
        },
        include=_INCLUDE,
    )
    await log_activity(user.id, "habit.created", entity_id=row.id, name=row.name)
    return view(row, [], ctx)


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await _get(user, id)


@router.get("/{id}/history")
async def history(id: str, request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("from", "to"))
    q = _range_errors(request, errors)
    if errors:
        raise nest_error(400, errors)
    habit = await _owned(user.id, id)
    ctx = await _ctx(user)
    start, end = _range(q, ctx, 364)
    logs = (await logs_by_habit(user.id, [id])).get(id, [])
    schedule, counts = schedule_of(habit), count_map(logs)
    items = [
        {"id": l["id"], "date": l["date"], "count": l["count"], "note": l["note"], "done": l["count"] >= habit.targetPerDay}
        for l in logs if start <= l["date"] <= end
    ]
    return {
        "habitId": id, "from": start, "to": end, "today": ctx.today, "items": items, "total": len(items),
        "days": [day_status(schedule, counts, n) for n in range(day_num(start), day_num(end) + 1)],
        "completion": completion_between(schedule, counts, day_num(start), day_num(end), day_num(ctx.today), ctx.week_starts_on),
    }


async def _update(user, id: str, body: UpdateBody) -> dict:
    existing = await _owned(user.id, id)
    await assert_owned("category", body.categoryId, user.id)
    await assert_owned("goal", body.goalId, user.id)
    sent = body.model_fields_set
    # null on a required column: Prisma refused it (a null daysOfWeek just keeps the stored days)
    if any(f in sent and getattr(body, f) is None for f in ("name", "frequency", "targetPerDay", "sortOrder", "isArchived")):
        raise invalid_data()
    frequency = body.frequency or existing.frequency
    touched = bool(sent & {"frequency", "daysOfWeek", "timesPerWeek"})
    data: dict = {}
    if touched:
        days = body.daysOfWeek if body.daysOfWeek is not None else existing.daysOfWeek
        times = body.timesPerWeek if "timesPerWeek" in sent else existing.timesPerWeek
        data.update(_normalise_schedule(frequency, days, times))
    for field in ("name", "description", "icon", "color", "frequency", "targetPerDay", "unit", "reminderTime", "categoryId", "goalId", "sortOrder", "isArchived"):
        if field in sent:
            data[field] = getattr(body, field)
    if body.startDate:
        data["startDate"] = dates.parse_date_only(body.startDate)
    row = await db.habit.update(where={"id": existing.id}, data=data or {"name": existing.name}, include=_INCLUDE)
    if body.isArchived is not None and body.isArchived != existing.isArchived:
        await log_activity(user.id, "habit.archived" if body.isArchived else "habit.unarchived", entity_id=id, name=row.name)
    else:
        await log_activity(user.id, "habit.updated", entity_id=id)
    return await _get(user, row.id)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    return await _update(user, id, body)


@router.post("/{id}/check")
async def check(id: str, body: CheckBody, user=Depends(plus_user)):
    habit = await _owned(user.id, id)
    ctx = await _ctx(user)
    day = _valid_date(body.date, ctx)
    key = {"habitId_date": {"habitId": habit.id, "date": day}}
    existing = await db.habitlog.find_unique(where=key)
    nxt = body.count if body.count is not None else min(MAX_CHECK_COUNT, (existing.count if existing else 0) + 1)
    if nxt <= 0:
        if existing:
            await db.habitlog.delete(where={"id": existing.id})
        await log_activity(user.id, "habit.unchecked", entity_id=habit.id, name=habit.name, date=_dstr(day), count=0)
        return await _get(user, habit.id)
    await db.habitlog.upsert(
        where=key,
        data={
            "create": {"habitId": habit.id, "userId": user.id, "date": day, "count": nxt, "note": body.note},
            "update": {"count": nxt, **({"note": body.note} if "note" in body.model_fields_set else {})},
        },
    )
    await log_activity(
        user.id, "habit.checked", entity_id=habit.id, name=habit.name, date=_dstr(day), count=nxt, done=nxt >= habit.targetPerDay
    )
    return await _get(user, habit.id)


@router.delete("/{id}/check")
async def uncheck(id: str, request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("date", "all"))
    value = request.query_params.get("date")
    if value is not None:
        errors.extend(_date_problems(value, "date"))
    remove_all = _query_bool(request, "all", errors)
    if errors:
        raise nest_error(400, errors)
    habit = await _owned(user.id, id)
    ctx = await _ctx(user)
    day = _valid_date(value, ctx)
    existing = await db.habitlog.find_unique(where={"habitId_date": {"habitId": habit.id, "date": day}})
    if existing:
        nxt = 0 if remove_all else existing.count - 1
        if nxt <= 0:
            await db.habitlog.delete(where={"id": existing.id})
        else:
            await db.habitlog.update(where={"id": existing.id}, data={"count": nxt})
        await log_activity(user.id, "habit.unchecked", entity_id=habit.id, name=habit.name, date=_dstr(day), count=max(0, nxt))
    return await _get(user, habit.id)


@router.post("/{id}/archive")
async def archive(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(isArchived=True))


@router.post("/{id}/unarchive")
async def unarchive(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(isArchived=False))


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.habit.delete(where={"id": existing.id})
    await log_activity(user.id, "habit.deleted", entity_id=id, name=existing.name)
    return {"ok": True}
