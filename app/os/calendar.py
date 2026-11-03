"""/api/calendar (port of vournalOS CalendarService): one feed of everything dated.

Timed items carry ISO instants in start/end; all-day items carry local dates
("YYYY-MM-DD", end inclusive). Sources from hidden sections are left out; events
always show. Within a day all-day items come first, then by start, then title.
"""

import functools
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request

from app.db import db
from app.os.calendar_utils import (
    DAY,
    ENTITY_LINKS,
    add_days_str,
    anniversary_str,
    date_only_str,
    date_only_value,
    diff_days_str,
    is_date_only_string,
    is_local_midnight,
    local_date_str,
    local_midnight,
    safe_tz,
)
from app.os.common import dates, rrule
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.query import _query_bool, string_list, unknown_params
from app.os.common.prayer import resolver
from app.os.event_occurrences import expand_event
from app.os.profile import ensure_os_profile

router = nest_router("/calendar")

SOURCE_TYPES = (
    "event", "task", "bill", "subscription", "birthday", "anniversary", "importantDate", "document",
    "maintenance", "trip", "tripItem", "assessment", "reminder", "studySession", "debt",
)
MAX_RANGE_DAYS = 400
_ONE_MS = timedelta(milliseconds=1)

# Source → section key; a hidden section's items stay out of the feed. Events always show.
SOURCE_SECTION = {
    "event": None, "task": "tasks", "bill": "finance", "subscription": "finance", "birthday": "people",
    "anniversary": "people", "importantDate": "people", "document": "documents", "maintenance": "maintenance",
    "trip": "travel", "tripItem": "travel", "assessment": "study", "studySession": "study",
    "reminder": "reminders", "debt": "finance",
}

# Titles the feed writes itself (everything else is the user's own text).
FEED_TITLES = {
    "en": {
        "birthday": "{name}'s birthday",
        "anniversary": "{name} — anniversary",
        "documentExpires": "{name} expires",
        "studyTopic": "Study: {topic}",
        "studySession": "Study: session",
        "debtPay": "Pay back: {name}",
        "debtCollect": "{who} pays you back",
    },
    "ar": {
        "birthday": "عيد ميلاد {name}",
        "anniversary": "الذكرى السنوية — {name}",
        "documentExpires": "انتهاء صلاحية {name}",
        "studyTopic": "دراسة: {topic}",
        "studySession": "جلسة دراسة",
        "debtPay": "سداد: {name}",
        "debtCollect": "استرداد الدين من {who}",
    },
}


def _iso(moment: datetime) -> str:
    moment = dates._utc(moment)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _fmt(template: str, **values) -> str:
    out = template
    for key, value in values.items():
        if value is not None:
            out = out.replace("{" + key + "}", str(value))
    return out


class Ctx:
    def __init__(self, user_id, tz, lang, start, end):
        self.user_id = user_id
        self.tz = tz
        self.titles = FEED_TITLES[lang]
        self.start = start
        self.end = end
        self.start_date = local_date_str(start, tz)
        self.end_date = local_date_str(end, tz)
        self.now = datetime.now(timezone.utc)
        self.today = local_date_str(self.now, tz)
        self.include_completed = True


def _item(id, source_type, source_id, title, start, end, all_day, color, link, priority=None, meta=None) -> dict:
    item = {
        "id": id, "sourceType": source_type, "sourceId": source_id, "title": title, "start": start, "end": end,
        "allDay": all_day, "color": color, "link": link,
    }
    if priority is not None:
        item["priority"] = priority
    if meta is not None:
        item["meta"] = meta
    return item


def _years(start_date: str, end_date: str) -> range:
    return range(int(start_date[:4]), int(end_date[:4]) + 1)


# ---------------------------------------------------------------------------
# Sources


