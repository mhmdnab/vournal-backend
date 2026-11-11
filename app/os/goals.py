"""/api/goals and /api/goals/{goalId}/milestones (port of vournalOS GoalsService and
GoalMilestonesService). Progress comes from a value (a linked account's balance, or the
manual value plus contributions for financial goals), else from milestones, else status."""

import math
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import finance_utils as fu
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.currency import get_converter, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.jsparse import js_sum
from app.os.common.ownership import assert_owned
from app.os.common.query import _query_list, unknown_params
from app.os.habit_streaks import compute_habit
from app.os.profile import ensure_os_profile
from app.os.transactions import as_decimal

router = nest_router("/goals")

DAYS_PER_MONTH = 365.25 / 12
MAX_VALUE = 999_999_999_999
TYPES = ("FINANCIAL", "PERSONAL", "FITNESS", "EDUCATION", "CAREER", "BUSINESS", "TRAVEL", "OTHER")
STATUSES = ("ACTIVE", "COMPLETED", "PAUSED", "ABANDONED")
_RANK = {"ACTIVE": 0, "PAUSED": 1, "COMPLETED": 2, "ABANDONED": 3}
TypeName = Literal[TYPES]  # type: ignore[valid-type]
StatusName = Literal[STATUSES]  # type: ignore[valid-type]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
_FIELDS = (
    "id", "name", "description", "notes", "type", "status", "targetValue", "currentValue", "unit", "currency", "startDate",
    "deadline", "linkedAccountId", "categoryId", "color", "icon", "completedAt", "createdAt", "updatedAt",
)
_INCLUDE = {"linkedAccount": True, "category": True, "milestones": True}


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _round1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def _round2(x: float) -> float:
    return math.floor(x * 100 + 0.5) / 100


def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return min(hi, max(lo, x))


def _num(v) -> float:
    return to_number(v if v is not None else 0)


def _dstr(v) -> str:
    return dates.format_date_only(v)


def _pick(row, *fields):
    return None if row is None else {f: getattr(row, f) for f in fields}


class Ctx:
    def __init__(self, settings, fx):
        self.tz = settings.timezone
        self.week_starts_on = settings.weekStartsOn
        self.today = dates.local_date_string(dates._now(), settings.timezone)
        self.fx = fx
        self.default_currency = settings.defaultCurrency


async def _ctx(user) -> Ctx:
    return Ctx(await ensure_os_profile(user), await get_converter(user.id))


def milestone_view(m, today: str) -> dict:
    out = _pick(m, "id", "goalId", "title", "targetValue", "dueDate", "completedAt", "sortOrder", "createdAt")
    out.update(completed=m.completedAt is not None, isOverdue=m.completedAt is None and m.dueDate is not None and _dstr(m.dueDate) < today)
    return out


async def _account_balances(user_id: str, account_ids: list[str], fx) -> dict[str, float]:
    """In the account's own currency: initial + INCOME − EXPENSE − TRANSFER out + TRANSFER in."""
    out: dict[str, float] = {}
    if not account_ids:
        return out
    accounts = await db.account.find_many(where={"userId": user_id, "id": {"in": account_ids}})
    own = await db.transaction.group_by(by=["accountId", "type", "currency"], where={"userId": user_id, "accountId": {"in": account_ids}}, sum={"amount": True})
    in_plain = await db.transaction.group_by(
        by=["toAccountId", "currency"], where={"userId": user_id, "type": "TRANSFER", "toAccountId": {"in": account_ids}, "toAmount": None}, sum={"amount": True}
    )
    in_to = await db.transaction.group_by(
        by=["toAccountId"], where={"userId": user_id, "type": "TRANSFER", "toAccountId": {"in": account_ids}, "toAmount": {"not": None}}, sum={"toAmount": True}
    )
    for a in accounts:
        balance = _num(a.initialBalance)
        for g in own:
            if g["accountId"] == a.id:
                v = fx.convert(fu.group_decimal(g["_sum"]["amount"]), g["currency"], a.currency)
                balance += v if g["type"] == "INCOME" else -v
        for g in in_plain:
            if g["toAccountId"] == a.id:
                balance += fx.convert(fu.group_decimal(g["_sum"]["amount"]), g["currency"], a.currency)
        for g in in_to:
            if g["toAccountId"] == a.id:
                balance += to_number(fu.group_decimal(g["_sum"]["toAmount"]))
        out[a.id] = _round2(balance)
    return out


