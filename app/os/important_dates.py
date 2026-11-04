"""/api/important-dates (port of vournalOS ImportantDatesService and important-dates.util):
yearly or one-off dates, optionally about a person, sorted by their next occurrence."""

from datetime import datetime

from fastapi import Depends, Request

from app.db import db
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, iso_date, number, text
from app.os.common.collation import locale_key
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.ownership import assert_owned
from app.os.common.query import unknown_params
from app.os.profile import ensure_os_profile

router = nest_router("/important-dates")

NO_YEAR_PLACEHOLDER = 2000  # the year stored when it's unknown (a leap year, so Feb 29 stays valid)
_FIELDS = ("id", "personId", "title", "date", "repeatsYearly", "reminderDaysBefore", "notes", "createdAt")


def with_placeholder_year(value: datetime) -> datetime:
    return value.replace(year=NO_YEAR_PLACEHOLDER)


def next_occurrence(value: datetime, repeats_yearly: bool, tz: str, has_year: bool = True) -> dict:
    """{nextDate, daysUntil, years}: the next occurrence (the date itself when one-off),
    days from the user's today (negative = past), and full years at it (repeating dates
    with a known year)."""
    next_date = dates.next_anniversary(value, tz) if repeats_yearly else value
    days_until = dates.days_until(next_date, tz)
    diff = dates._utc(next_date).year - dates._utc(value).year
    return {"nextDate": next_date, "daysUntil": days_until, "years": diff if repeats_yearly and has_year and diff > 0 else None}


def sort_key(item: dict) -> tuple:
    """Upcoming first (soonest), then past one-off dates (most recent first); ties by title."""
    past = item["daysUntil"] < 0
    return (past, -item["daysUntil"] if past else item["daysUntil"], locale_key(item["title"]))


def person_brief(person) -> dict | None:
    return {"id": person.id, "name": person.name, "avatarColor": person.avatarColor} if person else None


def view(row, tz: str) -> dict:
    occ = next_occurrence(row.date, row.repeatsYearly, tz)
    out = {f: getattr(row, f) for f in _FIELDS}
    return {**out, "person": person_brief(row.person), **occ, "isPast": occ["daysUntil"] < 0}


# ----- bodies --------------------------------------------------------------------------

RULES = (
    ("title", False, True, text(120, 1)),
    ("date", False, False, iso_date),
    ("personId", True, False, validators.string_rules),
    ("repeatsYearly", True, False, validators.bool_rules),
    ("reminderDaysBefore", True, False, number(0, 365)),
    ("notes", True, False, text(2000)),
)


class CreateBody(RuleBody):
    RULES = RULES
    title: object = None
    date: object = None
    personId: object = None
    repeatsYearly: object = None
    reminderDaysBefore: object = None
    notes: object = None


class UpdateBody(CreateBody):
    RULES = tuple((name, True, trimmed, rule) for name, _optional, trimmed, rule in RULES)  # PartialType


# ----- routes --------------------------------------------------------------------------


async def _tz(user) -> str:
    return (await ensure_os_profile(user)).timezone


async def _find(user_id: str, id: str):
    row = await db.importantdate.find_first(where={"id": id, "userId": user_id}, include={"person": True})
    if row is None:
        raise nest_error(404, "Important date not found")
    return row


@router.get("")
async def list_dates(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("personId",))
    if errors:
        raise nest_error(400, errors)
    tz = await _tz(user)
    where = {"userId": user.id}
    if request.query_params.get("personId"):
        where["personId"] = request.query_params["personId"]
    rows = await db.importantdate.find_many(where=where, include={"person": True})
    items = sorted((view(r, tz) for r in rows), key=sort_key)
    return {"items": items, "total": len(items)}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    await assert_owned("person", body.personId, user.id)
    date = dates.parse_date_only(body.date)
    if date is None:
        raise nest_error(400, "date must be a valid date (YYYY-MM-DD)")
    row = await db.importantdate.create(
        data={
            "userId": user.id,
            "personId": body.personId,
            "title": body.title,
            "date": date,
            "repeatsYearly": True if body.repeatsYearly is None else body.repeatsYearly,
            "reminderDaysBefore": body.reminderDaysBefore if body.sent("reminderDaysBefore") else 3,
            "notes": body.notes,
        },
        include={"person": True},
    )
    await log_activity(user.id, "importantDate.created", entity_id=row.id, title=row.title, personId=row.personId,
                       date=dates.format_date_only(row.date))
    return view(row, await _tz(user))


@router.get("/{id}")
async def get_date(id: str, user=Depends(plus_user)):
    return view(await _find(user.id, id), await _tz(user))


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _find(user.id, id)
    await assert_owned("person", body.personId, user.id)
    if body.sent("date") and body.date is None:
        raise nest_error(400, "date cannot be empty")
    if body.sent("title") and body.title is None:
        raise nest_error(400, "title cannot be empty")
    data: dict = {}
    if body.sent("date"):
        parsed = dates.parse_date_only(body.date)
        if parsed is None:
            raise nest_error(400, "date must be a valid date (YYYY-MM-DD)")
        data["date"] = parsed
    for field in ("personId", "title", "reminderDaysBefore", "notes"):
        if body.sent(field):
            data[field] = getattr(body, field)
    if body.repeatsYearly is not None:
        data["repeatsYearly"] = body.repeatsYearly
    row = await db.importantdate.update(where={"id": existing.id}, data=data, include={"person": True})
    await log_activity(user.id, "importantDate.updated", entity_id=row.id, title=row.title)
    return view(row, await _tz(user))


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _find(user.id, id)
    async with db.tx() as tx:
        await tx.reminder.update_many(
            where={"userId": user.id, "entityType": "IMPORTANT_DATE", "entityId": existing.id},
            data={"entityType": None, "entityId": None},
        )
        await tx.importantdate.delete(where={"id": existing.id})
    await log_activity(user.id, "importantDate.deleted", entity_id=id, title=existing.title)
    return {"ok": True}
