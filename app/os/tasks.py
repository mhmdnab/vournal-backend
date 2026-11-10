"""/api/tasks (port of vournalOS TasksService and task-view).

Overdue is derived, never stored: not completed, and due before now (a timed task)
or before the start of the user's today (an all-day task). Completing a repeating
task creates the next occurrence once; reopening and completing again finds it
instead of making another.
"""

import math
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, Request
from prisma import Json
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import attachments
from app.os.common import dates, rrule, validators
from app.os.common.dbnull import DB_NULL, pop_db_nulls, set_db_null
from app.os.common.activity import log_activity
from app.os.common.space import Space, assert_in_space, assert_personal_links_allowed, current_space, member_role
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.ownership import assert_many_owned, assert_owned
from app.os.common.prayer import TimeAnchor, read_anchor, resolve_or_fail, resolver
from app.os.common.query import _query_bool, _query_date, _query_int, _query_list, string_list, unknown_params
from app.os.profile import ensure_os_profile

log = logging.getLogger("vournal.os")
router = nest_router("/tasks")

Status = Literal["TODO", "IN_PROGRESS", "COMPLETED"]
PriorityName = Literal["LOW", "MEDIUM", "HIGH", "URGENT"]
LifeAreaName = Literal["PERSONAL", "WORK", "STUDY", "HOME", "HEALTH", "FINANCE", "SOCIAL", "OTHER"]
PRIORITIES = ("LOW", "MEDIUM", "HIGH", "URGENT")
LIFE_AREAS = ("PERSONAL", "WORK", "STUDY", "HOME", "HEALTH", "FINANCE", "SOCIAL", "OTHER")
TASK_SORTS = ("dueAt", "priority", "createdAt", "position", "updatedAt", "completedAt", "title")
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 50, 200

_FIELDS = (
    "id", "title", "description", "notes", "status", "priority", "lifeArea", "categoryId", "dueAt",
    "hasDueTime", "timeAnchor", "reminderAt", "estimatedMinutes", "rrule", "recurrenceEndAt",
    "recurrenceParentId", "position", "completedAt", "focusDate", "parentId", "projectId", "goalId",
    "courseId", "tripId", "createdAt", "updatedAt",
)
_SUBTASK_FIELDS = ("id", "title", "status", "priority", "position", "dueAt", "hasDueTime", "timeAnchor", "completedAt", "createdAt")
_EVENT_FIELDS = ("id", "title", "startAt", "endAt", "allDay", "type", "color")
_INCLUDE = {
    "category": True,
    "tags": {"order_by": {"name": "asc"}},
    "project": True,
    "goal": True,
    "course": True,
    "trip": True,
}


class Ctx:
    def __init__(self, tz: str, lang: str):
        self.tz = tz
        self.lang = lang
        self.now = datetime.now(timezone.utc)
        self.start_of_today = dates.today(tz)


async def task_ctx(user) -> Ctx:
    settings = await ensure_os_profile(user)
    return Ctx(dates.valid_zone(settings.timezone), to_lang(settings.language))


# ---------------------------------------------------------------------------
# Views


def is_overdue(task, now: datetime, start_of_today: datetime) -> bool:
    if task.status == "COMPLETED" or task.dueAt is None:
        return False
    due = dates._utc(task.dueAt)
    return due < now if task.hasDueTime else due < start_of_today


def overdue_where(now: datetime, start_of_today: datetime) -> dict:
    return {
        "status": {"not": "COMPLETED"},
        "OR": [
            {"hasDueTime": True, "dueAt": {"lt": now}},
            {"hasDueTime": False, "dueAt": {"lt": start_of_today}},
        ],
    }


def _pick(row, *fields) -> dict | None:
    return None if row is None else {f: getattr(row, f) for f in fields}


async def people(user_ids) -> dict:
    """{userId: {id, name, avatarColor}} for the assignees and creators a view shows."""
    ids = [i for i in dict.fromkeys(user_ids) if i]
    if not ids:
        return {}
    profiles = {s.userId: s for s in await db.usersettings.find_many(where={"userId": {"in": ids}})}
    return {
        i: {"id": i, "name": profiles[i].name if i in profiles else None, "avatarColor": profiles[i].avatarColor if i in profiles else None}
        for i in ids
    }