async def _counts(user_id: str, goal_ids: list[str]) -> dict[str, dict]:
    """_count of tasks, projects, habits and transactions per goal."""
    out = {g: {"tasks": 0, "projects": 0, "habits": 0, "transactions": 0} for g in goal_ids}
    for key, table in (("tasks", db.task), ("projects", db.project), ("habits", db.habit), ("transactions", db.transaction)):
        for g in await table.group_by(by=["goalId"], where={"goalId": {"in": goal_ids}}, count={"_all": True}):
            out[g["goalId"]][key] = g["_count"]["_all"]
    return out


def _compute(row, money: dict, counts: dict, ctx: Ctx) -> dict:
    target = None if row.targetValue is None else _num(row.targetValue)
    milestones = row.milestones or []
    ms_total, ms_done = len(milestones), sum(1 for m in milestones if m.completedAt)
    completed = row.status == "COMPLETED"
    percent, source = 0.0, "none"
    if target is not None and target > 0:
        percent, source = _clamp(money["current"] / target * 100), "value"
    elif ms_total > 0:
        percent, source = ms_done / ms_total * 100, "milestones"
    if completed:
        percent = max(percent, 100)
        if source == "none":
            source = "status"
    percent = _round1(min(100, percent))
    remaining = _round2(max(0, target - money["current"])) if target is not None else None
    today = dates.parse_date_only(ctx.today)
    days_left = dates.diff_days(today, row.deadline) if row.deadline else None
    start = row.startDate or dates.to_date_only(row.createdAt, ctx.tz)
    elapsed_pct = None
    if row.deadline:
        total, elapsed = dates.diff_days(start, row.deadline), dates.diff_days(start, today)
        elapsed_pct = (100 if elapsed >= 0 else 0) if total <= 0 else _round1(_clamp(elapsed / total * 100))
    on_track = True if completed else (percent >= elapsed_pct if elapsed_pct is not None and source != "none" else None)
    months_left = weeks_left = monthly = weekly = None
    if days_left is not None:
        months_left = max(1, math.ceil(days_left / DAYS_PER_MONTH))
        weeks_left = max(1, math.ceil(days_left / 7))
        if remaining is not None and not completed:
            monthly, weekly = _round2(remaining / months_left), _round2(remaining / weeks_left)
    out = {f: getattr(row, f) for f in _FIELDS}
    out.update(
        linkedAccount=_pick(row.linkedAccount, "id", "name", "type", "currency", "color", "icon", "isArchived"),
        category=_pick(row.category, "id", "name", "color", "icon"),
        current=money["current"],
        target=target,
        manualValue=money["manualValue"],
        displayCurrency=money["displayCurrency"],
        progressPercent=percent,
        progressSource=source,
        remaining=remaining,
        daysLeft=days_left,
        isOverdue=not completed and row.status != "ABANDONED" and days_left is not None and days_left < 0,
        timeElapsedPercent=elapsed_pct,
        onTrack=on_track,
        monthsLeft=months_left,
        weeksLeft=weeks_left,
        monthlyRequired=monthly,
        weeklyRequired=weekly,
        contributionsTotal=money["contributionsTotal"],
        contributionsCount=money["contributionsCount"],
        accountBalance=money["accountBalance"],
        milestoneCounts={"total": ms_total, "completed": ms_done},
        counts=counts,
    )
    return out


