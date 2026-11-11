"""/api/projects (port of vournalOS ProjectsService). Progress, task counts, spending
against the budget and the next deadline are computed for every project in a few
grouped queries."""

import math
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import attachments
from app.os import finance_utils as fu
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.currency import get_converter, round_money, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.space import Space, assert_personal_links_allowed, current_space
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.jsparse import _JS_SPACE, js_sum
from app.os.common.ownership import assert_owned
from app.os.common.query import _query_bool, _query_list, unknown_params
from app.os.tasks import LIFE_AREAS, overdue_where, task_ctx, task_views
from app.os.tasks import _INCLUDE as TASK_INCLUDE
from app.os.transactions import as_decimal

router = nest_router("/projects")

STATUSES = ("PLANNING", "ACTIVE", "ON_HOLD", "COMPLETED", "CANCELLED")
SORTS = ("deadline", "name", "createdAt", "updatedAt")
StatusName = Literal[STATUSES]  # type: ignore[valid-type]
LifeAreaName = Literal[LIFE_AREAS]  # type: ignore[valid-type]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
_FIELDS = (
    "id", "name", "description", "notes", "status", "lifeArea", "color", "icon", "startDate", "deadline", "budget",
    "currency", "progressOverride", "categoryId", "goalId", "completedAt", "createdAt", "updatedAt",
)
_INCLUDE = {"category": True, "goal": True}


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _pick(row, *fields):
    return None if row is None else {f: getattr(row, f) for f in fields}


async def _ctx(user):
    ctx = await task_ctx(user)  # tz, lang, now, start_of_today
    ctx.fx = await get_converter(user.id)
    return ctx


async def with_computed(user_id: str, rows, ctx) -> list[dict]:
    """Progress, task counts, spending and deadlines for many projects at once."""
    if not rows:
        return []
    ids = [r.id for r in rows]
    # a project's tasks are in its space (links never cross spaces), so they're found by project
    mine = {"projectId": {"in": ids}}
    status_groups = await db.task.group_by(by=["projectId", "status"], where=mine, count={"_all": True})
    overdue_groups = await db.task.group_by(
        by=["projectId"], where={"AND": [mine, overdue_where(ctx.now, ctx.start_of_today)]}, count={"_all": True}
    )
    next_groups = await db.task.group_by(
        by=["projectId"], where={**mine, "status": {"not": "COMPLETED"}, "dueAt": {"gte": ctx.start_of_today}}, min={"dueAt": True}
    )
    spend_groups = await db.transaction.group_by(by=["projectId", "currency"], where={**mine, "userId": user_id, "type": "EXPENSE"}, sum={"amount": True})

    # the task behind each project's earliest upcoming due date
    pairs = [(g["projectId"], fu.group_instant(g["_min"]["dueAt"])) for g in next_groups if g.get("projectId") and g["_min"]["dueAt"]]
    next_tasks = (
        await db.task.find_many(
            where={"status": {"not": "COMPLETED"}, "OR": [{"projectId": p, "dueAt": d} for p, d in pairs]},
            order=[{"parentId": {"sort": "asc", "nulls": "first"}}, {"priority": "desc"}],
        )
        if pairs
        else []
    )

    today = dates.today_date_only(ctx.tz)
    out = []
    for p in rows:
        total = completed = in_progress = 0
        for g in status_groups:
            if g["projectId"] != p.id:
                continue
            n = g["_count"]["_all"]
            total += n
            completed += n if g["status"] == "COMPLETED" else 0
            in_progress += n if g["status"] == "IN_PROGRESS" else 0
        overdue = next((g["_count"]["_all"] for g in overdue_groups if g["projectId"] == p.id), 0)
        auto = _js_round(completed / total * 100) if total else 0
        progress = p.progressOverride if p.progressOverride is not None else (100 if p.status == "COMPLETED" and total == 0 else auto)

        spent_currency = p.currency or ctx.fx.default_currency
        spent = round_money(js_sum(
            ctx.fx.convert(fu.group_decimal(g["_sum"]["amount"]), g["currency"], spent_currency) for g in spend_groups if g["projectId"] == p.id
        ))
        budget = None if p.budget is None else to_number(p.budget)
        if budget is None:
            remaining = used = None
        else:
            remaining = round_money(budget - spent)
            used = _js_round(spent / budget * 100) if budget > 0 else (100 if spent > 0 else 0)

        task_row = next((t for t in next_tasks if t.projectId == p.id), None)
        next_task = {"id": task_row.id, "title": task_row.title, "dueAt": task_row.dueAt} if task_row else None
        done = p.status in ("COMPLETED", "CANCELLED")
        upcoming_deadline = p.deadline if p.deadline and dates._utc(p.deadline) >= today else None
        candidates = [dates._utc(d) for d in (next_task["dueAt"] if next_task else None, upcoming_deadline) if d]
        days_left = dates.days_until(p.deadline, ctx.tz) if p.deadline else None
        view = {f: getattr(p, f) for f in _FIELDS}
        view.update(
            category=_pick(p.category, "id", "name", "color", "icon"),
            goal=_pick(p.goal, "id", "name"),
            progress=progress,
            autoProgress=auto,
            taskCounts={"total": total, "completed": completed, "open": total - completed, "inProgress": in_progress, "overdue": overdue},
            spent=spent,
            spentCurrency=spent_currency,
            budgetRemaining=remaining,
            budgetUsedPercent=used,
            isOverBudget=budget is not None and spent > budget,
            nextDeadline=None if done or not candidates else min(candidates),
            nextTask=next_task,
            daysUntilDeadline=days_left,
            isOverdue=not done and days_left is not None and days_left < 0,
        )
        out.append(view)
    return out


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=5000)
    notes: StrictStr | None = Field(default=None, max_length=20000)
    status: StatusName | None = None
    lifeArea: LifeAreaName | None = None
    color: StrictStr | None = None
    icon: StrictStr | None = None
    startDate: StrictStr | None = None
    deadline: StrictStr | None = None
    budget: object = None
    currency: CurrencyCode | None = None
    progressOverride: StrictInt | None = Field(default=None, ge=0, le=100)
    categoryId: StrictStr | None = None
    goalId: StrictStr | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

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

    @field_validator("startDate", "deadline")
    @classmethod
    def _dates(cls, v, info):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError(f"{info.field_name} must be a valid ISO 8601 date string")
        return v

    @field_validator("budget")
    @classmethod
    def _budget(cls, v):
        return validators.money(v, "budget", 0)


