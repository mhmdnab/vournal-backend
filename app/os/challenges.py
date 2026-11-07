"""/api/challenges — personal challenges: a set of the user's habits kept for a fixed number of
days from a start date (e.g. "30 days of mornings": wake early, read, no sugar).

Progress is computed from the habits' own check-ins (no separate log):
- A day of the challenge is "perfect" when every habit scheduled that day (DAILY, or CUSTOM on
  its weekdays) is done. WEEKLY habits have no fixed days, so they count only towards the
  completion rate, never towards a perfect day.
- Completion = achieved / expected over the challenge's days so far, summed over its habits
  (habit_streaks.completion_between, so a not-yet-done today is pending, not missed).
- The perfect-day streak runs back from today (today not done yet is pending, not a break).
"""

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.query import _query_bool, unknown_params
from app.os.habit_streaks import _completion, completion_between, count_map, day_num, day_str, effective_start, is_scheduled_weekday
from app.os.habits import _ctx, _dstr, logs_by_habit, schedule_of

router = nest_router("/challenges")

MAX_DAYS = 365
MAX_HABITS = 20
_FIELDS = ("id", "name", "description", "icon", "color", "durationDays", "isArchived", "sortOrder", "createdAt", "updatedAt")
_INCLUDE = {"habits": {"include": {"habit": True}, "order_by": [{"sortOrder": "asc"}, {"id": "asc"}]}}


def _done(h: dict, counts: dict[int, int], n: int) -> bool:
    return counts.get(n, 0) >= max(1, h.get("targetPerDay") or 1)


def _day_scheduled(h: dict, counts: dict[int, int], n: int) -> bool:
    """Scheduled on a specific day (WEEKLY habits have no specific days)."""
    if h["frequency"] == "WEEKLY":
        return False
    return n >= effective_start(h, counts) and is_scheduled_weekday(h, n)


def compute_challenge(start_str: str, duration: int, habits: list[dict], logs: dict[str, list[dict]], today_str: str, week_starts_on: int) -> dict:
    """Pure: habits are schedule dicts with id/name/…; logs maps habit id → [{date, count}]."""
    start = day_num(start_str)
    end = start + duration - 1
    today = day_num(today_str)
    counts = {h["id"]: count_map(logs.get(h["id"], [])) for h in habits}
    status = "upcoming" if today < start else "completed" if today > end else "active"

    days = []
    for n in range(start, end + 1):
        scheduled = [h for h in habits if _day_scheduled(h, counts[h["id"]], n)]
        done = sum(1 for h in scheduled if _done(h, counts[h["id"]], n))
        days.append({"date": day_str(n), "scheduled": len(scheduled), "done": done,
                     "perfect": bool(scheduled) and done == len(scheduled), "future": n > today})

    past = [d for d in days if not d["future"]]
    perfect_days = sum(1 for d in past if d["perfect"])
    # streak of perfect days back from today; today not perfect yet is pending; days with nothing scheduled are skipped
    streak = 0
    for i, d in enumerate(reversed(past)):
        if d["scheduled"] == 0:
            continue
        if d["perfect"]:
            streak += 1
        elif i == 0 and d["date"] == today_str:
            continue
        else:
            break
    best = run = 0
    for d in past:
        if d["scheduled"] == 0:
            continue
        run = run + 1 if d["perfect"] else 0
        best = max(best, run)

    achieved = expected = 0
    habit_views = []
    for h in habits:
        c = completion_between(h, counts[h["id"]], start, end, today, week_starts_on)
        achieved += c["achieved"]
        expected += c["expected"]
        today_count = counts[h["id"]].get(today, 0)
        habit_views.append({
            "id": h["id"], "name": h["name"], "icon": h.get("icon"), "color": h.get("color"), "frequency": h["frequency"],
            "targetPerDay": h.get("targetPerDay") or 1, "unit": h.get("unit"), "isArchived": h.get("isArchived", False),
            "todayCount": today_count, "doneToday": _done(h, counts[h["id"]], today),
            "scheduledToday": start <= today <= end and (h["frequency"] == "WEEKLY" or _day_scheduled(h, counts[h["id"]], today)),
            "completion": c["rate"],
        })

    today_day = next((d for d in days if d["date"] == today_str), None)
    return {
        "startDate": start_str,
        "endDate": day_str(end),
        "status": status,
        "dayNumber": min(duration, max(0, today - start + 1)),
        "daysLeft": max(0, end - max(today, start - 1)),
        "perfectDays": perfect_days,
        "currentStreak": streak,
        "bestStreak": best,
        "completionRate": _completion(achieved, expected)["rate"],
        "todayDone": today_day["done"] if today_day else 0,
        "todayTotal": today_day["scheduled"] if today_day else 0,
        "habits": habit_views,
        "days": days,
    }


def _habit_dict(h) -> dict:
    return {**schedule_of(h), "id": h.id, "name": h.name, "icon": h.icon, "color": h.color, "unit": h.unit, "isArchived": h.isArchived}