async def views(user_id: str, rows, ctx: Ctx, sync_completion: bool = True) -> list[dict]:
    """Computed fields; goals whose value reached the target are completed on the way."""
    if not rows:
        return []
    goal_ids = [r.id for r in rows]
    balances = await _account_balances(user_id, list(dict.fromkeys(r.linkedAccountId for r in rows if r.linkedAccountId)), ctx.fx)
    contributions = await db.transaction.group_by(
        by=["goalId", "currency"], where={"userId": user_id, "goalId": {"in": goal_ids}}, sum={"amount": True}, count={"_all": True}
    )
    counts = await _counts(user_id, goal_ids)
    out = []
    for row in rows:
        display = (
            row.currency or (row.linkedAccount.currency if row.linkedAccount else None) or ctx.fx.default_currency
            if row.type == "FINANCIAL" or row.linkedAccountId else None
        )
        total = count = 0
        for c in contributions:
            if c["goalId"] != row.id:
                continue
            count += c["_count"]["_all"]
            total += ctx.fx.convert(fu.group_decimal(c["_sum"]["amount"]), c["currency"], display or c["currency"])
        total = _round2(total)
        manual = _num(row.currentValue)
        balance = None
        current = manual
        if row.linkedAccountId and row.linkedAccount:
            balance = ctx.fx.convert(balances.get(row.linkedAccountId, 0), row.linkedAccount.currency, display or row.linkedAccount.currency)
            current = balance
        elif row.type == "FINANCIAL":
            current = _round2(manual + total)
        money = {"current": current, "manualValue": manual, "accountBalance": balance, "contributionsTotal": total, "contributionsCount": count, "displayCurrency": display}
        out.append(_compute(row, money, counts[row.id], ctx))
    if sync_completion:
        for v in out:
            if v["status"] == "ACTIVE" and v["target"] is not None and v["target"] > 0 and v["current"] >= v["target"] and v["progressSource"] == "value":
                now = datetime.now(timezone.utc)
                await db.goal.update_many(where={"id": v["id"], "userId": user_id, "status": "ACTIVE"}, data={"status": "COMPLETED", "completedAt": now})
                v.update(status="COMPLETED", completedAt=now, onTrack=True, isOverdue=False)
                await log_activity(user_id, "goal.completed", entity_id=v["id"], name=v["name"], auto=True)
    return out


async def _owned(user_id: str, id: str):
    row = await db.goal.find_first(where={"id": id, "userId": user_id}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Goal not found")
    return row


def _date_problems(value, field: str) -> list[str]:
    problems = []
    if not isinstance(value, str) or not dates.DATE_ONLY_RE.match(value):
        problems.append(f"{field} must be YYYY-MM-DD")
    if not dates.is_iso_date_string(value):
        problems.append(f"{field} must be a valid ISO 8601 date string")
    return problems


def _amount(v, field: str, minimum: float):
    if v is None:
        return v
    if validators.money(v, field, minimum) > MAX_VALUE:
        raise ValueError(f"{field} must not be greater than {MAX_VALUE}")
    return v


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=2000)
    notes: StrictStr | None = Field(default=None, max_length=10000)
    type: TypeName | None = None
    status: StatusName | None = None
    targetValue: object = None
    currentValue: object = None
    unit: StrictStr | None = Field(default=None, max_length=30)
    currency: CurrencyCode | None = None
    startDate: object = None
    deadline: object = None
    linkedAccountId: StrictStr | None = None
    categoryId: StrictStr | None = None
    color: StrictStr | None = None
    icon: StrictStr | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("targetValue", "currentValue")
    @classmethod
    def _values(cls, v, info):
        return _amount(v, info.field_name, 0)

    @field_validator("startDate", "deadline")
    @classmethod
    def _dates(cls, v, info):
        if v is not None and (problems := _date_problems(v, info.field_name)):
            raise ValueError("\n".join(problems))
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        if v is not None and len(v) > 50:
            raise ValueError("icon must be shorter than or equal to 50 characters")
        return validators.icon(v)