async def task_views(user_id: str, rows, ctx: Ctx) -> list[dict]:
    """List shape: the row, its links, overdue, subtask counts, isRecurring, who it's assigned
    to and who added it. (Subtasks share their parent's space, so they're found by parent.)"""
    counts: dict[str, list[int]] = {}
    who = await people([r.assigneeId for r in rows] + [r.createdById for r in rows])
    if rows:
        children = await db.task.find_many(where={"parentId": {"in": [r.id for r in rows]}})
        for child in children:
            total_done = counts.setdefault(child.parentId, [0, 0])
            total_done[0] += 1
            total_done[1] += child.status == "COMPLETED"
    out = []
    for r in rows:
        total, done = counts.get(r.id, (0, 0))
        view = {f: getattr(r, f) for f in _FIELDS}
        view.update(
            category=_pick(r.category, "id", "name", "color", "icon"),
            tags=[_pick(t, "id", "name", "color") for t in (r.tags or [])],
            project=_pick(r.project, "id", "name", "color"),
            goal=_pick(r.goal, "id", "name"),
            course=_pick(r.course, "id", "name"),
            trip=_pick(r.trip, "id", "name"),
            isOverdue=is_overdue(r, ctx.now, ctx.start_of_today),
            subtaskCount=total,
            subtaskDoneCount=done,
            isRecurring=bool(r.rrule),
            workspaceId=r.workspaceId,
            assignee=who.get(r.assigneeId),
            createdBy=who.get(r.createdById),
        )
        out.append(view)
    return out


async def _detail(user_id: str, row, ctx: Ctx) -> dict:
    [view] = await task_views(user_id, [row], ctx)
    subtasks = await db.task.find_many(
        where={"parentId": row.id}, order=[{"position": "asc"}, {"createdAt": "asc"}]
    )
    events = await db.event.find_many(where={"taskId": row.id, "workspaceId": row.workspaceId}, order={"startAt": "asc"})
    parent = (
        await db.task.find_first(where={"id": row.parentId, "workspaceId": row.workspaceId}) if row.parentId else None
    )
    description = None
    if row.rrule:
        try:
            description = rrule.describe(row.rrule, ctx.lang)
        except Exception:  # noqa: BLE001
            description = None
    return {
        **view,
        "recurrenceDescription": description,
        "parent": _pick(parent, "id", "title"),
        "subtasks": [
            {**_pick(s, *_SUBTASK_FIELDS), "isOverdue": is_overdue(s, ctx.now, ctx.start_of_today)} for s in subtasks
        ],
        "events": [_pick(e, *_EVENT_FIELDS) for e in events],
    }


async def _get(space: Space, id: str, ctx: Ctx) -> dict:
    row = await db.task.find_first(where={"id": id, **space.where}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Task not found")
    return await _detail(space.user.id, row, ctx)


# ---------------------------------------------------------------------------
# Bodies


def _iso_or_none(value, field: str):
    if value is not None and not dates.is_iso_date_string(value):
        raise ValueError(f"{field} must be a valid ISO 8601 date string")
    return value


class InlineSubtask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr
    priority: PriorityName | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)


class _TaskFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=5000)
    notes: StrictStr | None = Field(default=None, max_length=20000)
    status: Status | None = None
    priority: PriorityName | None = None
    lifeArea: LifeAreaName | None = None
    categoryId: StrictStr | None = None
    dueAt: StrictStr | None = None
    hasDueTime: StrictBool | None = None
    reminderAt: StrictStr | None = None
    estimatedMinutes: StrictInt | None = Field(default=None, ge=0, le=100000)
    rrule: StrictStr | None = None
    recurrenceEndAt: StrictStr | None = None
    parentId: StrictStr | None = None
    projectId: StrictStr | None = None
    goalId: StrictStr | None = None
    courseId: StrictStr | None = None
    tripId: StrictStr | None = None
    tagIds: list[StrictStr] | None = Field(default=None, max_length=50)
    position: StrictInt | StrictFloat | None = None
    timeAnchor: TimeAnchor | None = None
    # workspaces: a member of the space (Personal: only yourself)
    assigneeId: StrictStr | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("dueAt", "reminderAt", "recurrenceEndAt")
    @classmethod
    def _dates(cls, v, info):
        return _iso_or_none(v, info.field_name)

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v):
        problem = None if v is None else rrule.validate_rrule(v)
        if problem:
            raise ValueError(f"rrule {problem}")
        return v


class CreateBody(_TaskFields):
    title: StrictStr
    subtasks: list[InlineSubtask] | None = Field(default=None, max_length=100)

    @field_validator("title")
    @classmethod
    def _required_title(cls, v):
        return validators.trimmed(v, "title", 1, 200)


class UpdateBody(_TaskFields):
    pass


class ReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1)
    status: Status | None = None
    position: StrictInt | StrictFloat


class ReorderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReorderItem] = Field(min_length=1, max_length=500)


class FocusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: StrictStr | None = None
    taskIds: list[StrictStr] = Field(max_length=3)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _iso_or_none(v, "date")


# ---------------------------------------------------------------------------
# Helpers


def _parse_due(value, has_due_time, tz: str):
    """(dueAt, hasDueTime) from a request: "YYYY-MM-DD" is all-day at local midnight;
    an instant is timed unless it falls exactly on local midnight."""
    if not value:
        return None
    if dates.DATE_ONLY_RE.match(value):
        return dates.local_midnight(value, tz), False
    due = dates.parse_instant(value)
    if due is None:
        raise nest_error(400, "dueAt must be a valid date")
    if has_due_time is not None:
        return due, has_due_time
    return due, dates.start_of_day(due, tz) != due


def _parse_end(value, tz: str):
    """recurrenceEndAt: "YYYY-MM-DD" → the end of that local day."""
    if not value:
        return None
    if dates.DATE_ONLY_RE.match(value):
        return dates.end_of_day(dates.local_midnight(value, tz), tz)
    return dates.parse_instant(value)


def _boundary(value: str, tz: str, edge: str) -> datetime:
    if dates.DATE_ONLY_RE.match(value):
        start = dates.local_midnight(value, tz)
        return start if edge == "start" else dates.end_of_day(start, tz)
    moment = dates.parse_instant(value)
    if moment is None:
        raise nest_error(400, "Invalid date")
    return moment


def _transition(prev: str, nxt: str | None) -> str | None:
    if not nxt or nxt == prev:
        return None
    if nxt == "COMPLETED":
        return "completed"
    return "reopened" if prev == "COMPLETED" else None


async def _owned(space: Space, id: str):
    row = await db.task.find_first(where={"id": id, **space.where}, include={"tags": True})
    if row is None:
        raise nest_error(404, "Task not found")
    return row


async def _assert_links(space: Space, body: _TaskFields) -> None:
    # categories, tags, goals, courses and trips are personal; a project is in the same space
    assert_personal_links_allowed(space, body)
    for model, field in (("category", "categoryId"), ("goal", "goalId"), ("course", "courseId"), ("trip", "tripId")):
        await assert_owned(model, getattr(body, field), space.user.id)
    await assert_many_owned("tag", body.tagIds, space.user.id)
    await assert_in_space("project", body.projectId, space)
    await _assert_assignee(space, body.assigneeId)


async def _assert_assignee(space: Space, assignee_id: str | None) -> None:
    if assignee_id is None:
        return
    ok = assignee_id == space.user.id if space.personal else await member_role(space.workspace_id, assignee_id) is not None
    if not ok:
        raise nest_error(404, "User not found")


async def _notify_assigned(space: Space, task_id: str, title: str, assignee_id: str | None) -> None:
    """In-app notification and web push for whoever was just given a task (not yourself)."""
    if assignee_id is None or assignee_id == space.user.id or space.personal:
        return
    from app.os import push

    who = (await people([space.user.id])).get(space.user.id) or {}
    text = f"{who.get('name') or space.user.email} assigned you a task"
    key = f"task:{task_id}:assigned:{datetime.now(timezone.utc).isoformat()}"
    row = await db.notification.upsert(
        where={"userId_dedupeKey": {"userId": assignee_id, "dedupeKey": key}},
        data={
            "create": {
                "userId": assignee_id, "dedupeKey": key,
                "title": title[:200], "body": text, "link": f"/tasks?id={task_id}&space={space.workspace_id}",
                "entityType": "TASK", "entityId": task_id,
            },
            "update": {},
        },
    )
    try:
        await push.send_to_user(assignee_id, [{"title": row.title, "body": row.body, "link": row.link, "tag": row.id, "id": row.id}])
    except Exception:  # noqa: BLE001 — a push never stops a save
        log.warning("assignment push failed", exc_info=True)


async def _assert_many_in_space(ids: list[str], space: Space) -> None:
    unique = list(dict.fromkeys(ids))
    if unique and await db.task.count(where={"id": {"in": unique}, **space.where}) != len(unique):
        raise nest_error(404, "Task not found")


async def _valid_parent(space: Space, parent_id: str):
    parent = await db.task.find_first(where={"id": parent_id, **space.where})
    if parent is None:
        raise nest_error(404, "Task not found")
    if parent.parentId:
        raise nest_error(400, "Subtasks can only be one level deep")
    return parent