class CreateBody(_Fields):
    name: StrictStr


class UpdateBody(_Fields):
    pass


async def _assert_links(space: Space, body: _Fields) -> None:
    assert_personal_links_allowed(space, body)
    await assert_owned("category", body.categoryId, space.user.id)
    await assert_owned("goal", body.goalId, space.user.id)


async def _owned(space: Space, id: str):
    row = await db.project.find_first(where={"id": id, **space.where})
    if row is None:
        raise nest_error(404, "Project not found")
    return row


_LIST_QUERY = ("status", "goalId", "lifeArea", "categoryId", "q", "sort")  # the NestJS query DTO's fields


@router.get("")
async def list_projects(request: Request, space: Space = Depends(current_space)):
    user = space.user
    q = request.query_params
    errors = unknown_params(request, _LIST_QUERY)
    statuses = _query_list(request, "status", STATUSES, errors)
    areas = _query_list(request, "lifeArea", LIFE_AREAS, errors)
    text = q.get("q")
    text = text.strip(_JS_SPACE) if text is not None else None
    if text is not None and len(text) > 200:
        errors.append("q must be shorter than or equal to 200 characters")
    sort = q.get("sort")
    if sort is not None and sort not in SORTS:
        errors.append(f"sort must be one of the following values: {', '.join(SORTS)}")
    if errors:
        raise nest_error(400, errors)
    where: dict = dict(space.where)
    if statuses:
        where["status"] = {"in": statuses}
    if areas:
        where["lifeArea"] = {"in": areas}
    for field in ("goalId", "categoryId"):
        if q.get(field):
            where[field] = q.get(field)
    if text:
        where["OR"] = [{"name": {"contains": text, "mode": "insensitive"}}, {"description": {"contains": text, "mode": "insensitive"}}]
    order = {
        "name": [{"name": "asc"}],
        "createdAt": [{"createdAt": "desc"}],
        "updatedAt": [{"updatedAt": "desc"}],
    }.get(sort, [{"deadline": {"sort": "asc", "nulls": "last"}}, {"createdAt": "desc"}])
    rows = await db.project.find_many(where=where, include=_INCLUDE, order=order)
    items = await with_computed(user.id, rows, await _ctx(user))
    return {"items": items, "total": len(items)}