class CreateBody(_Fields):
    name: StrictStr


class UpdateBody(_Fields):
    pass


class ProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delta: object = None
    value: object = None

    @field_validator("delta")
    @classmethod
    def _delta(cls, v):
        if v is None:
            return v
        validators.money(v, "delta")
        if v < -MAX_VALUE:
            raise ValueError(f"delta must not be less than {-MAX_VALUE}")
        if v > MAX_VALUE:
            raise ValueError(f"delta must not be greater than {MAX_VALUE}")
        return v

    @field_validator("value")
    @classmethod
    def _value(cls, v):
        return _amount(v, "value", 0)


async def _list(user, q: dict, ctx: Ctx) -> list[dict]:
    where: dict = {"userId": user.id}
    if q.get("status"):
        where["status"] = {"in": q["status"]}
    if q.get("type"):
        where["type"] = {"in": q["type"]}
    if q.get("q"):
        where["name"] = {"contains": q["q"], "mode": "insensitive"}
    rows = await db.goal.find_many(where=where, include=_INCLUDE, order=[{"deadline": {"sort": "asc", "nulls": "last"}}, {"createdAt": "desc"}])
    items = await views(user.id, rows, ctx)
    items.sort(key=lambda g: _RANK[g["status"]])
    return items


@router.get("")
async def list_goals(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("status", "type", "q"))
    statuses = _query_list(request, "status", STATUSES, errors)
    types = _query_list(request, "type", TYPES, errors)
    text = request.query_params.get("q")
    if text is not None and len(text) > 100:
        errors.append("q must be shorter than or equal to 100 characters")
    if errors:
        raise nest_error(400, errors)
    items = await _list(user, {"status": statuses, "type": types, "q": text}, await _ctx(user))
    return {"items": items, "total": len(items)}


@router.get("/summary")
async def summary(user=Depends(plus_user)):
    items = await _list(user, {}, await _ctx(user))
    active = [g for g in items if g["status"] == "ACTIVE"]
    tracked = [g for g in active if g["onTrack"] is not None]
    return {
        "total": len(items),
        "active": len(active),
        "completed": sum(1 for g in items if g["status"] == "COMPLETED"),
        "paused": sum(1 for g in items if g["status"] == "PAUSED"),
        "abandoned": sum(1 for g in items if g["status"] == "ABANDONED"),
        "onTrack": sum(1 for g in tracked if g["onTrack"]),
        "offTrack": sum(1 for g in tracked if not g["onTrack"]),
        "overdue": sum(1 for g in active if g["isOverdue"]),
        "averageProgress": _round1(js_sum(g["progressPercent"] for g in active) / len(active)) if active else None,
        "dueSoon": [
            {"id": g["id"], "name": g["name"], "daysLeft": g["daysLeft"], "progressPercent": g["progressPercent"]}
            for g in active if g["daysLeft"] is not None and 0 <= g["daysLeft"] <= 30
        ],
    }


async def _project_progress(user_id: str, project_ids: list[str]) -> dict[str, int]:
    if not project_ids:
        return {}
    totals: dict[str, list[int]] = {}
    for g in await db.task.group_by(by=["projectId", "status"], where={"userId": user_id, "workspaceId": None, "projectId": {"in": project_ids}, "parentId": None}, count={"_all": True}):
        if not g.get("projectId"):
            continue
        t = totals.setdefault(g["projectId"], [0, 0])
        t[1] += g["_count"]["_all"]
        t[0] += g["_count"]["_all"] if g["status"] == "COMPLETED" else 0
    return {pid: (_js_round(done / total * 100) if total else 0) for pid, (done, total) in totals.items()}