def view(row, logs: dict[str, list[dict]], today: str, week_starts_on: int) -> dict:
    out = {f: getattr(row, f) for f in _FIELDS}
    habits = [_habit_dict(link.habit) for link in (row.habits or [])]
    out["habitIds"] = [h["id"] for h in habits]
    out.update(compute_challenge(_dstr(row.startDate), row.durationDays, habits, logs, today, week_starts_on))
    return out


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=2000)
    icon: StrictStr | None = None
    color: StrictStr | None = None
    startDate: StrictStr | None = None
    durationDays: StrictInt | None = Field(default=None, ge=1, le=MAX_DAYS)
    habitIds: list[StrictStr] | None = Field(default=None, max_length=MAX_HABITS)
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        return validators.icon(v)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)

    @field_validator("startDate")
    @classmethod
    def _start(cls, v):
        if v is not None and (not dates.DATE_ONLY_RE.match(v) or not dates.is_iso_date_string(v)):
            raise ValueError("startDate must be YYYY-MM-DD")
        return v

    @field_validator("habitIds")
    @classmethod
    def _habits(cls, v):
        if v is not None and len(set(v)) != len(v):
            raise ValueError("All habitIds's elements must be unique")
        return v


class CreateBody(_Fields):
    name: StrictStr
    durationDays: StrictInt = Field(ge=1, le=MAX_DAYS)
    habitIds: list[StrictStr] = Field(min_length=1, max_length=MAX_HABITS)


class UpdateBody(_Fields):
    isArchived: StrictBool | None = None


async def _owned(user_id: str, id: str):
    row = await db.challenge.find_first(where={"id": id, "userId": user_id}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Challenge not found")
    return row


async def _assert_habits(user_id: str, ids: list[str]) -> None:
    if ids and await db.habit.count(where={"id": {"in": ids}, "userId": user_id}) != len(ids):
        raise nest_error(404, "Habit not found")


async def _views(user, rows) -> list[dict]:
    ctx = await _ctx(user)
    ids = list(dict.fromkeys(link.habitId for r in rows for link in (r.habits or [])))
    logs = await logs_by_habit(user.id, ids)
    return [view(r, logs, ctx.today, ctx.week_starts_on) for r in rows]


async def get_view(user, id: str) -> dict:
    return (await _views(user, [await _owned(user.id, id)]))[0]


@router.get("")
async def list_challenges(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("archived",))
    archived = _query_bool(request, "archived", errors)
    if errors:
        raise nest_error(400, errors)
    rows = await db.challenge.find_many(where={"userId": user.id, "isArchived": bool(archived)}, include=_INCLUDE,
                                        order=[{"sortOrder": "asc"}, {"createdAt": "desc"}])
    items = await _views(user, rows)
    return {"items": items, "total": len(items)}


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await get_view(user, id)


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    await _assert_habits(user.id, body.habitIds)
    ctx = await _ctx(user)
    sort_order = body.sortOrder
    if sort_order is None:
        top = await db.challenge.find_first(where={"userId": user.id}, order={"sortOrder": "desc"})
        sort_order = (top.sortOrder if top else -1) + 1
    row = await db.challenge.create(data={
        "userId": user.id, "name": body.name, "description": body.description or None, "icon": body.icon, "color": body.color,
        "startDate": dates.parse_date_only(body.startDate or ctx.today), "durationDays": body.durationDays, "sortOrder": sort_order,
        "habits": {"create": [{"habitId": hid, "sortOrder": n} for n, hid in enumerate(body.habitIds)]},
    })
    await log_activity(user.id, "challenge.created", entity_id=row.id, name=row.name)
    return await get_view(user, row.id)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    for required in ("name", "durationDays", "startDate", "isArchived"):
        if required in sent and getattr(body, required) is None:
            raise invalid_data()
    if body.habitIds is not None:
        if not body.habitIds:
            raise nest_error(400, ["habitIds must contain at least 1 elements"])
        await _assert_habits(user.id, body.habitIds)
    data: dict = {f: getattr(body, f) for f in ("name", "icon", "color", "durationDays", "sortOrder", "isArchived") if f in sent}
    if "description" in sent:
        data["description"] = body.description or None
    if "startDate" in sent:
        data["startDate"] = dates.parse_date_only(body.startDate)
    async with db.tx() as tx:
        if body.habitIds is not None:  # replaces the whole set
            await tx.challengehabit.delete_many(where={"challengeId": existing.id})
            await tx.challengehabit.create_many(data=[{"challengeId": existing.id, "habitId": hid, "sortOrder": n} for n, hid in enumerate(body.habitIds)])
        await tx.challenge.update(where={"id": existing.id}, data=data or {"name": existing.name})
    await log_activity(user.id, "challenge.updated", entity_id=id, name=data.get("name", existing.name))
    return await get_view(user, id)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.challenge.delete(where={"id": existing.id})
    await log_activity(user.id, "challenge.deleted", entity_id=id, name=existing.name)
    return {"ok": True}