async def _events(ctx: Ctx) -> list[dict]:
    member = await db.householdmember.find_unique(where={"userId": ctx.user_id})
    access = {"OR": [{"userId": ctx.user_id}, {"householdId": member.householdId}]} if member else {"userId": ctx.user_id}
    rows = await db.event.find_many(
        where={
            "AND": [access],
            "startAt": {"lte": ctx.end},
            "OR": [
                # one-off: overlaps the range (a multi-day event that began earlier counts)
                {"rrule": None, "OR": [{"endAt": {"gte": ctx.start}}, {"endAt": None, "startAt": {"gte": ctx.start}}]},
                {"rrule": {"not": None}, "OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": ctx.start}}]},
            ],
        },
        include={"category": True, "task": True, "project": True, "trip": True, "course": True},
        order={"startAt": "asc"},
        take=5000,
    )
    owners = {r.userId for r in rows if r.userId != ctx.user_id}
    names = {}
    if owners:
        for s in await db.usersettings.find_many(where={"userId": {"in": list(owners)}}):
            names[s.userId] = s.name or ""
    resolve = await resolver(ctx.user_id)
    out = []
    for ev in rows:
        # Someone else's shared event: visible, never editable, without their private links.
        mine = ev.userId == ctx.user_id
        recurring = bool(ev.rrule)
        for o in expand_event(ev, ctx.start, ctx.end, ctx.tz, resolve):
            if ev.allDay:
                end = o.end_date
            else:
                end = _iso(o.end if ev.endAt else o.start + timedelta(hours=1))
            out.append(
                _item(
                    f"event:{ev.id}:{_iso(o.key)}" if recurring else f"event:{ev.id}",
                    "event",
                    ev.id,
                    ev.title,
                    o.start_date if ev.allDay else _iso(o.start),
                    end,
                    ev.allDay,
                    ev.color or (ev.category.color if ev.category else None) or (ev.course.color if ev.course else None),
                    f"/calendar?id={ev.id}&date={o.start_date if ev.allDay else local_date_str(o.start, ctx.tz)}",
                    ev.priority,
                    {
                        "type": ev.type,
                        "location": ev.location,
                        "description": ev.description,
                        "hasEnd": ev.endAt is not None,
                        "isRecurring": recurring,
                        "occurrenceStart": _iso(o.key),
                        "timeAnchor": ev.timeAnchor,
                        "rrule": ev.rrule,
                        "reminderMinutes": ev.reminderMinutes,
                        "categoryId": ev.categoryId if mine else None,
                        "categoryName": ev.category.name if ev.category else None,
                        "categoryIcon": ev.category.icon if ev.category else None,
                        "taskId": ev.taskId if mine else None,
                        "taskTitle": (ev.task.title if ev.task else None) if mine else None,
                        "projectId": ev.projectId if mine else None,
                        "projectName": (ev.project.name if ev.project else None) if mine else None,
                        "goalId": ev.goalId if mine else None,
                        "tripId": ev.tripId if mine else None,
                        "tripName": (ev.trip.name if ev.trip else None) if mine else None,
                        "courseId": ev.courseId if mine else None,
                        "courseName": (ev.course.name if ev.course else None) if mine else None,
                        "shared": bool(ev.householdId),
                        "readOnly": not mine,
                        "sharedBy": None if mine else names.get(ev.userId, ""),
                    },
                )
            )
    return out


async def _tasks(ctx: Ctx) -> list[dict]:
    where = {"userId": ctx.user_id, "dueAt": {"gte": ctx.start, "lte": ctx.end}}
    if not ctx.include_completed:
        where["status"] = {"not": "COMPLETED"}
    rows = await db.task.find_many(where=where, include={"project": True, "category": True}, order={"dueAt": "asc"}, take=5000)
    start_of_today = local_midnight(ctx.today, ctx.tz)
    out = []
    for t in rows:
        due = dates._utc(t.dueAt)
        all_day = not t.hasDueTime
        completed = t.status == "COMPLETED"
        overdue = not completed and (due < start_of_today if all_day else due < ctx.now)
        day = local_date_str(due, ctx.tz)
        out.append(
            _item(
                f"task:{t.id}", "task", t.id, t.title,
                day if all_day else _iso(due),
                day if all_day else _iso(due + timedelta(minutes=t.estimatedMinutes or 0)),
                all_day, None, ENTITY_LINKS["TASK"](t.id), t.priority,
                {
                    "status": t.status,
                    "completed": completed,
                    "completedAt": t.completedAt,
                    "overdue": overdue,
                    "estimatedMinutes": t.estimatedMinutes,
                    "isRecurring": bool(t.rrule),
                    "isSubtask": bool(t.parentId),
                    "projectId": t.projectId,
                    "projectName": t.project.name if t.project else None,
                    "categoryName": t.category.name if t.category else None,
                    "categoryColor": t.category.color if t.category else None,
                },
            )
        )
    return out