async def _detail(user, id: str) -> dict:
    row = await _owned(user.id, id)
    ctx = await _ctx(user)
    (goal,) = await views(user.id, [row], ctx)
    milestones = await db.goalmilestone.find_many(where={"goalId": id, "goal": {"is": {"userId": user.id}}}, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    tasks = await db.task.find_many(
        where={"userId": user.id, "workspaceId": None, "goalId": id, "parentId": None},
        order=[{"status": "asc"}, {"dueAt": {"sort": "asc", "nulls": "last"}}, {"createdAt": "desc"}], take=200,
    )
    projects = await db.project.find_many(where={"userId": user.id, "workspaceId": None, "goalId": id}, order={"createdAt": "desc"})
    habits = await db.habit.find_many(where={"userId": user.id, "goalId": id}, include={"logs": True}, order=[{"isArchived": "asc"}, {"sortOrder": "asc"}])
    events = await db.event.find_many(
        where={"userId": user.id, "workspaceId": None, "goalId": id, "startAt": {"gte": dates.start_of_day(dates._now(), ctx.tz)}}, order={"startAt": "asc"}, take=10
    )
    contributions = await db.transaction.find_many(where={"userId": user.id, "goalId": id}, include={"account": True}, order={"date": "desc"}, take=20)
    completed_tasks = await db.task.count(where={"userId": user.id, "workspaceId": None, "goalId": id, "parentId": None, "status": "COMPLETED"})
    project_tasks = {g["projectId"]: g["_count"]["_all"] for g in await db.task.group_by(by=["projectId"], where={"projectId": {"in": [p.id for p in projects]}}, count={"_all": True})} if projects else {}
    progress = await _project_progress(user.id, [p.id for p in projects])
    now = dates._now()

    def habit_view(h):
        c = compute_habit(
            {"frequency": h.frequency, "daysOfWeek": list(h.daysOfWeek or []), "timesPerWeek": h.timesPerWeek, "targetPerDay": h.targetPerDay,
             "startDate": _dstr(h.startDate), "isArchived": h.isArchived},
            [{"date": _dstr(l.date), "count": l.count} for l in h.logs or []], ctx.today, ctx.week_starts_on,
        )
        base = _pick(h, "id", "name", "icon", "color", "frequency", "daysOfWeek", "timesPerWeek", "targetPerDay", "unit", "startDate", "isArchived")
        return {**base, **{k: c[k] for k in ("doneToday", "todayCount", "isScheduledToday", "currentStreak", "bestStreak", "streakUnit", "overallCompletion")}}

    return {
        **goal,
        "milestones": [milestone_view(m, ctx.today) for m in milestones],
        "tasks": [
            {**_pick(t, "id", "title", "status", "priority", "dueAt", "hasDueTime", "completedAt"),
             "isOverdue": t.status != "COMPLETED" and t.dueAt is not None and dates._utc(t.dueAt) < now}
            for t in tasks
        ],
        "taskStats": {"total": len(tasks), "completed": completed_tasks},
        "projects": [
            {**_pick(p, "id", "name", "status", "color", "icon", "deadline", "progressOverride"), "taskCount": project_tasks.get(p.id, 0),
             "progress": p.progressOverride if p.progressOverride is not None else progress.get(p.id, 0)}
            for p in projects
        ],
        "habits": [habit_view(h) for h in habits],
        "events": [_pick(e, "id", "title", "startAt", "endAt", "allDay") for e in events],
        "recentContributions": [
            {**_pick(t, "id", "type", "title", "amount", "currency", "date", "accountId"), "account": _pick(t.account, "id", "name"),
             "convertedAmount": ctx.fx.convert(t.amount, t.currency, goal["displayCurrency"]) if goal["displayCurrency"] else None}
            for t in contributions
        ],
    }


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await _detail(user, id)


def _assert_dates(start, deadline) -> None:
    if start and deadline and dates._utc(deadline) < dates._utc(start):
        raise nest_error(400, "deadline must be on or after startDate")


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    await assert_owned("category", body.categoryId, user.id)
    await assert_owned("account", body.linkedAccountId, user.id)
    settings = await ensure_os_profile(user)
    kind = body.type or "PERSONAL"
    if body.linkedAccountId and kind != "FINANCIAL":
        raise nest_error(400, "Only financial goals can be linked to an account")
    start = dates.parse_date_only(body.startDate) if body.startDate else dates.today_date_only(settings.timezone)
    deadline = dates.parse_date_only(body.deadline)
    _assert_dates(start, deadline)
    currency = body.currency
    if not currency and kind == "FINANCIAL":
        account = await db.account.find_first(where={"id": body.linkedAccountId, "userId": user.id}) if body.linkedAccountId else None
        currency = account.currency if account else settings.defaultCurrency
    status = body.status or "ACTIVE"
    row = await db.goal.create(
        data={
            "userId": user.id, "name": body.name, "description": body.description, "notes": body.notes, "type": kind, "status": status,
            "targetValue": None if body.targetValue is None else as_decimal(body.targetValue),
            "currentValue": as_decimal(body.currentValue if body.currentValue is not None else 0),
            "unit": body.unit, "currency": currency, "startDate": start, "deadline": deadline,
            "linkedAccountId": body.linkedAccountId, "categoryId": body.categoryId, "color": body.color, "icon": body.icon,
            "completedAt": datetime.now(timezone.utc) if status == "COMPLETED" else None,
        }
    )
    await log_activity(user.id, "goal.created", entity_id=row.id, name=row.name, type=kind)
    return await _detail(user, row.id)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await assert_owned("category", body.categoryId, user.id)
    await assert_owned("account", body.linkedAccountId, user.id)
    sent = body.model_fields_set
    kind = body.type or existing.type
    linked = body.linkedAccountId if "linkedAccountId" in sent else existing.linkedAccountId
    if linked and kind != "FINANCIAL":
        raise nest_error(400, "Only financial goals can be linked to an account")
    start = dates.parse_date_only(body.startDate) if "startDate" in sent else existing.startDate
    deadline = dates.parse_date_only(body.deadline) if "deadline" in sent else existing.deadline
    _assert_dates(start, deadline)
    if any(f in sent and getattr(body, f) is None for f in ("name", "type", "status", "currentValue")):
        raise invalid_data()  # Prisma refused null on a required column
    data: dict = {}
    for field in ("name", "description", "notes", "type", "status", "unit", "linkedAccountId", "categoryId", "color", "icon"):
        if field in sent:
            data[field] = getattr(body, field)
    if "currency" in sent:
        data["currency"] = body.currency
    if kind == "FINANCIAL" and not (body.currency or existing.currency):
        data["currency"] = (await ensure_os_profile(user)).defaultCurrency
    for field in ("targetValue", "currentValue"):
        if field in sent:
            data[field] = None if getattr(body, field) is None else as_decimal(getattr(body, field))
    if "startDate" in sent:
        data["startDate"] = start
    if "deadline" in sent:
        data["deadline"] = deadline
    completed_at = "unchanged"
    if body.status and body.status != existing.status:
        completed_at = datetime.now(timezone.utc) if body.status == "COMPLETED" else None
        data["completedAt"] = completed_at
    await db.goal.update(where={"id": existing.id}, data=data or {"name": existing.name})
    name = body.name or existing.name
    if isinstance(completed_at, datetime):
        await log_activity(user.id, "goal.completed", entity_id=id, name=name)
    elif completed_at is None and existing.status == "COMPLETED":
        await log_activity(user.id, "goal.reopened", entity_id=id, name=name)
    else:
        await log_activity(user.id, "goal.updated", entity_id=id)
    return await _detail(user, id)


@router.post("/{id}/progress")
async def progress(id: str, body: ProgressBody, user=Depends(plus_user)):
    has_delta, has_value = body.delta is not None, body.value is not None
    if has_delta == has_value:
        raise nest_error(400, 'Send exactly one of "delta" or "value"')
    row = await _owned(user.id, id)
    if row.linkedAccountId:
        raise nest_error(400, "This goal follows its linked account balance — add a transaction to that account instead")
    (goal,) = await views(user.id, [row], await _ctx(user), sync_completion=False)
    # `current` = manual + contributions (FINANCIAL) or manual: only the manual part is stored
    offset = goal["current"] - goal["manualValue"]
    new_total = max(0, _round2(body.value if has_value else goal["current"] + body.delta))
    new_manual = _round2(new_total - offset)
    data: dict = {"currentValue": as_decimal(new_manual)}
    target = goal["target"]
    completed = reopened = False
    now = datetime.now(timezone.utc)
    if target is not None and target > 0:
        if new_total >= target and row.status != "COMPLETED":
            data.update(status="COMPLETED", completedAt=now)
            completed = True
        elif new_total < target and row.status == "COMPLETED":
            data.update(status="ACTIVE", completedAt=None)
            reopened = True
    await db.goal.update(where={"id": row.id}, data=data)
    # value milestones this progress reached are ticked
    await db.goalmilestone.update_many(
        where={"goalId": row.id, "goal": {"is": {"userId": user.id}}, "completedAt": None, "targetValue": {"not": None, "lte": as_decimal(new_total)}},
        data={"completedAt": now},
    )
    await log_activity(user.id, "goal.progressed", entity_id=row.id, name=row.name, **{"from": goal["current"]}, to=new_total, delta=_round2(new_total - goal["current"]))
    if completed:
        await log_activity(user.id, "goal.completed", entity_id=row.id, name=row.name)
    if reopened:
        await log_activity(user.id, "goal.reopened", entity_id=row.id, name=row.name)
    return await _detail(user, row.id)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.goal.delete(where={"id": existing.id})
    await log_activity(user.id, "goal.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


# ----- milestones ----------------------------------------------------------------------------


class _MilestoneFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr | None = None
    targetValue: object = None
    dueDate: object = None
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("targetValue")
    @classmethod
    def _target(cls, v):
        return _amount(v, "targetValue", 0)

    @field_validator("dueDate")
    @classmethod
    def _due(cls, v):
        if v is not None and (problems := _date_problems(v, "dueDate")):
            raise ValueError("\n".join(problems))
        return v


class MilestoneCreate(_MilestoneFields):
    title: StrictStr


class MilestoneUpdate(_MilestoneFields):
    completed: StrictBool | None = None


class ToggleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: StrictBool | None = None


class MilestoneOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1)
    sortOrder: StrictInt = Field(ge=0)


class MilestoneReorder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MilestoneOrder] = Field(min_length=1, max_length=500)