async def _default_position(space: Space, status: str, parent_id: str | None) -> float:
    """New top-level tasks go to the top of their column; subtasks are appended."""
    where = {**space.where, "parentId": parent_id, **({} if parent_id else {"status": status})}
    if parent_id:
        last = await db.task.find_first(where=where, order={"position": "desc"})
        return (last.position if last else -1) + 1
    first = await db.task.find_first(where=where, order={"position": "asc"})
    return (first.position if first else 1) - 1


async def _anchored_due(user_id: str, anchor: dict, day: datetime, tz: str) -> datetime:
    start, _ = await resolve_or_fail(user_id, dates.local_date_string(day, tz), anchor)
    return start


def _order(sort: str | None, direction: str | None) -> list:
    if sort == "priority":
        return [{"priority": direction or "desc"}, {"dueAt": {"sort": "asc", "nulls": "last"}}, {"createdAt": "desc"}]
    if sort == "createdAt":
        return [{"createdAt": direction or "desc"}]
    if sort == "updatedAt":
        return [{"updatedAt": direction or "desc"}]
    if sort == "completedAt":
        return [{"completedAt": {"sort": direction or "desc", "nulls": "last"}}, {"updatedAt": "desc"}]
    if sort == "title":
        return [{"title": direction or "asc"}]
    if sort == "position":
        return [{"position": direction or "asc"}, {"createdAt": "desc"}]
    return [
        {"dueAt": {"sort": direction or "asc", "nulls": "last"}},
        {"priority": "desc"},
        {"position": "asc"},
        {"createdAt": "desc"},
    ]


def _focus_date(value: str | None, tz: str) -> datetime:
    if not value:
        return dates.today_date_only(tz)
    day = dates.parse_date_only(value)
    if day is None:
        raise nest_error(400, "date must be YYYY-MM-DD")
    return day


# ---------------------------------------------------------------------------
# Recurrence


async def _after_completed(space: Space, id: str, ctx: Ctx):
    """Logs task.completed and, for a repeating task, creates the next occurrence."""
    user_id = space.user.id
    task = await db.task.find_first(where={"id": id, **space.where}, include={"tags": True})
    if task is None:
        return None
    nxt = None
    try:
        nxt = await _spawn_next(user_id, task, ctx)
    except Exception as exc:  # noqa: BLE001 — NestJS logged and carried on
        log.warning("Could not create next occurrence for task %s: %s", id, exc)
    await log_activity(
        user_id, "task.completed", entity_id=id, title=task.title, projectId=task.projectId,
        nextOccurrenceId=nxt["id"] if nxt else None,
    )
    return nxt