async def _recurring_payments(ctx: Ctx, wanted: set) -> list[dict]:
    if "bill" in wanted and "subscription" in wanted:
        kinds = {}
    elif "subscription" in wanted:
        kinds = {"kind": "SUBSCRIPTION"}
    else:
        kinds = {"kind": {"not": "SUBSCRIPTION"}}
    end_value = date_only_value(ctx.end_date)
    rows = await db.recurringpayment.find_many(
        where={"userId": ctx.user_id, "status": "ACTIVE", "nextDate": {"lte": end_value}, **kinds},
        include={"category": True},
        take=2000,
    )
    out = []
    for p in rows:
        nxt = date_only_str(p.nextDate)
        days = [nxt] if ctx.start_date <= nxt <= ctx.end_date else []
        after_next = add_days_str(nxt, 1)
        projected_from = after_next if after_next > ctx.start_date else ctx.start_date
        if projected_from <= ctx.end_date:
            for d in rrule.between(p.rrule, p.startDate, date_only_value(projected_from), end_value, [], date_only=True):
                s = date_only_str(d)
                if s > nxt and s not in days:
                    days.append(s)
        kind = "subscription" if p.kind == "SUBSCRIPTION" else "bill"
        for day in days:
            out.append(
                _item(
                    f"{kind}:{p.id}:{day}", kind, p.id, p.name, day, day, True,
                    p.category.color if p.category else None,
                    f"/finance/subscriptions?id={p.id}" if kind == "subscription" else ENTITY_LINKS["RECURRING_PAYMENT"](p.id),
                    None,
                    {
                        "amount": float(p.amount),
                        "currency": p.currency,
                        "kind": p.kind,
                        "isNext": day == nxt,
                        "overdue": day == nxt and day < ctx.today,
                        "daysUntil": diff_days_str(ctx.today, day),
                        "autoLogExpense": p.autoLogExpense,
                        "categoryName": p.category.name if p.category else None,
                        "categoryIcon": p.category.icon if p.category else None,
                    },
                )
            )
    return out


async def _people(ctx: Ctx, wanted: set) -> list[dict]:
    rows = await db.person.find_many(
        where={"userId": ctx.user_id, "OR": [{"birthday": {"not": None}}, {"anniversary": {"not": None}}]}, take=5000
    )
    out = []
    for p in rows:
        for year in _years(ctx.start_date, ctx.end_date):
            if p.birthday and "birthday" in wanted:
                day = anniversary_str(p.birthday, year)
                if ctx.start_date <= day <= ctx.end_date:
                    age = year - dates._utc(p.birthday).year if p.birthdayHasYear else None
                    if age is None or age >= 0:
                        out.append(
                            _item(
                                f"birthday:{p.id}:{day}", "birthday", p.id, _fmt(ctx.titles["birthday"], name=p.name),
                                day, day, True, p.avatarColor, ENTITY_LINKS["PERSON"](p.id), None,
                                {"personId": p.id, "personName": p.name, "age": age, "relationship": p.relationship, "daysUntil": diff_days_str(ctx.today, day)},
                            )
                        )
            if p.anniversary and "anniversary" in wanted:
                day = anniversary_str(p.anniversary, year)
                years = year - dates._utc(p.anniversary).year
                if ctx.start_date <= day <= ctx.end_date and years >= 0:
                    out.append(
                        _item(
                            f"anniversary:{p.id}:{day}", "anniversary", p.id, _fmt(ctx.titles["anniversary"], name=p.name),
                            day, day, True, p.avatarColor, ENTITY_LINKS["PERSON"](p.id), None,
                            {"personId": p.id, "personName": p.name, "years": years, "daysUntil": diff_days_str(ctx.today, day)},
                        )
                    )
    return out