async def _today(user) -> str:
    settings = await ensure_os_profile(user)
    return dates.local_date_string(dates._now(), settings.timezone)


async def _milestone(user_id: str, goal_id: str, id: str):
    m = await db.goalmilestone.find_first(where={"id": id, "goalId": goal_id, "goal": {"is": {"userId": user_id}}})
    if m is None:
        raise nest_error(404, "Milestone not found")
    return m


async def _log_completion(user_id: str, goal_id: str, before, after, m) -> None:
    if not before and after:
        await log_activity(user_id, "milestone.completed", entity_id=goal_id, milestoneId=m.id, title=m.title)
    elif before and not after:
        await log_activity(user_id, "milestone.reopened", entity_id=goal_id, milestoneId=m.id, title=m.title)
    else:
        await log_activity(user_id, "milestone.updated", entity_id=goal_id, milestoneId=m.id)


@router.get("/{goal_id}/milestones")
async def list_milestones(goal_id: str, user=Depends(plus_user)):
    await assert_owned("goal", goal_id, user.id)
    today = await _today(user)
    rows = await db.goalmilestone.find_many(where={"goalId": goal_id, "goal": {"is": {"userId": user.id}}}, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    return {"items": [milestone_view(m, today) for m in rows], "total": len(rows)}


@router.post("/{goal_id}/milestones/reorder")
async def reorder_milestones(goal_id: str, body: MilestoneReorder, user=Depends(plus_user)):
    await assert_owned("goal", goal_id, user.id)
    ids = [i.id for i in body.items]
    count = await db.goalmilestone.count(where={"id": {"in": ids}, "goalId": goal_id, "goal": {"is": {"userId": user.id}}})
    if count != len(set(ids)):
        raise nest_error(404, "Milestone not found")
    async with db.batch_() as batch:
        for item in body.items:
            batch.goalmilestone.update_many(where={"id": item.id, "goalId": goal_id}, data={"sortOrder": item.sortOrder})
    return {"ok": True}


@router.post("/{goal_id}/milestones", status_code=201)
async def create_milestone(goal_id: str, body: MilestoneCreate, user=Depends(plus_user)):
    await assert_owned("goal", goal_id, user.id)
    sort_order = body.sortOrder
    if sort_order is None:
        top = await db.goalmilestone.find_first(where={"goalId": goal_id}, order={"sortOrder": "desc"})
        sort_order = (top.sortOrder if top else -1) + 1
    m = await db.goalmilestone.create(
        data={"goalId": goal_id, "title": body.title, "targetValue": None if body.targetValue is None else as_decimal(body.targetValue),
              "dueDate": dates.parse_date_only(body.dueDate), "sortOrder": sort_order}
    )
    await log_activity(user.id, "milestone.created", entity_id=goal_id, milestoneId=m.id, title=m.title)
    return milestone_view(m, await _today(user))


@router.patch("/{goal_id}/milestones/{id}")
async def update_milestone(goal_id: str, id: str, body: MilestoneUpdate, user=Depends(plus_user)):
    existing = await _milestone(user.id, goal_id, id)
    sent = body.model_fields_set
    if any(f in sent and getattr(body, f) is None for f in ("title", "sortOrder")):
        raise invalid_data()
    data: dict = {}
    if "title" in sent:
        data["title"] = body.title
    if "targetValue" in sent:
        data["targetValue"] = None if body.targetValue is None else as_decimal(body.targetValue)
    if "dueDate" in sent:
        data["dueDate"] = dates.parse_date_only(body.dueDate)
    if "sortOrder" in sent:
        data["sortOrder"] = body.sortOrder
    if "completed" in sent:  # null reopens it, as NestJS read it
        data["completedAt"] = (existing.completedAt or datetime.now(timezone.utc)) if body.completed else None
    m = await db.goalmilestone.update(where={"id": existing.id}, data=data or {"title": existing.title})
    await _log_completion(user.id, goal_id, existing.completedAt, m.completedAt, m)
    return milestone_view(m, await _today(user))


@router.post("/{goal_id}/milestones/{id}/toggle")
async def toggle_milestone(goal_id: str, id: str, body: ToggleBody, user=Depends(plus_user)):
    existing = await _milestone(user.id, goal_id, id)
    complete = body.completed if body.completed is not None else existing.completedAt is None
    m = await db.goalmilestone.update(
        where={"id": existing.id}, data={"completedAt": (existing.completedAt or datetime.now(timezone.utc)) if complete else None}
    )
    await _log_completion(user.id, goal_id, existing.completedAt, m.completedAt, m)
    return milestone_view(m, await _today(user))


@router.delete("/{goal_id}/milestones/{id}")
async def remove_milestone(goal_id: str, id: str, user=Depends(plus_user)):
    existing = await _milestone(user.id, goal_id, id)
    await db.goalmilestone.delete(where={"id": existing.id})
    await log_activity(user.id, "milestone.deleted", entity_id=goal_id, milestoneId=id, title=existing.title)
    return {"ok": True}