async def _spawn_next(user_id: str, task, ctx: Ctx):
    if not task.rrule:
        return None
    root_id = task.recurrenceParentId or task.id
    # Anchor on the series root's due date (keeps BYMONTHDAY and time of day stable).
    anchor = task.dueAt
    if task.recurrenceParentId:
        root = await db.task.find_first(where={"id": task.recurrenceParentId, "workspaceId": task.workspaceId})
        if root is not None and root.dueAt:
            anchor = root.dueAt
    has_due = task.dueAt is not None
    anchor = anchor or ctx.start_of_today
    after = task.dueAt or ctx.now
    opts = {"tz": ctx.tz, "until": task.recurrenceEndAt}
    next_due = rrule.next_occurrence(task.rrule, anchor, after, **opts)
    # Catch-up: completing a long-overdue occurrence shouldn't create another overdue one.
    if next_due is not None and next_due < ctx.start_of_today:
        next_due = (
            rrule.next_occurrence(task.rrule, anchor, ctx.start_of_today - timedelta(milliseconds=1), **opts)
            or next_due
        )
    if next_due is None:
        return None
    if task.recurrenceEndAt and next_due > dates._utc(task.recurrenceEndAt):
        return None
    prayer_anchor = read_anchor(task.timeAnchor) if task.hasDueTime else None
    if prayer_anchor:
        resolved = (await resolver(user_id))(dates.local_date_string(next_due, ctx.tz), prayer_anchor)
        if resolved:
            next_due = resolved[0]

    # Completing → reopening → completing must not create duplicates.
    dup = await db.task.find_first(
        where={
            "workspaceId": task.workspaceId,
            "OR": [{"id": root_id}, {"recurrenceParentId": root_id}],
            "dueAt": next_due,
            "NOT": [{"id": task.id}],
        }
    )
    if dup is not None:
        return {"id": dup.id, "dueAt": dup.dueAt}

    reminder_at = (
        next_due - (dates._utc(task.dueAt) - dates._utc(task.reminderAt)) if task.reminderAt and task.dueAt else None
    )
    subtasks = await db.task.find_many(where={"parentId": task.id}, order={"position": "asc"})
    # the next repeat stays where the series is: same owner, space and assignee
    owner = {"userId": task.userId, "createdById": task.createdById, "workspaceId": task.workspaceId}
    async with db.tx() as tx:
        data = {
            **owner,
            "assigneeId": task.assigneeId,
            "title": task.title,
            "description": task.description,
            "notes": task.notes,
            "status": "TODO",
            "priority": task.priority,
            "lifeArea": task.lifeArea,
            "categoryId": task.categoryId,
            "dueAt": next_due,
            "hasDueTime": task.hasDueTime if has_due else False,
            "reminderAt": reminder_at,
            "estimatedMinutes": task.estimatedMinutes,
            "rrule": task.rrule,
            "recurrenceEndAt": task.recurrenceEndAt,
            "recurrenceParentId": root_id,
            "position": task.position,
            "parentId": task.parentId,
            "projectId": task.projectId,
            "goalId": task.goalId,
            "courseId": task.courseId,
            "tripId": task.tripId,
        }
        if prayer_anchor:
            data["timeAnchor"] = Json(prayer_anchor)
        if task.tags:
            data["tags"] = {"connect": [{"id": t.id} for t in task.tags]}
        created = await tx.task.create(data=data)
        if subtasks:
            await tx.task.create_many(
                data=[
                    {
                        **owner,
                        "title": s.title,
                        "description": s.description,
                        "notes": s.notes,
                        "priority": s.priority,
                        "position": s.position,
                        "estimatedMinutes": s.estimatedMinutes,
                        "lifeArea": s.lifeArea,
                        "categoryId": s.categoryId,
                        "status": "TODO",
                        "parentId": created.id,
                        "projectId": task.projectId,
                        "goalId": task.goalId,
                    }
                    for s in subtasks
                ]
            )
    await log_activity(
        user_id, "task.created", entity_id=created.id, title=task.title, recurrenceParentId=root_id,
        projectId=task.projectId,
    )
    return {"id": created.id, "dueAt": created.dueAt}


# ---------------------------------------------------------------------------
# Routes


_LIST_TASKS_QUERY = (  # the NestJS query DTO's fields
    "status", "overdue", "priority", "categoryId", "projectId", "goalId", "courseId", "tripId", "lifeArea",
    "tagId", "dueFrom", "dueTo", "hasDue", "completedFrom", "completedTo", "q", "parentId", "topLevel",
    "sort", "order", "page", "pageSize", "assignee",
)