async def _important_dates(ctx: Ctx) -> list[dict]:
    rows = await db.importantdate.find_many(
        where={
            "userId": ctx.user_id,
            "OR": [
                {"repeatsYearly": True},
                {"date": {"gte": date_only_value(ctx.start_date), "lte": date_only_value(ctx.end_date)}},
            ],
        },
        include={"person": True},
        take=5000,
    )
    out = []
    for d in rows:
        own = date_only_str(d.date)
        days = (
            [s for s in (anniversary_str(d.date, y) for y in _years(ctx.start_date, ctx.end_date)) if s >= own]
            if d.repeatsYearly
            else [own]
        )
        for day in days:
            if day < ctx.start_date or day > ctx.end_date:
                continue
            out.append(
                _item(
                    f"importantDate:{d.id}:{day}", "importantDate", d.id,
                    f"{d.title} · {d.person.name}" if d.person else d.title,
                    day, day, True, d.person.avatarColor if d.person else None,
                    ENTITY_LINKS["PERSON"](d.personId) if d.personId else "/people", None,
                    {
                        "personId": d.personId,
                        "personName": d.person.name if d.person else None,
                        "repeatsYearly": d.repeatsYearly,
                        "years": int(day[:4]) - dates._utc(d.date).year if d.repeatsYearly else None,
                        "notes": d.notes,
                        "daysUntil": diff_days_str(ctx.today, day),
                    },
                )
            )
    return out


async def _documents(ctx: Ctx) -> list[dict]:
    rows = await db.document.find_many(
        where={"userId": ctx.user_id, "expiryDate": {"gte": date_only_value(ctx.start_date), "lte": date_only_value(ctx.end_date)}},
        take=2000,
    )
    out = []
    for d in rows:
        day = date_only_str(d.expiryDate)
        out.append(
            _item(
                f"document:{d.id}", "document", d.id, _fmt(ctx.titles["documentExpires"], name=d.name), day, day, True,
                None, ENTITY_LINKS["DOCUMENT"](d.id), None,
                {"type": d.type, "name": d.name, "holderName": d.holderName, "daysRemaining": diff_days_str(ctx.today, day)},
            )
        )
    return out


async def _debts(ctx: Ctx) -> list[dict]:
    rows = await db.debt.find_many(
        where={"userId": ctx.user_id, "status": "OPEN", "dueDate": {"gte": date_only_value(ctx.start_date), "lte": date_only_value(ctx.end_date)}},
        include={"person": True, "payments": True},
        take=1000,
    )
    out = []
    for d in rows:
        day = date_only_str(d.dueDate)
        who = (d.person.name if d.person else None) or d.counterparty or d.title
        remaining = max(0, float(d.principal) - sum(float(p.amount) for p in d.payments or []))
        title = _fmt(ctx.titles["debtPay"], name=d.title) if d.direction == "I_OWE" else _fmt(ctx.titles["debtCollect"], who=who)
        out.append(
            _item(
                f"debt:{d.id}", "debt", d.id, title, day, day, True, None, ENTITY_LINKS["DEBT"](d.id), None,
                {"direction": d.direction, "name": d.title, "who": who, "remaining": remaining, "currency": d.currency, "overdue": day < ctx.today, "daysUntil": diff_days_str(ctx.today, day)},
            )
        )
    return out


async def _maintenance(ctx: Ctx) -> list[dict]:
    end_value = date_only_value(ctx.end_date)
    rows = await db.maintenanceitem.find_many(
        where={"userId": ctx.user_id, "isActive": True, "nextDueAt": {"lte": end_value}}, include={"category": True}, take=2000
    )
    out = []
    for m in rows:
        nxt = date_only_str(m.nextDueAt)
        days = {nxt} if ctx.start_date <= nxt <= ctx.end_date else set()
        projected_from = add_days_str(nxt, 1) if add_days_str(nxt, 1) > ctx.start_date else ctx.start_date
        if projected_from <= ctx.end_date:
            for d in rrule.between(m.rrule, m.nextDueAt, date_only_value(projected_from), end_value, [], date_only=True):
                days.add(date_only_str(d))
        for day in sorted(days):
            out.append(
                _item(
                    f"maintenance:{m.id}:{day}", "maintenance", m.id, m.title, day, day, True,
                    m.category.color if m.category else None, ENTITY_LINKS["MAINTENANCE"](m.id), None,
                    {
                        "area": m.area,
                        "isNext": day == nxt,
                        "projected": day != nxt,
                        "overdue": day == nxt and day < ctx.today,
                        "daysUntil": diff_days_str(ctx.today, day),
                        "estimatedCost": None if m.estimatedCost is None else float(m.estimatedCost),
                        "currency": m.currency,
                    },
                )
            )
    return out


