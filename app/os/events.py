"""/api/events (port of vournalOS EventsService).

All-day events are stored at local midnight in the user's timezone. A repeating
event keeps a list of excluded occurrences (exDates, the original start instants);
"edit this occurrence only" excludes it and creates a standalone copy.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, Request
from prisma import Json
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.calendar_utils import (
    DAY,
    is_date_only_string,
    local_date_str,
    local_date_time,
    local_midnight,
    local_time_str,
    safe_tz,
    to_all_day_instant,
)
from app.os.common import dates, rrule, validators
from app.os.common.dbnull import DB_NULL, pop_db_nulls, set_db_null
from app.os.common.activity import log_activity
from app.os.common.space import Space, assert_in_space, assert_personal_links_allowed, current_space
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.ownership import assert_owned
from app.os.common.prayer import TimeAnchor, resolve_or_fail, resolver
from app.os.common.query import _query_bool, _query_date, _query_int, string_list, unknown_params
from app.os.event_occurrences import expand_event
from app.os.profile import ensure_os_profile
from app.os.tasks import PRIORITIES, PriorityName

router = nest_router("/events")

EVENT_TYPES = ("GENERAL", "MEETING", "CLASS", "EXAM", "APPOINTMENT", "SOCIAL", "TRAVEL", "WORKOUT", "OTHER")
EventTypeName = Literal["GENERAL", "MEETING", "CLASS", "EXAM", "APPOINTMENT", "SOCIAL", "TRAVEL", "WORKOUT", "OTHER"]
MAX_REMINDER_MINUTES = 40320
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 50, 200
_ONE_MS = timedelta(milliseconds=1)
_FIELDS = (
    "id", "timeAnchor", "title", "description", "location", "type", "priority", "color", "startAt", "endAt",
    "allDay", "rrule", "recurrenceEndAt", "exDates", "reminderMinutes", "categoryId", "taskId", "projectId",
    "goalId", "tripId", "courseId", "householdId", "createdAt", "updatedAt",
)
_INCLUDE = {"category": True, "task": True, "project": True, "goal": True, "trip": True, "course": True}
_LINKS = (("category", "categoryId"), ("task", "taskId"), ("project", "projectId"), ("goal", "goalId"), ("trip", "tripId"), ("course", "courseId"))


def _pick(row, *fields):
    return None if row is None else {f: getattr(row, f) for f in fields}


async def _locale(user):
    settings = await ensure_os_profile(user)
    return safe_tz(settings.timezone), to_lang(settings.language)


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def event_view(row, tz: str, now: datetime, lang: str = "en") -> dict:
    start, end = dates._utc(row.startAt), dates._utc(row.endAt) if row.endAt else None
    ex_dates = {dates._utc(d) for d in row.exDates or []}
    nxt = None
    if row.rrule:
        cursor = rrule.next_occurrence(row.rrule, start, now, tz=tz, until=row.recurrenceEndAt, inclusive=True)
        guard = 0
        while cursor is not None and cursor in ex_dates and guard < 50:  # skip excluded dates
            guard += 1
            cursor = rrule.next_occurrence(row.rrule, start, cursor, tz=tz, until=row.recurrenceEndAt)
        nxt = cursor
    elif (end or start) >= now:
        nxt = start
    view = {f: getattr(row, f) for f in _FIELDS}
    view.update(
        category=_pick(row.category, "id", "name", "color", "icon"),
        task=_pick(row.task, "id", "title", "status"),
        project=_pick(row.project, "id", "name", "color"),
        goal=_pick(row.goal, "id", "name"),
        trip=_pick(row.trip, "id", "name", "color"),
        course=_pick(row.course, "id", "name", "code", "color"),
        isShared=bool(row.householdId),
        isRecurring=bool(row.rrule),
        recurrenceText=rrule.describe(row.rrule, lang) if row.rrule else None,
        nextOccurrence=nxt,
        durationMinutes=_js_round((end - start).total_seconds() / 60) if end else None,
    )
    return view


# ---------------------------------------------------------------------------
# Bodies


def _iso(value, field):
    if value is not None and not dates.is_iso_date_string(value):
        raise ValueError(f"{field} must be a valid ISO 8601 date string")
    return value


class _EventFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=5000)
    location: StrictStr | None = None
    type: EventTypeName | None = None
    priority: PriorityName | None = None
    color: StrictStr | None = None
    date: StrictStr | None = None
    startAt: StrictStr | None = None
    endAt: StrictStr | None = None
    allDay: StrictBool | None = None
    rrule: StrictStr | None = None
    recurrenceEndAt: StrictStr | None = None
    exDates: list[StrictStr] | None = Field(default=None, max_length=1000)
    reminderMinutes: StrictInt | None = Field(default=None, ge=0, le=MAX_REMINDER_MINUTES)
    categoryId: StrictStr | None = None
    taskId: StrictStr | None = None
    projectId: StrictStr | None = None
    goalId: StrictStr | None = None
    tripId: StrictStr | None = None
    courseId: StrictStr | None = None
    shared: StrictBool | None = None
    timeAnchor: TimeAnchor | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("location")
    @classmethod
    def _location(cls, v):
        return validators.trimmed(v, "location", 0, 300)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)

    @field_validator("date", "startAt", "endAt", "recurrenceEndAt")
    @classmethod
    def _dates(cls, v, info):
        return _iso(v, info.field_name)

    @field_validator("exDates")
    @classmethod
    def _ex_dates(cls, v):
        for d in v or []:
            _iso(d, "each value in exDates")
        return v

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v):
        problem = None if v is None else rrule.validate_rrule(v)
        if problem:
            raise ValueError(f"rrule {problem}")
        return v


class CreateBody(_EventFields):
    title: StrictStr

    @field_validator("title")
    @classmethod
    def _required_title(cls, v):
        return validators.trimmed(v, "title", 1, 200)


class UpdateBody(_EventFields):
    pass


class ExcludeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: StrictStr

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _iso(v, "date")


class DetachBody(_EventFields):
    occurrence: StrictStr

    @field_validator("occurrence")
    @classmethod
    def _occurrence(cls, v):
        return _iso(v, "occurrence")


# ---------------------------------------------------------------------------
# Helpers


async def _owned(space: Space, id: str):
    row = await db.event.find_first(where={"id": id, **space.where}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Event not found")
    return row


async def _assert_links(space: Space, body) -> None:
    """Categories, goals, trips and courses are personal; a task or project is in the same
    space; sharing with the household is Personal only."""
    assert_personal_links_allowed(space, body)
    if not space.personal and getattr(body, "shared", None):
        raise nest_error(400, "Household sharing isn't available in a workspace")
    for model, field in _LINKS:
        if model in ("task", "project"):
            await assert_in_space(model, getattr(body, field, None), space)
        else:
            await assert_owned(model, getattr(body, field, None), space.user.id)


def _resolve_start(start_at, day, all_day: bool, tz: str, fallback: datetime | None) -> datetime:
    if start_at:
        if all_day:
            return to_all_day_instant(start_at, tz)
        return local_midnight(start_at, tz) if is_date_only_string(start_at) else dates.parse_instant(start_at)
    if day:
        if all_day:
            return local_midnight(day[:10], tz)
        # Only the date changed: keep the time of day.
        return local_date_time(day[:10], local_time_str(fallback, tz) if fallback else "09:00", tz)
    if fallback is not None:
        return dates._utc(fallback)
    if all_day:
        return local_midnight(local_date_str(datetime.now(timezone.utc), tz), tz)
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)  # next full hour


def _resolve_end(end_at: str, all_day: bool, tz: str) -> datetime:
    if all_day:
        return to_all_day_instant(end_at, tz)
    return local_midnight(end_at, tz) if is_date_only_string(end_at) else dates.parse_instant(end_at)


def _recurrence_end(value, tz: str):
    if not value:
        return None
    if is_date_only_string(value):
        return local_midnight(value, tz) + DAY - _ONE_MS  # through that day
    return dates.parse_instant(value)


def _assert_range(start: datetime, end: datetime | None) -> None:
    if start is None:
        raise nest_error(400, "startAt is invalid")
    if end is not None and end < start:
        raise nest_error(400, "endAt must be after startAt")


def _norm_rrule(value):
    if not value:
        return None
    return rrule.normalize_rrule(value) or None


def _create_data(body, tz: str, sent: set) -> dict:
    all_day = body.allDay if body.allDay is not None else (
        is_date_only_string(body.startAt) if "startAt" in sent else "date" in sent
    )
    start = _resolve_start(body.startAt, body.date, all_day, tz, None)
    end = _resolve_end(body.endAt, all_day, tz) if body.endAt else None
    _assert_range(start, end)
    rule = _norm_rrule(body.rrule)
    data = {
        "title": body.title,
        "description": body.description,
        "location": body.location,
        "color": body.color,
        "startAt": start,
        "endAt": end,
        "allDay": all_day,
        "rrule": rule,
        "recurrenceEndAt": _recurrence_end(body.recurrenceEndAt, tz) if rule else None,
        "exDates": [dates.parse_instant(d) for d in body.exDates or []] if rule else [],
        "reminderMinutes": body.reminderMinutes,
        "categoryId": body.categoryId,
        "taskId": body.taskId,
        "projectId": body.projectId,
        "goalId": body.goalId,
        "tripId": body.tripId,
        "courseId": body.courseId,
    }
    for field in ("type", "priority"):
        if field in sent and getattr(body, field) is None:
            raise invalid_data()
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    return data


async def _anchored(user_id: str, anchor: TimeAnchor, start: datetime, end: datetime | None, tz: str):
    """Start/end of an event following a prayer on the day `start` falls on."""
    canonical = {"start": anchor.start.model_dump(), "end": anchor.end.model_dump() if anchor.end else None}
    resolved_start, resolved_end = await resolve_or_fail(user_id, local_date_str(start, tz), canonical)
    length = end - start if end else None
    return resolved_start, resolved_end or (resolved_start + length if length is not None else None), canonical


async def _household_for(user_id: str) -> str:
    member = await db.householdmember.find_unique(where={"userId": user_id})
    if member is None:
        raise nest_error(400, "Create or join a household first to share an event")
    return member.householdId


def _resolve_occurrence(ev, value: str, tz: str) -> datetime:
    if not ev.rrule:
        raise nest_error(400, "Event is not recurring")
    start = dates._utc(ev.startAt)
    if not is_date_only_string(value):
        instant = dates.parse_instant(value)
        exact = rrule.between(ev.rrule, start, instant, instant, [], tz=tz, until=ev.recurrenceEndAt)
        if exact:
            return exact[0]
    day = value if is_date_only_string(value) else local_date_str(dates.parse_instant(value), tz)
    day_start = local_midnight(day, tz)
    found = rrule.between(ev.rrule, start, day_start, day_start + DAY - _ONE_MS, [], tz=tz, until=ev.recurrenceEndAt)
    if not found:
        raise nest_error(404, "No occurrence of this event on that date")
    return found[0]


def _add_ex_date(existing, occurrence: datetime) -> list[datetime]:
    current = [dates._utc(d) for d in existing or []]
    if occurrence in current:
        return current
    return sorted([*current, occurrence])


def _range_bound(value: str, tz: str, side: str) -> datetime:
    if is_date_only_string(value):
        start = local_midnight(value, tz)
        return start if side == "start" else start + DAY - _ONE_MS
    moment = dates.parse_instant(value)
    if moment is None:
        raise nest_error(400, "Invalid date")
    return moment


# ---------------------------------------------------------------------------
# Routes


_LIST_EVENTS_QUERY = (  # the NestJS query DTO's fields
    "from", "to", "expand", "taskId", "projectId", "goalId", "tripId", "courseId", "categoryId", "type",
    "priority", "recurring", "q", "page", "pageSize",
)


@router.get("")
async def list_events(request: Request, space: Space = Depends(current_space)):
    user = space.user
    tz, lang = await _locale(user)
    q = request.query_params
    errors = unknown_params(request, _LIST_EVENTS_QUERY)
    start_s, end_s = _query_date(request, "from", errors), _query_date(request, "to", errors)
    expand = _query_bool(request, "expand", errors)
    recurring = _query_bool(request, "recurring", errors)
    types = string_list(q.getlist("type"))
    priorities = string_list(q.getlist("priority"))
    if types and any(t not in EVENT_TYPES for t in types):
        errors.append(f"each value in type must be one of the following values: {', '.join(EVENT_TYPES)}")
    if priorities and any(p not in PRIORITIES for p in priorities):
        errors.append(f"each value in priority must be one of the following values: {', '.join(PRIORITIES)}")
    page = _query_int(request, "page", errors)
    page_size = _query_int(request, "pageSize", errors, MAX_PAGE_SIZE)
    text = q.get("q")
    if text is not None:
        text = text.strip()
        if len(text) > 200:
            errors.append("q must be shorter than or equal to 200 characters")
    if errors:
        raise nest_error(400, errors)
    start = _range_bound(start_s, tz, "start") if start_s else None
    end = _range_bound(end_s, tz, "end") if end_s else None
    if start and end and end < start:
        raise nest_error(400, '"to" must be after "from"')

    and_: list[dict] = []
    if start or end:
        lte = {"startAt": {"lte": end}} if end else {}
        and_.append(
            {
                "OR": [
                    {"rrule": None, **lte, **({"OR": [{"endAt": {"gte": start}}, {"endAt": None, "startAt": {"gte": start}}]} if start else {})},
                    {"rrule": {"not": None}, **lte, **({"OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": start}}]} if start else {})},
                ]
            }
        )
    if text:
        and_.append(
            {
                "OR": [
                    {"title": {"contains": text, "mode": "insensitive"}},
                    {"description": {"contains": text, "mode": "insensitive"}},
                    {"location": {"contains": text, "mode": "insensitive"}},
                ]
            }
        )
    where: dict = dict(space.where)
    for field in ("taskId", "projectId", "goalId", "tripId", "courseId", "categoryId"):
        if q.get(field):
            where[field] = q.get(field)
    if types:
        where["type"] = {"in": types}
    if priorities:
        where["priority"] = {"in": priorities}
    if recurring is True:
        where["rrule"] = {"not": None}
    elif recurring is False:
        where["rrule"] = None
    if and_:
        where["AND"] = and_
    size = page_size or DEFAULT_PAGE_SIZE
    skip = ((page or 1) - 1) * size
    now = datetime.now(timezone.utc)

    if expand and start and end:
        rows = await db.event.find_many(where=where, include=_INCLUDE, order={"startAt": "asc"}, take=2000)
        resolve = await resolver(user.id)
        occurrences = []
        for row in rows:
            view = event_view(row, tz, now, lang)
            for occ in expand_event(row, start, end, tz, resolve):
                occurrences.append({**view, "occurrenceStart": occ.start, "occurrenceEnd": occ.end})
        occurrences.sort(key=lambda o: o["occurrenceStart"])
        return {"items": occurrences[skip : skip + size], "total": len(occurrences)}

    rows = await db.event.find_many(
        where=where, include=_INCLUDE, order=[{"startAt": "asc"}, {"id": "asc"}], skip=skip, take=size
    )
    total = await db.event.count(where=where)
    return {"items": [event_view(r, tz, now, lang) for r in rows], "total": total}


@router.post("", status_code=201)
async def create(body: CreateBody, space: Space = Depends(current_space)):
    user = space.user
    tz, lang = await _locale(user)
    await _assert_links(space, body)
    sent = body.model_fields_set
    data = _create_data(body, tz, sent)
    if body.timeAnchor and not data["allDay"]:
        data["startAt"], data["endAt"], anchor = await _anchored(user.id, body.timeAnchor, data["startAt"], data["endAt"], tz)
        data["timeAnchor"] = Json(anchor)
    data["householdId"] = await _household_for(user.id) if body.shared else None
    created = await db.event.create(data={**data, **space.create_fields()}, include=_INCLUDE)
    await log_activity(user.id, "event.created", entity_id=created.id, title=created.title, startAt=created.startAt)
    return event_view(created, tz, datetime.now(timezone.utc), lang)


@router.get("/{id}")
async def get(id: str, space: Space = Depends(current_space)):
    user = space.user
    tz, _ = await _locale(user)
    # NestJS left out the language here: the repeat text is always English on this route.
    return event_view(await _owned(space, id), tz, datetime.now(timezone.utc))


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, space: Space = Depends(current_space)):
    user = space.user
    tz, lang = await _locale(user)
    existing = await _owned(space, id)
    await _assert_links(space, body)
    sent = body.model_fields_set
    old_start = dates._utc(existing.startAt)
    old_end = dates._utc(existing.endAt) if existing.endAt else None
    all_day = body.allDay if body.allDay is not None else existing.allDay
    all_day_changed = all_day != existing.allDay

    start = old_start
    if "startAt" in sent or "date" in sent:
        start = _resolve_start(body.startAt, body.date, all_day, tz, old_start)
    elif all_day_changed and all_day:
        start = local_midnight(local_date_str(old_start, tz), tz)
    end = old_end
    if "endAt" in sent and body.endAt is None:
        end = None
    elif isinstance(body.endAt, str):
        end = _resolve_end(body.endAt, all_day, tz)
    elif old_end:
        end = old_end + (start - old_start)  # moving keeps the duration
        if all_day_changed and all_day:
            end = local_midnight(local_date_str(end, tz), tz)

    # A new prayer anchor sets the times; an explicit new time (a drag) drops the old one.
    time_anchor = None
    if body.timeAnchor and not all_day:
        start, end, anchor = await _anchored(user.id, body.timeAnchor, start, end, tz)
        time_anchor = Json(anchor)
    elif ("timeAnchor" in sent and body.timeAnchor is None) or all_day or (
        ("startAt" in sent or "endAt" in sent) and existing.timeAnchor
    ):
        time_anchor = DB_NULL
    delta = start - old_start
    _assert_range(start, end)

    rule = existing.rrule if "rrule" not in sent else _norm_rrule(body.rrule)
    recurrence_end = existing.recurrenceEndAt if "recurrenceEndAt" not in sent else _recurrence_end(body.recurrenceEndAt, tz)
    if "exDates" in sent:
        ex_dates = [dates.parse_instant(d) for d in body.exDates or []]
    elif not rule:
        ex_dates = []
    elif rule == existing.rrule and delta:
        ex_dates = [dates._utc(d) + delta for d in existing.exDates or []]
    else:
        ex_dates = list(existing.exDates or [])

    if any(f in sent and getattr(body, f) is None for f in ("title", "type", "priority")):
        raise invalid_data()
    data = {
        f: getattr(body, f)
        for f in (
            "title", "description", "location", "type", "priority", "color", "reminderMinutes", "categoryId",
            "taskId", "projectId", "goalId", "tripId", "courseId",
        )
        if f in sent
    }
    data.update(startAt=start, endAt=end, allDay=all_day, rrule=rule, recurrenceEndAt=recurrence_end, exDates=ex_dates)
    if time_anchor is not None:
        data["timeAnchor"] = time_anchor
    if "shared" in sent:
        data["householdId"] = await _household_for(user.id) if body.shared else None
    nulls = pop_db_nulls(data)
    row = await db.event.update(where={"id": existing.id}, data=data, include=_INCLUDE)
    await set_db_null(db, "Event", existing.id, nulls)
    if nulls:  # the row came back before the NULL was written
        row = row.model_copy(update={column: None for column in nulls})
    await log_activity(user.id, "event.updated", entity_id=row.id, title=row.title)
    return event_view(row, tz, datetime.now(timezone.utc), lang)


@router.delete("/{id}")
async def remove(id: str, space: Space = Depends(current_space)):
    user = space.user
    existing = await _owned(space, id)
    if not space.can_delete(existing):
        raise nest_error(404, "Event not found")
    await db.event.delete(where={"id": existing.id})
    await log_activity(user.id, "event.deleted", entity_id=id, title=existing.title)
    return {"ok": True}


@router.post("/{id}/exclude")
async def exclude(id: str, body: ExcludeBody, space: Space = Depends(current_space)):
    user = space.user
    """Skip one occurrence of a repeating event ("delete this event only")."""
    tz, lang = await _locale(user)
    existing = await _owned(space, id)
    occurrence = _resolve_occurrence(existing, body.date, tz)
    row = await db.event.update(
        where={"id": existing.id}, data={"exDates": _add_ex_date(existing.exDates, occurrence)}, include=_INCLUDE
    )
    await log_activity(user.id, "event.occurrenceExcluded", entity_id=id, occurrence=occurrence)
    return event_view(row, tz, datetime.now(timezone.utc), lang)


@router.post("/{id}/occurrence")
async def detach(id: str, body: DetachBody, space: Space = Depends(current_space)):
    user = space.user
    """"Edit this occurrence only": excluded from the series, recreated standalone."""
    tz, lang = await _locale(user)
    series = await _owned(space, id)
    occurrence = _resolve_occurrence(series, body.occurrence, tz)
    await _assert_links(space, body)
    start, end = dates._utc(series.startAt), dates._utc(series.endAt) if series.endAt else None
    duration = end - start if end else None
    iso = lambda m: m.isoformat().replace("+00:00", "Z")  # noqa: E731
    occ_start = local_date_str(occurrence, tz) if series.allDay else iso(occurrence)
    occ_end = None
    if duration is not None:
        occ_end = local_date_str(occurrence + duration, tz) if series.allDay else iso(occurrence + duration)

    overrides = {
        f: getattr(body, f)
        for f in body.model_fields_set
        if f not in ("occurrence", "rrule", "recurrenceEndAt", "exDates")
    }
    merged = {
        "title": series.title,
        "description": series.description,
        "location": series.location,
        "type": series.type,
        "priority": series.priority,
        "color": series.color,
        "allDay": series.allDay,
        "startAt": occ_start,
        "endAt": occ_end,
        "reminderMinutes": series.reminderMinutes,
        "categoryId": series.categoryId,
        "taskId": series.taskId,
        "projectId": series.projectId,
        "goalId": series.goalId,
        "tripId": series.tripId,
        "courseId": series.courseId,
        **overrides,
        "rrule": None,
        "recurrenceEndAt": None,
    }
    # Only allDay toggled, no new times: re-derive them from the occurrence.
    if "allDay" in overrides and "startAt" not in overrides and "date" not in overrides:
        merged["startAt"] = local_date_str(occurrence, tz) if overrides["allDay"] else iso(occurrence)
        if "endAt" not in overrides:
            merged["endAt"] = None
    merged_body = UpdateBody.model_construct(**{k: v for k, v in merged.items() if k in UpdateBody.model_fields})
    data = _create_data(merged_body, tz, set(merged))
    async with db.tx() as tx:
        series_row = await tx.event.update(
            where={"id": series.id}, data={"exDates": _add_ex_date(series.exDates, occurrence)}, include=_INCLUDE
        )
        created = await tx.event.create(
            data={**data, "userId": series.userId, "createdById": series.createdById, "workspaceId": series.workspaceId}, include=_INCLUDE
        )
    await log_activity(user.id, "event.occurrenceExcluded", entity_id=id, occurrence=occurrence)
    await log_activity(user.id, "event.created", entity_id=created.id, title=created.title, detachedFrom=id)
    now = datetime.now(timezone.utc)
    return {"series": event_view(series_row, tz, now, lang), "event": event_view(created, tz, now, lang)}