@router.get("")
async def list_tasks(request: Request, space: Space = Depends(current_space)):
    user = space.user
    ctx = await task_ctx(user)
    errors = unknown_params(request, _LIST_TASKS_QUERY)
    q = request.query_params
    statuses = _query_list(request, "status", ("TODO", "IN_PROGRESS", "COMPLETED", "OVERDUE"), errors)
    overdue = _query_bool(request, "overdue", errors)
    priorities = _query_list(request, "priority", PRIORITIES, errors)
    life_areas = _query_list(request, "lifeArea", LIFE_AREAS, errors)
    has_due = _query_bool(request, "hasDue", errors)
    top_level = _query_bool(request, "topLevel", errors)
    page = _query_int(request, "page", errors)
    page_size = _query_int(request, "pageSize", errors, MAX_PAGE_SIZE)
    due_from, due_to = _query_date(request, "dueFrom", errors), _query_date(request, "dueTo", errors)
    done_from, done_to = _query_date(request, "completedFrom", errors), _query_date(request, "completedTo", errors)
    sort, order = q.get("sort"), q.get("order")
    if sort is not None and sort not in TASK_SORTS:
        errors.append(f"sort must be one of the following values: {', '.join(TASK_SORTS)}")
    if order is not None and order not in ("asc", "desc"):
        errors.append("order must be one of the following values: asc, desc")
    text = q.get("q")
    if text is not None:
        text = text.strip()
        if len(text) > 200:
            errors.append("q must be shorter than or equal to 200 characters")
    if errors:
        raise nest_error(400, errors)

    and_: list[dict] = [space.where]
    if q.get("assignee") == "me":
        and_.append({"assigneeId": user.id})
    overdue_clause = overdue_where(ctx.now, ctx.start_of_today)
    if statuses:
        real = [s for s in statuses if s != "OVERDUE"]
        wants_overdue = "OVERDUE" in statuses
        if wants_overdue and real:
            and_.append({"OR": [{"status": {"in": real}}, overdue_clause]})
        elif wants_overdue:
            and_.append(overdue_clause)
        else:
            and_.append({"status": {"in": real}})
    if overdue is True:
        and_.append(overdue_clause)
    if overdue is False:
        and_.append({"NOT": [overdue_clause]})
    if priorities:
        and_.append({"priority": {"in": priorities}})
    for field in ("categoryId", "projectId", "goalId", "courseId", "tripId"):
        if q.get(field):
            and_.append({field: q.get(field)})
    if life_areas:
        and_.append({"lifeArea": {"in": life_areas}})
    if q.get("tagId"):
        and_.append({"tags": {"some": {"id": q.get("tagId")}}})
    if due_from:
        and_.append({"dueAt": {"gte": _boundary(due_from, ctx.tz, "start")}})
    if due_to:
        and_.append({"dueAt": {"lte": _boundary(due_to, ctx.tz, "end")}})
    if has_due is True:
        and_.append({"dueAt": {"not": None}})
    if has_due is False:
        and_.append({"dueAt": None})
    if done_from:
        and_.append({"completedAt": {"gte": _boundary(done_from, ctx.tz, "start")}})
    if done_to:
        and_.append({"completedAt": {"lte": _boundary(done_to, ctx.tz, "end")}})
    if text:
        and_.append(
            {"OR": [{"title": {"contains": text, "mode": "insensitive"}}, {"description": {"contains": text, "mode": "insensitive"}}]}
        )
    if q.get("parentId"):
        and_.append({"parentId": q.get("parentId")})
    elif top_level:
        and_.append({"parentId": None})

    where = {"AND": and_}
    size = page_size or DEFAULT_PAGE_SIZE
    rows = await db.task.find_many(
        where=where, include=_INCLUDE, order=_order(sort, order), skip=((page or 1) - 1) * size, take=size
    )
    total = await db.task.count(where=where)
    return {"items": await task_views(user.id, rows, ctx), "total": total}


@router.get("/focus")
async def get_focus(request: Request, space: Space = Depends(current_space)):
    user = space.user
    errors = unknown_params(request, ("date",))
    value = request.query_params.get("date")
    if value is not None and not dates.is_iso_date_string(value):
        errors.append("date must be a valid ISO 8601 date string")
    if errors:
        raise nest_error(400, errors)
    ctx = await task_ctx(user)
    return await _focus_view(space, _focus_date(value, ctx.tz), ctx)


async def _focus_view(space: Space, day: datetime, ctx: Ctx) -> dict:
    user_id = space.user.id
    rows = await db.task.find_many(
        where={**space.where, "focusDate": day},
        include=_INCLUDE,
        order=[{"priority": "desc"}, {"dueAt": {"sort": "asc", "nulls": "last"}}, {"createdAt": "asc"}],
    )
    return {"date": dates.format_date_only(day), "items": await task_views(user_id, rows, ctx), "max": 3}


@router.put("/focus")
async def set_focus(body: FocusBody, space: Space = Depends(current_space)):
    user = space.user
    ctx = await task_ctx(user)
    day = _focus_date(body.date, ctx.tz)
    ids = list(dict.fromkeys(body.taskIds))
    await _assert_many_in_space(ids, space)
    async with db.tx() as tx:
        await tx.task.update_many(
            where={**space.where, "focusDate": day, "id": {"not_in": ids}}, data={"focusDate": None}
        )
        await tx.task.update_many(where={**space.where, "id": {"in": ids}}, data={"focusDate": day})
    await log_activity(user.id, "task.focusSet", entity_id=None, date=dates.format_date_only(day), taskIds=ids)
    return await _focus_view(space, day, ctx)


@router.post("/reorder")
async def reorder(body: ReorderBody, space: Space = Depends(current_space)):
    user = space.user
    """Drag and drop: status and position in bulk; status changes run PATCH's side effects."""
    ctx = await task_ctx(user)
    ids = list(dict.fromkeys(i.id for i in body.items))
    await _assert_many_in_space(ids, space)
    existing = {t.id: t for t in await db.task.find_many(where={**space.where, "id": {"in": ids}})}
    transitions = []
    async with db.tx() as tx:
        for item in body.items:
            prev = existing[item.id]
            kind = _transition(prev.status, item.status)
            if kind:
                transitions.append((item.id, kind, prev.title))
            data = {"position": item.position}
            if item.status:
                data["status"] = item.status
            if kind == "completed":
                data["completedAt"] = ctx.now
            if kind == "reopened":
                data["completedAt"] = None
            await tx.task.update_many(where={"id": item.id, **space.where}, data=data)
    for task_id, kind, title in transitions:
        if kind == "completed":
            await _after_completed(space, task_id, ctx)
        else:
            await log_activity(user.id, "task.reopened", entity_id=task_id, title=title)
    return {"ok": True}