@router.get("/{id}")
async def get(id: str, space: Space = Depends(current_space)):
    user = space.user
    ctx = await _ctx(user)
    row = await db.project.find_first(where={"id": id, **space.where}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Project not found")
    (view,) = await with_computed(user.id, [row], ctx)
    top = {"projectId": id, "parentId": None}
    by_status = await db.task.group_by(by=["status"], where=top, count={"_all": True})
    upcoming_rows = await db.task.find_many(
        where={**top, "status": {"not": "COMPLETED"}, "dueAt": {"not": None}},
        include=TASK_INCLUDE, order=[{"dueAt": "asc"}, {"priority": "desc"}], take=5,
    )
    events = await db.event.find_many(where={"projectId": id, "workspaceId": row.workspaceId}, order={"startAt": "asc"}, take=200)
    notes = await db.note.find_many(where={"projectId": id, "workspaceId": row.workspaceId}, order=[{"isPinned": "desc"}, {"updatedAt": "desc"}], take=100)
    transactions = await db.transaction.find_many(
        where={"userId": user.id, "projectId": id}, include={"category": True}, order={"date": "desc"}, take=20
    )
    attachment_count = await db.attachment.count(where={"entityType": "PROJECT", "entityId": id})

    def ends(e):
        return dates._utc(e.endAt or e.startAt)

    # upcoming first (soonest), then past (most recent first)
    upcoming_events = [e for e in events if ends(e) >= ctx.now]
    past_events = [e for e in events if ends(e) < ctx.now][::-1]
    count = lambda status: next((g["_count"]["_all"] for g in by_status if g["status"] == status), 0)  # noqa: E731
    return {
        **view,
        "taskSummary": {
            "topLevel": {"todo": count("TODO"), "inProgress": count("IN_PROGRESS"), "completed": count("COMPLETED")},
            "upcoming": await task_views(user.id, upcoming_rows, ctx),
        },
        "events": [
            {**_pick(e, "id", "title", "startAt", "endAt", "allDay", "type", "location", "color", "rrule"), "isPast": ends(e) < ctx.now}
            for e in [*upcoming_events, *past_events]
        ],
        # `notes` stays the project's own markdown brief; notes linked to it are `linkedNotes`
        "linkedNotes": [_pick(n, "id", "title", "updatedAt", "isPinned", "isArchived") for n in notes],
        "transactions": [
            {**_pick(t, "id", "type", "title", "amount", "currency", "date"), "category": _pick(t.category, "id", "name", "color", "icon")}
            for t in transactions
        ],
        "attachmentCount": attachment_count,
    }


@router.post("", status_code=201)
async def create(body: CreateBody, space: Space = Depends(current_space)):
    user = space.user
    await _assert_links(space, body)
    ctx = await _ctx(user)
    status = body.status or "ACTIVE"
    row = await db.project.create(
        data={
            **space.create_fields(),
            "name": body.name,
            "description": body.description,
            "notes": body.notes,
            "status": status,
            "lifeArea": body.lifeArea or (None if space.personal else "WORK"),
            "color": body.color,
            "icon": body.icon,
            "startDate": dates.parse_date_only(body.startDate),
            "deadline": dates.parse_date_only(body.deadline),
            "budget": None if body.budget is None else as_decimal(body.budget),
            "currency": body.currency or (ctx.fx.default_currency if body.budget is not None else None),
            "progressOverride": body.progressOverride,
            "categoryId": body.categoryId,
            "goalId": body.goalId,
            "completedAt": ctx.now if status == "COMPLETED" else None,
        },
        include=_INCLUDE,
    )
    await log_activity(user.id, "project.created", entity_id=row.id, name=row.name)
    (view,) = await with_computed(user.id, [row], ctx)
    return view


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, space: Space = Depends(current_space)):
    user = space.user
    existing = await _owned(space, id)
    await _assert_links(space, body)
    ctx = await _ctx(user)
    sent = body.model_fields_set
    if any(f in sent and getattr(body, f) is None for f in ("name", "status")):
        raise invalid_data()  # Prisma refused null on a required column
    data: dict = {}
    for field in ("name", "description", "notes", "status", "lifeArea", "color", "icon", "currency", "progressOverride", "categoryId", "goalId"):
        if field in sent:
            data[field] = getattr(body, field)
    if "budget" in sent:
        data["budget"] = None if body.budget is None else as_decimal(body.budget)
    if "startDate" in sent:
        data["startDate"] = dates.parse_date_only(body.startDate)
    if "deadline" in sent:
        data["deadline"] = dates.parse_date_only(body.deadline)
    if body.budget is not None and "currency" not in sent and not existing.currency:
        data["currency"] = ctx.fx.default_currency
    became_completed = body.status == "COMPLETED" and existing.status != "COMPLETED"
    left_completed = body.status is not None and body.status != "COMPLETED" and existing.status == "COMPLETED"
    if became_completed:
        data["completedAt"] = ctx.now
    if left_completed:
        data["completedAt"] = None
    row = await db.project.update(where={"id": existing.id}, data=data or {"name": existing.name}, include=_INCLUDE)
    await log_activity(user.id, "project.updated", entity_id=id, name=row.name)
    if became_completed:
        await log_activity(user.id, "project.completed", entity_id=id, name=row.name)
    (view,) = await with_computed(user.id, [row], ctx)
    return view


@router.delete("/{id}")
async def remove(id: str, request: Request, space: Space = Depends(current_space)):
    user = space.user
    errors = unknown_params(request, ("deleteTasks",))
    delete_tasks = _query_bool(request, "deleteTasks", errors)
    if errors:
        raise nest_error(400, errors)
    existing = await _owned(space, id)
    if not space.can_delete(existing):
        raise nest_error(404, "Project not found")
    async with db.tx() as tx:
        keys = await attachments.delete_rows(tx, None, "PROJECT", [id])
        if delete_tasks:
            task_ids = [t.id for t in await tx.task.find_many(where={"projectId": id})]
            keys += await attachments.delete_rows(tx, None, "TASK", task_ids)
            if task_ids:
                await tx.task.delete_many(where={"id": {"in": task_ids}})
        await tx.project.delete(where={"id": existing.id})
    await attachments.delete_objects(keys)
    await log_activity(user.id, "project.deleted", entity_id=id, name=existing.name, deletedTasks=bool(delete_tasks))
    return {"ok": True}