async def _trips(ctx: Ctx) -> list[dict]:
    rows = await db.trip.find_many(
        where={
            "userId": ctx.user_id,
            "status": {"not": "CANCELLED"},
            "startDate": {"lte": date_only_value(ctx.end_date)},
            "endDate": {"gte": date_only_value(ctx.start_date)},
        },
        take=1000,
    )
    out = []
    for t in rows:
        start = date_only_str(t.startDate)
        end = start if date_only_str(t.endDate) < start else date_only_str(t.endDate)
        title = f"{t.name} · {t.destination}" if t.destination and t.destination.lower() not in t.name.lower() else t.name
        out.append(
            _item(
                f"trip:{t.id}", "trip", t.id, title, start, end, True, t.color, ENTITY_LINKS["TRIP"](t.id), None,
                {"destination": t.destination, "status": t.status, "days": diff_days_str(start, end) + 1, "daysUntil": diff_days_str(ctx.today, start)},
            )
        )
    return out


async def _trip_items(ctx: Ctx) -> list[dict]:
    rows = await db.tripitem.find_many(
        where={
            "trip": {"is": {"userId": ctx.user_id, "status": {"not": "CANCELLED"}}},
            "startAt": {"not": None, "lte": ctx.end},
            "OR": [{"endAt": {"gte": ctx.start}}, {"endAt": None, "startAt": {"gte": ctx.start}}],
        },
        include={"trip": True},
        take=2000,
    )
    out = []
    for i in rows:
        start = dates._utc(i.startAt)
        end = dates._utc(i.endAt) if i.endAt and dates._utc(i.endAt) >= start else start
        all_day = is_local_midnight(start, ctx.tz) and (not i.endAt or is_local_midnight(end, ctx.tz))
        out.append(
            _item(
                f"tripItem:{i.id}", "tripItem", i.id, i.title,
                local_date_str(start, ctx.tz) if all_day else _iso(start),
                local_date_str(end, ctx.tz) if all_day else _iso(end),
                all_day, i.trip.color, ENTITY_LINKS["TRIP"](i.tripId), None,
                {"type": i.type, "location": i.location, "tripId": i.tripId, "tripName": i.trip.name, "isDone": i.isDone},
            )
        )
    return out


async def _assessments(ctx: Ctx) -> list[dict]:
    rows = await db.assessment.find_many(
        where={"userId": ctx.user_id, "dueAt": {"gte": ctx.start, "lte": ctx.end}}, include={"course": True}, take=2000
    )
    out = []
    for a in rows:
        due = dates._utc(a.dueAt)
        all_day = is_local_midnight(due, ctx.tz)
        done = a.status in ("SUBMITTED", "GRADED")
        when = local_date_str(due, ctx.tz) if all_day else _iso(due)
        out.append(
            _item(
                f"assessment:{a.id}", "assessment", a.id, f"{a.course.code or a.course.name}: {a.title}", when, when, all_day,
                a.course.color, f"/study?id={a.courseId}&assessment={a.id}", "HIGH" if a.type == "EXAM" else None,
                {
                    "type": a.type,
                    "status": a.status,
                    "completed": done,
                    "overdue": not done and due < ctx.now,
                    "location": a.location,
                    "weight": None if a.weight is None else float(a.weight),
                    "courseId": a.courseId,
                    "courseName": a.course.name,
                },
            )
        )
    return out