@router.post("", status_code=201)
async def create(body: CreateBody, space: Space = Depends(current_space)):
    user = space.user
    ctx = await task_ctx(user)
    await _assert_links(space, body)
    parent = await _valid_parent(space, body.parentId) if body.parentId else None
    status = body.status or "TODO"
    due = _parse_due(body.dueAt, body.hasDueTime, ctx.tz)
    anchor = {"start": body.timeAnchor.start.model_dump()} if body.timeAnchor else None
    if anchor:
        due = (await _anchored_due(user.id, anchor, due[0] if due else ctx.now, ctx.tz), True)
    parent_id = parent.id if parent else None
    position = body.position if body.position is not None else await _default_position(space, status, parent_id)
    sent = body.model_fields_set
    async with db.tx() as tx:
        data = {
            **space.create_fields(),
            "assigneeId": body.assigneeId,
            "title": body.title,
            "description": body.description,
            "notes": body.notes,
            "status": status,
            "priority": body.priority or "MEDIUM",
            # work items: a workspace task is WORK unless it says otherwise
            "lifeArea": body.lifeArea or (None if space.personal else "WORK"),
            "categoryId": body.categoryId,
            "dueAt": due[0] if due else None,
            "hasDueTime": due[1] if due else False,
            "reminderAt": dates.parse_instant(body.reminderAt) if body.reminderAt else None,
            "estimatedMinutes": body.estimatedMinutes,
            "rrule": rrule.normalize_rrule(body.rrule) if body.rrule else None,
            "recurrenceEndAt": _parse_end(body.recurrenceEndAt, ctx.tz),
            "position": position,
            "completedAt": ctx.now if status == "COMPLETED" else None,
            "parentId": parent_id,
            "projectId": body.projectId if "projectId" in sent else (parent.projectId if parent else None),
            "goalId": body.goalId if "goalId" in sent else (parent.goalId if parent else None),
            "courseId": body.courseId,
            "tripId": body.tripId,
        }
        if anchor:
            data["timeAnchor"] = Json(anchor)
        if body.tagIds:
            data["tags"] = {"connect": [{"id": t} for t in body.tagIds]}
        created = await tx.task.create(data=data)
        if body.subtasks and not parent_id:
            await tx.task.create_many(
                data=[
                    {
                        **space.create_fields(),
                        "title": s.title,
                        "priority": s.priority or "MEDIUM",
                        "position": i,
                        "parentId": created.id,
                        "projectId": created.projectId,
                        "goalId": created.goalId,
                    }
                    for i, s in enumerate(body.subtasks)
                ]
            )
    await log_activity(
        user.id, "task.created", entity_id=created.id, title=created.title, projectId=created.projectId,
        parentId=parent_id,
    )
    await _notify_assigned(space, created.id, created.title, body.assigneeId)
    return await _get(space, created.id, ctx)


