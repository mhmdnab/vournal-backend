"""/api/reminders (port of vournalOS RemindersService)."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, Request
from prisma import Json
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from app.db import db
from app.os import reminder_schedule as schedule
from app.os.calendar_utils import (
    DAY,
    entity_link,
    is_date_only_string,
    is_local_midnight,
    local_date_str,
    local_date_time,
    local_midnight,
    safe_tz,
)
from app.os.common import dates, rrule, validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.ownership import ENTITY_TYPE_MODEL, assert_entity
from app.os.common.prayer import TimeAnchor, read_anchor, resolve_or_fail, resolver
from app.os.profile import ensure_os_profile
from app.os.tasks import _query_bool, _query_date, _query_int

router = nest_router("/reminders")

STATUSES = ("active", "upcoming", "overdue", "today", "done", "all")
EntityTypeName = Literal[tuple(ENTITY_TYPE_MODEL)]  # type: ignore[valid-type]
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 50, 200
_ONE_MS = timedelta(milliseconds=1)
_FIELDS = (
    "id", "timeAnchor", "title", "notes", "remindAt", "rrule", "isDeadline", "isDone", "snoozedUntil",
    "lastFiredAt", "entityType", "entityId", "createdAt", "updatedAt",
)
# EntityType → (model, title field); every one of these has a userId column.
ENTITY_TITLE = {
    "TASK": ("task", "title"),
    "EVENT": ("event", "title"),
    "PROJECT": ("project", "name"),
    "TRANSACTION": ("transaction", "title"),
    "RECURRING_PAYMENT": ("recurringpayment", "name"),
    "BUDGET": ("budget", "name"),
    "GOAL": ("goal", "name"),
    "HABIT": ("habit", "name"),
    "MAINTENANCE": ("maintenanceitem", "title"),
    "DOCUMENT": ("document", "name"),
    "SHOPPING_LIST": ("shoppinglist", "name"),
    "SHOPPING_ITEM": ("shoppingitem", "name"),
    "MEAL": ("mealplanentry", "title"),
    "RECIPE": ("recipe", "name"),
    "NOTE": ("note", "title"),
    "COURSE": ("course", "name"),
    "ASSESSMENT": ("assessment", "title"),
    "TRIP": ("trip", "name"),
    "PERSON": ("person", "name"),
    "IMPORTANT_DATE": ("importantdate", "title"),
    "REMINDER": ("reminder", "title"),
    "DEBT": ("debt", "title"),
}


async def entity_titles(user_id: str, refs) -> dict[str, str]:
    """Titles of linked items, one query per type, scoped to the user."""
    by_type: dict[str, set[str]] = {}
    for r in refs:
        if r.entityType and r.entityId:
            by_type.setdefault(r.entityType, set()).add(r.entityId)
    out = {}
    for kind, ids in by_type.items():
        model, field = ENTITY_TITLE[kind]
        for row in await getattr(db, model).find_many(where={"id": {"in": list(ids)}, "userId": user_id}):
            title = getattr(row, field, None)
            if isinstance(title, str) and title:
                out[f"{kind}:{row.id}"] = title
    return out


async def reminder_views(user_id: str, rows, tz: str, now: datetime, lang: str) -> list[dict]:
    titles = await entity_titles(user_id, rows)
    today = local_date_str(now, tz)
    out = []
    for r in rows:
        day = local_date_str(r.snoozedUntil or r.remindAt, tz)
        view = {f: getattr(r, f) for f in _FIELDS}
        view.update(
            allDay=schedule.is_all_day(r, tz),
            date=day,
            fireAt=schedule.fire_at(r, tz),
            isOverdue=schedule.is_overdue(r, tz, now),
            isToday=not r.isDone and day == today,
            isRecurring=bool(r.rrule),
            recurrenceText=rrule.describe(r.rrule, lang) if r.rrule else None,
            pendingAck=bool(r.rrule) and not r.isDone and r.lastFiredAt is not None,
            entity=(
                {
                    "type": r.entityType,
                    "id": r.entityId,
                    "title": titles.get(f"{r.entityType}:{r.entityId}"),
                    "link": entity_link(r.entityType, r.entityId),
                }
                if r.entityType and r.entityId
                else None
            ),
        )
        out.append(view)
    return out


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr | None = None
    notes: StrictStr | None = Field(default=None, max_length=5000)
    remindAt: StrictStr | None = None
    date: StrictStr | None = None
    time: StrictStr | None = None
    allDay: StrictBool | None = None
    rrule: StrictStr | None = None
    isDeadline: StrictBool | None = None
    entityType: EntityTypeName | None = None
    entityId: StrictStr | None = None
    timeAnchor: TimeAnchor | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("remindAt", "date")
    @classmethod
    def _dates(cls, v, info):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError(f"{info.field_name} must be a valid ISO 8601 date string")
        return v

    @field_validator("time")
    @classmethod
    def _time(cls, v):
        if v is not None and not validators.HHMM.match(v):
            raise ValueError("time must be a time in HH:mm format")
        return v

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v):
        problem = None if v is None else rrule.validate_rrule(v)
        if problem:
            raise ValueError(f"rrule {problem}")
        return v


class CreateBody(_Fields):
    title: StrictStr

    @field_validator("title")
    @classmethod
    def _required_title(cls, v):
        return validators.trimmed(v, "title", 1, 200)


class UpdateBody(_Fields):
    isDone: StrictBool | None = None


class SnoozeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int | str | None = None
    until: StrictStr | None = None

    @field_validator("minutes")
    @classmethod
    def _minutes(cls, v):
        if v is None:
            return v
        try:
            number = float(v)  # @Type(() => Number): "10" counts
        except (TypeError, ValueError):
            raise ValueError("minutes must be an integer number")
        if isinstance(v, bool) or number != int(number) or not 1 <= number <= 43200:
            raise ValueError("minutes must be an integer between 1 and 43200")
        return int(number)

    @field_validator("until")
    @classmethod
    def _until(cls, v):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError("until must be a valid ISO 8601 date string")
        return v


async def _locale(user):
    settings = await ensure_os_profile(user)
    return safe_tz(settings.timezone), to_lang(settings.language)


async def _owned(user_id: str, id: str):
    row = await db.reminder.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Reminder not found")
    return row


async def _get(user_id: str, id: str, tz: str, lang: str) -> dict:
    [view] = await reminder_views(user_id, [await _owned(user_id, id)], tz, datetime.now(timezone.utc), lang)
    return view


def _norm_rrule(value):
    return (rrule.normalize_rrule(value) or None) if value else None


def _resolve_remind_at(body, tz: str, existing: datetime | None) -> datetime:
    """remindAt from `remindAt` (ISO → timed, "YYYY-MM-DD" → date-based) or `date` +
    optional `time`; `allDay` forces date-based. Defaults to the next full hour."""
    all_day = body.allDay
    if body.remindAt:
        if is_date_only_string(body.remindAt):
            at = local_midnight(body.remindAt, tz)
            all_day = True if all_day is None else all_day
        else:
            at = dates.parse_instant(body.remindAt)
    elif body.date:
        day = body.date[:10]
        if body.time:
            at = local_date_time(day, body.time, tz)
            all_day = False if all_day is None else all_day
        else:
            at = local_midnight(day, tz)
            all_day = True if all_day is None else all_day
    elif body.time and existing:
        at = local_date_time(local_date_str(existing, tz), body.time, tz)
        all_day = False if all_day is None else all_day
    elif existing:
        at = dates._utc(existing)
    else:
        at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if at is None:
        raise nest_error(400, "remindAt is invalid")
    if all_day is True:
        at = local_midnight(local_date_str(at, tz), tz)
    elif all_day is False and is_local_midnight(at, tz):
        at = local_date_time(local_date_str(at, tz), "09:00", tz)
    return at


async def _anchored_at(user_id: str, anchor: dict, day: datetime, tz: str) -> datetime:
    start, _ = await resolve_or_fail(user_id, local_date_str(day, tz), anchor)
    return start


def _bound(value: str, tz: str, side: str) -> datetime:
    if is_date_only_string(value):
        start = local_midnight(value, tz)
        return start if side == "start" else start + DAY - _ONE_MS
    return dates.parse_instant(value)


@router.get("")
async def list_reminders(request: Request, user=Depends(plus_user)):
    tz, lang = await _locale(user)
    q = request.query_params
    errors: list[str] = []
    status = q.get("status")
    if status is not None and status not in STATUSES:
        errors.append(f"status must be one of the following values: {', '.join(STATUSES)}")
    start_s, end_s = _query_date(request, "from", errors), _query_date(request, "to", errors)
    recurring = _query_bool(request, "recurring", errors)
    deadline = _query_bool(request, "deadline", errors)
    entity_type = q.get("entityType")
    if entity_type is not None and entity_type not in ENTITY_TYPE_MODEL:
        errors.append("entityType must be one of the following values")
    page = _query_int(request, "page", errors)
    page_size = _query_int(request, "pageSize", errors, MAX_PAGE_SIZE)
    text = q.get("q")
    if text is not None:
        text = text.strip()
        if len(text) > 200:
            errors.append("q must be shorter than or equal to 200 characters")
    if errors:
        raise nest_error(400, errors)
    status = status or "active"
    now = datetime.now(timezone.utc)
    size = page_size or DEFAULT_PAGE_SIZE
    skip = ((page or 1) - 1) * size

    where: dict = {"userId": user.id}
    if recurring is True:
        where["rrule"] = {"not": None}
    elif recurring is False:
        where["rrule"] = None
    if deadline is not None:
        where["isDeadline"] = deadline
    if entity_type:
        where["entityType"] = entity_type
    if q.get("entityId"):
        where["entityId"] = q.get("entityId")
    if text:
        where["OR"] = [{"title": {"contains": text, "mode": "insensitive"}}, {"notes": {"contains": text, "mode": "insensitive"}}]

    if status == "done":
        done_where = {**where, "isDone": True}
        if start_s or end_s:
            done_where["remindAt"] = {
                **({"gte": _bound(start_s, tz, "start")} if start_s else {}),
                **({"lte": _bound(end_s, tz, "end")} if end_s else {}),
            }
        rows = await db.reminder.find_many(where=done_where, order={"updatedAt": "desc"}, skip=skip, take=size)
        total = await db.reminder.count(where=done_where)
        return {"items": await reminder_views(user.id, rows, tz, now, lang), "total": total}

    # Active reminders are few: work out the derived states in memory, exactly.
    rows = await db.reminder.find_many(
        where={**where, **({} if status == "all" else {"isDone": False})}, order={"remindAt": "asc"}, take=2000
    )
    views = await reminder_views(user.id, rows, tz, now, lang)
    start = _bound(start_s, tz, "start") if start_s else None
    end = _bound(end_s, tz, "end") if end_s else None

    def keep(v) -> bool:
        if status == "overdue" and not v["isOverdue"]:
            return False
        if status == "upcoming" and (v["isDone"] or v["isOverdue"]):
            return False
        if status == "today" and (v["isDone"] or not v["isToday"]):
            return False
        if start and v["fireAt"] < start:
            return False
        if end and v["fireAt"] > end:
            return False
        return True

    views = [v for v in views if keep(v)]
    views.sort(key=lambda v: (v["isDone"], v["fireAt"]))
    return {"items": views[skip : skip + size], "total": len(views)}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    tz, lang = await _locale(user)
    await assert_entity(body.entityType, body.entityId, user.id)
    remind_at = _resolve_remind_at(body, tz, None)
    anchor = {"start": body.timeAnchor.start.model_dump()} if body.timeAnchor else None
    if anchor:
        remind_at = await _anchored_at(user.id, anchor, remind_at, tz)
    data = {
        "userId": user.id,
        "title": body.title,
        "notes": body.notes,
        "remindAt": remind_at,
        "rrule": _norm_rrule(body.rrule),
        "isDeadline": bool(body.isDeadline),
        "entityType": body.entityType,
        "entityId": body.entityId if body.entityType else None,
    }
    if anchor:
        data["timeAnchor"] = Json(anchor)
    row = await db.reminder.create(data=data)
    await log_activity(user.id, "reminder.created", entity_id=row.id, title=row.title, remindAt=row.remindAt)
    return await _get(user.id, row.id, tz, lang)


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    tz, lang = await _locale(user)
    return await _get(user.id, id, tz, lang)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    tz, lang = await _locale(user)
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    if "entityType" in sent or "entityId" in sent:
        kind = existing.entityType if "entityType" not in sent else body.entityType
        eid = existing.entityId if "entityId" not in sent else body.entityId
        await assert_entity(kind, eid, user.id)
    time_sent = bool(sent & {"remindAt", "date", "time", "allDay"})
    resolved = _resolve_remind_at(body, tz, existing.remindAt) if time_sent else dates._utc(existing.remindAt)
    # A new prayer anchor sets the time on the (new) day; moving the day keeps following
    # it; a new clock time drops it.
    time_anchor = None
    kept = read_anchor(existing.timeAnchor)
    if body.timeAnchor:
        anchor = {"start": body.timeAnchor.start.model_dump()}
        resolved = await _anchored_at(user.id, anchor, resolved, tz)
        time_anchor = Json(anchor)
    elif kept and (("timeAnchor" in sent and body.timeAnchor is None) or "time" in sent or "remindAt" in sent):
        time_anchor = Json(None)
    elif kept and "date" in sent:
        resolved = await _anchored_at(user.id, kept, resolved, tz)
    # Only a real change of time re-arms the reminder (a new title mustn't re-notify).
    time_changed = resolved != dates._utc(existing.remindAt)

    if any(f in sent and getattr(body, f) is None for f in ("title", "isDeadline", "isDone")):
        raise invalid_data()
    data = {f: getattr(body, f) for f in ("title", "notes", "isDeadline", "isDone", "entityType") if f in sent}
    if "rrule" in sent:
        data["rrule"] = _norm_rrule(body.rrule)
    if "entityType" in sent and body.entityType is None:
        data["entityId"] = None
    elif "entityId" in sent:
        data["entityId"] = body.entityId
    if time_anchor is not None:
        data["timeAnchor"] = time_anchor
    if time_changed:
        # re-scheduling clears snooze and fired state so it fires again at the new time
        data.update(remindAt=resolved, snoozedUntil=None, lastFiredAt=None)
    if body.isDone is False and existing.isDone:
        data["lastFiredAt"] = None
    row = await db.reminder.update(where={"id": existing.id}, data=data)
    await log_activity(user.id, "reminder.updated", entity_id=row.id, title=row.title)
    return await _get(user.id, row.id, tz, lang)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.reminder.delete(where={"id": existing.id})
    # Its unread notifications go with it.
    await db.notification.delete_many(
        where={"userId": user.id, "entityType": "REMINDER", "entityId": id, "readAt": None}
    )
    await log_activity(user.id, "reminder.deleted", entity_id=id, title=existing.title)
    return {"ok": True}


@router.post("/{id}/done")
async def done(id: str, user=Depends(plus_user)):
    """One-time → finished. Repeating → this cycle is done: a recent unacknowledged
    fire is acknowledged; otherwise it advances (finishing when the rule ends)."""
    tz, lang = await _locale(user)
    r = await _owned(user.id, id)
    now = datetime.now(timezone.utc)
    advanced = False
    if not r.rrule:
        data = {"isDone": True, "snoozedUntil": None}
    else:
        fire = schedule.fire_at(r, tz)
        fired = dates._utc(r.lastFiredAt) if r.lastFiredAt else None
        recently_fired = fired is not None and fire > now and now < fired + (fire - fired) / 2
        if recently_fired:
            data = {"lastFiredAt": None, "snoozedUntil": None}
        else:
            nxt = schedule.next_occurrence(r, schedule.advance_after(r, now), tz, await resolver(user.id))
            data = {"remindAt": nxt, "snoozedUntil": None, "lastFiredAt": None} if nxt else {"isDone": True, "snoozedUntil": None}
            advanced = nxt is not None
    await db.reminder.update(where={"id": r.id}, data=data)
    # Its unread notifications are no longer actionable.
    await db.notification.update_many(
        where={"userId": user.id, "entityType": "REMINDER", "entityId": r.id, "readAt": None}, data={"readAt": now}
    )
    await log_activity(user.id, "reminder.done", entity_id=r.id, title=r.title, recurring=bool(r.rrule), advanced=advanced)
    return await _get(user.id, r.id, tz, lang)


@router.post("/{id}/snooze")
async def snooze(id: str, body: SnoozeBody, user=Depends(plus_user)):
    tz, lang = await _locale(user)
    r = await _owned(user.id, id)
    if r.isDone:
        raise nest_error(400, "Reminder is already done")
    now = datetime.now(timezone.utc)
    if body.until:
        until = dates.parse_instant(body.until)
    elif body.minutes:
        until = now + timedelta(minutes=body.minutes)
    else:
        raise nest_error(400, "Provide minutes or until")
    if until <= now:
        raise nest_error(400, "Snooze time must be in the future")
    if until - now > 366 * DAY:
        raise nest_error(400, "Snooze is limited to one year")
    await db.reminder.update(where={"id": r.id}, data={"snoozedUntil": until})
    await db.notification.update_many(
        where={"userId": user.id, "entityType": "REMINDER", "entityId": r.id, "readAt": None},
        data={"readAt": datetime.now(timezone.utc)},
    )
    await log_activity(user.id, "reminder.snoozed", entity_id=r.id, until=until)
    return await _get(user.id, r.id, tz, lang)