async def _study_sessions(ctx: Ctx) -> list[dict]:
    rows = await db.studysession.find_many(
        where={"userId": ctx.user_id, "startAt": {"gte": ctx.start - DAY, "lte": ctx.end}}, include={"course": True}, take=2000
    )
    out = []
    for s in rows:
        start = dates._utc(s.startAt)
        end = start + timedelta(minutes=s.durationMinutes)
        if end < ctx.start:
            continue
        course_name = s.course.name if s.course else None
        title = _fmt(ctx.titles["studyTopic"], topic=s.topic or course_name) if s.topic or course_name else ctx.titles["studySession"]
        out.append(
            _item(
                f"studySession:{s.id}", "studySession", s.id, title, _iso(start), _iso(end), False,
                s.course.color if s.course else None,
                ENTITY_LINKS["COURSE"](s.courseId) if s.courseId else "/study", None,
                {"durationMinutes": s.durationMinutes, "topic": s.topic, "courseId": s.courseId, "courseName": course_name},
            )
        )
    return out


async def _reminders(ctx: Ctx) -> list[dict]:
    clauses = [
        {"isDone": False, "remindAt": {"lte": ctx.end}, "rrule": {"not": None}},
        {"isDone": False, "rrule": None, "remindAt": {"gte": ctx.start, "lte": ctx.end}},
        {"isDone": False, "rrule": None, "snoozedUntil": {"gte": ctx.start, "lte": ctx.end}},
    ]
    if ctx.include_completed:
        clauses.append({"isDone": True, "remindAt": {"gte": ctx.start, "lte": ctx.end}})
    rows = await db.reminder.find_many(where={"userId": ctx.user_id, "OR": clauses}, take=5000)
    out = []
    for r in rows:
        remind_at = dates._utc(r.remindAt)
        snoozed = dates._utc(r.snoozedUntil) if r.snoozedUntil else None
        all_day = is_local_midnight(remind_at, ctx.tz)
        if r.rrule and not r.isDone:
            instants = rrule.between(r.rrule, remind_at, ctx.start, ctx.end, [], tz=ctx.tz)
        else:
            at = snoozed if not r.isDone and snoozed else remind_at
            instants = [at] if ctx.start <= at <= ctx.end else []
        for at in instants:
            day = local_date_str(at, ctx.tz)
            is_all_day = all_day and not (snoozed and at == snoozed)
            out.append(
                _item(
                    f"reminder:{r.id}:{_iso(at)}" if r.rrule else f"reminder:{r.id}", "reminder", r.id, r.title,
                    day if is_all_day else _iso(at), day if is_all_day else _iso(at), is_all_day, None,
                    ENTITY_LINKS["REMINDER"](r.id), "HIGH" if r.isDeadline else None,
                    {
                        "notes": r.notes,
                        "isDeadline": r.isDeadline,
                        "done": r.isDone,
                        "completed": r.isDone,
                        "isRecurring": bool(r.rrule),
                        # the stored row is the NEXT occurrence; later ones are projections
                        "isNext": not r.rrule or at == remind_at,
                        "snoozed": bool(r.snoozedUntil),
                        "overdue": not r.isDone and not r.rrule and at <= ctx.now,
                        "entityType": r.entityType,
                        "entityId": r.entityId,
                    },
                )
            )
    return out


# ---------------------------------------------------------------------------


def _compare(a: dict, b: dict) -> int:
    """All-day first within a day, then by start, then title (String.localeCompare
    for titles: case-insensitive first)."""
    ka, kb = a["start"][:10], b["start"][:10]
    if a["allDay"] != b["allDay"] and ka == kb:
        return -1 if a["allDay"] else 1
    if a["start"] != b["start"]:
        return -1 if a["start"] < b["start"] else 1
    ta, tb = (a["title"].casefold(), a["title"]), (b["title"].casefold(), b["title"])
    return (ta > tb) - (ta < tb)


def _bound(value: str, tz: str, side: str) -> datetime:
    if is_date_only_string(value):
        start = local_midnight(value, tz)
        return start if side == "start" else start + DAY - _ONE_MS
    moment = dates.parse_instant(value)
    if moment is None:
        raise nest_error(400, "Invalid date")
    return moment


_FEED_FOR_QUERY = ("from", "to", "types", "includeCompleted")  # the NestJS query DTO's fields