@router.get("/{id}")
async def get(id: str, space: Space = Depends(current_space)):
    user = space.user
    return await _get(space, id, await task_ctx(user))


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, space: Space = Depends(current_space)):
    user = space.user
    ctx = await task_ctx(user)
    existing = await _owned(space, id)
    await _assert_links(space, body)
    sent = body.model_fields_set
    if body.parentId:
        if body.parentId == id:
            raise nest_error(400, "A task cannot be its own parent")
        await _valid_parent(space, body.parentId)
        if await db.task.count(where={**space.where, "parentId": id}) > 0:
            raise nest_error(400, "A task with subtasks cannot become a subtask")
    # null on a column that can't be empty: NestJS failed in Prisma with 400 "Invalid data"
    if any(f in sent and getattr(body, f) is None for f in ("title", "priority", "position", "hasDueTime", "status")):
        raise invalid_data()

    data: dict = {
        f: getattr(body, f)
        for f in (
            "title", "description", "notes", "priority", "lifeArea", "categoryId", "estimatedMinutes",
            "parentId", "projectId", "goalId", "courseId", "tripId", "position", "assigneeId",
        )
        if f in sent
    }
    if "dueAt" in sent:
        due = _parse_due(body.dueAt, body.hasDueTime, ctx.tz)
        data["dueAt"] = due[0] if due else None
        data["hasDueTime"] = due[1] if due else False
    elif "hasDueTime" in sent:
        data["hasDueTime"] = body.hasDueTime
    # A prayer anchor sets the time on the (new) due day; a new explicit time or no due date drops it.
    if body.timeAnchor:
        anchor = {"start": body.timeAnchor.start.model_dump()}
        day = data.get("dueAt") or existing.dueAt or ctx.now
        data["dueAt"] = await _anchored_due(user.id, anchor, day, ctx.tz)
        data["hasDueTime"] = True
        data["timeAnchor"] = Json(anchor)
    elif existing.timeAnchor and (
        ("timeAnchor" in sent and body.timeAnchor is None)
        or ("dueAt" in sent and body.dueAt is None)
        or ("hasDueTime" in sent and body.hasDueTime is False)
    ):
        data["timeAnchor"] = DB_NULL
    elif existing.timeAnchor and isinstance(body.dueAt, str):
        if dates.DATE_ONLY_RE.match(body.dueAt):
            data["dueAt"] = await _anchored_due(user.id, read_anchor(existing.timeAnchor), data["dueAt"], ctx.tz)
            data["hasDueTime"] = True
        else:
            data["timeAnchor"] = DB_NULL
    if "reminderAt" in sent:
        data["reminderAt"] = dates.parse_instant(body.reminderAt) if body.reminderAt else None
    if "rrule" in sent:
        data["rrule"] = rrule.normalize_rrule(body.rrule) if body.rrule else None
    if "recurrenceEndAt" in sent:
        data["recurrenceEndAt"] = _parse_end(body.recurrenceEndAt, ctx.tz)
    if "tagIds" in sent:
        data["tags"] = {"set": [{"id": t} for t in body.tagIds or []]}

    transition = _transition(existing.status, body.status)
    if body.status is not None:
        data["status"] = body.status
    if transition == "completed":
        data["completedAt"] = ctx.now
    if transition == "reopened":
        data["completedAt"] = None

    nulls = pop_db_nulls(data)
    async with db.tx() as tx:
        await tx.task.update(where={"id": existing.id}, data=data)
        await set_db_null(tx, "Task", existing.id, nulls)
        # Subtasks stay in their parent's project (project progress counts them).
        if "projectId" in sent and existing.parentId is None and body.projectId != existing.projectId:
            await tx.task.update_many(where={**space.where, "parentId": id}, data={"projectId": body.projectId})
    await log_activity(user.id, "task.updated", entity_id=id, title=body.title or existing.title)
    if "assigneeId" in sent and body.assigneeId != existing.assigneeId:
        await _notify_assigned(space, id, body.title or existing.title, body.assigneeId)
    if transition == "completed":
        await _after_completed(space, id, ctx)
    if transition == "reopened":
        await log_activity(user.id, "task.reopened", entity_id=id, title=existing.title)
    return await _get(space, id, ctx)


@router.post("/{id}/complete")
async def complete(id: str, space: Space = Depends(current_space)):
    user = space.user
    ctx = await task_ctx(user)
    existing = await _owned(space, id)
    nxt = None
    if existing.status != "COMPLETED":
        await db.task.update(where={"id": existing.id}, data={"status": "COMPLETED", "completedAt": ctx.now})
        nxt = await _after_completed(space, id, ctx)
    return {**await _get(space, id, ctx), "nextOccurrence": nxt}


@router.post("/{id}/reopen")
async def reopen(id: str, space: Space = Depends(current_space)):
    user = space.user
    ctx = await task_ctx(user)
    existing = await _owned(space, id)
    if existing.status == "COMPLETED":
        await db.task.update(where={"id": existing.id}, data={"status": "TODO", "completedAt": None})
        await log_activity(user.id, "task.reopened", entity_id=id, title=existing.title)
    return await _get(space, id, ctx)


@router.delete("/{id}")
async def remove(id: str, space: Space = Depends(current_space)):
    user = space.user
    existing = await _owned(space, id)
    if not space.can_delete(existing):
        raise nest_error(404, "Task not found")
    children = await db.task.find_many(where={"parentId": id})
    ids = [id, *(c.id for c in children)]
    async with db.tx() as tx:
        # Attachments are linked by (entityType, entityId) with no foreign key: remove them here.
        keys = await attachments.delete_rows(tx, None, "TASK", ids)
        await tx.task.delete(where={"id": existing.id})  # subtasks cascade
    await attachments.delete_objects(keys)
    await log_activity(user.id, "task.deleted", entity_id=id, title=existing.title)
    return {"ok": True}