async def feed_for(user, request: Request, include_completed: bool | None = None) -> dict:
    q = request.query_params
    errors = unknown_params(request, _FEED_FOR_QUERY)
    for name in ("from", "to"):
        value = q.get(name)
        if value is None or not dates.is_iso_date_string(value):
            errors.append(f"{name} must be a valid ISO 8601 date string")
    types = string_list(q.getlist("types"))
    if types and any(t not in SOURCE_TYPES for t in types):
        errors.append(f"each value in types must be one of the following values: {', '.join(SOURCE_TYPES)}")
    completed = _query_bool(request, "includeCompleted", errors)
    if errors:
        raise nest_error(400, errors)
    settings = await ensure_os_profile(user)
    tz = safe_tz(settings.timezone)
    start, end = _bound(q.get("from"), tz, "start"), _bound(q.get("to"), tz, "end")
    if end < start:
        raise nest_error(400, '"to" must be after "from"')
    if end - start > (MAX_RANGE_DAYS + 1) * DAY:
        raise nest_error(400, f"Range too large (max {MAX_RANGE_DAYS} days)")
    enabled = set(settings.enabledSections or [])
    wanted = {t for t in (types or SOURCE_TYPES) if SOURCE_SECTION[t] is None or SOURCE_SECTION[t] in enabled}
    ctx = Ctx(user.id, tz, to_lang(settings.language), start, end)
    choice = include_completed if include_completed is not None else completed
    ctx.include_completed = True if choice is None else choice

    has = lambda *kinds: any(k in wanted for k in kinds)  # noqa: E731
    parts = [
        await _events(ctx) if has("event") else [],
        await _tasks(ctx) if has("task") else [],
        await _recurring_payments(ctx, wanted) if has("bill", "subscription") else [],
        await _people(ctx, wanted) if has("birthday", "anniversary") else [],
        await _important_dates(ctx) if has("importantDate") else [],
        await _documents(ctx) if has("document") else [],
        await _maintenance(ctx) if has("maintenance") else [],
        await _trips(ctx) if has("trip") else [],
        await _trip_items(ctx) if has("tripItem") else [],
        await _assessments(ctx) if has("assessment") else [],
        await _study_sessions(ctx) if has("studySession") else [],
        await _reminders(ctx) if has("reminder") else [],
        await _debts(ctx) if has("debt") else [],
    ]
    items = sorted((item for part in parts for item in part), key=functools.cmp_to_key(_compare))
    return {"from": _iso(start), "to": _iso(end), "timezone": tz, "items": items, "total": len(items)}


@router.get("/feed")
async def feed(request: Request, user=Depends(plus_user)):
    return await feed_for(user, request)


@router.get("/summary")
async def summary(request: Request, user=Depends(plus_user)):
    """Per-day counts (year heatmap, busy days); completed tasks and done reminders don't count."""
    data = await feed_for(user, request, include_completed=False)
    tz = data["timezone"]
    start_date = local_date_str(dates.parse_instant(data["from"]), tz)
    end_date = local_date_str(dates.parse_instant(data["to"]), tz)
    days: dict[str, dict] = {}
    for item in data["items"]:
        first = item["start"] if item["allDay"] else local_date_str(dates.parse_instant(item["start"]), tz)
        last = item["end"] if item["allDay"] else local_date_str(dates.parse_instant(item["end"]), tz)
        last = max(last, first)
        # cap long spans (a three-week trip) to the visible range
        cursor = max(first, start_date)
        last = min(last, end_date)
        guard = 0
        while cursor <= last and guard < MAX_RANGE_DAYS + 2:
            guard += 1
            if start_date <= cursor <= end_date:
                day = days.setdefault(cursor, {"date": cursor, "count": 0, "sources": {}})
                day["count"] += 1
                day["sources"][item["sourceType"]] = day["sources"].get(item["sourceType"], 0) + 1
            cursor = add_days_str(cursor, 1)
    ordered = sorted(days.values(), key=lambda d: d["date"])
    return {
        "from": data["from"],
        "to": data["to"],
        "timezone": tz,
        "days": ordered,
        "total": sum(d["count"] for d in ordered),
        "maxCount": max((d["count"] for d in ordered), default=0),
    }
