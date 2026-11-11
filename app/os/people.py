"""/api/people (port of vournalOS PeopleService): people with birthdays, anniversaries and
gift ideas, and what's coming up.

People are the app's `Person` rows (spec "Shared people"): everyone the app picked up
from a recording is listed here, with the OS fields empty until the web fills them. A
name follows the app's rule (`normalize_name`, unique per user), so adding someone who
already exists answers that person (200), and renaming onto another person's name is a
conflict. Each view carries `mentionCount`, the person's journal entries, so the
dashboard can ask before deleting someone who is mentioned.
"""

from fastapi import Depends, Request, Response
from prisma.errors import UniqueViolationError

from app.db import db
from app.entities import normalize_name
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, choice, iso_date, number, text
from app.os.common.collation import locale_key
from app.os.common.currency import as_decimal
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import fmt, to_lang
from app.os.common.jsparse import _JS_SPACE
from app.os.common.query import _query_int, unknown_params
from app.os.important_dates import next_occurrence, sort_key, with_placeholder_year
from app.os.important_dates import view as date_view
from app.os.profile import ensure_os_profile

router = nest_router("/people")

_FIELDS = ("id", "name", "relationship", "birthday", "birthdayHasYear", "anniversary", "email", "phone", "notes",
           "avatarColor", "reminderDaysBefore", "createdAt", "updatedAt")
_GIFT_FIELDS = ("id", "personId", "title", "price", "currency", "url", "occasion", "isPurchased", "notes", "createdAt")
_GIFT_ORDER = [{"isPurchased": "asc"}, {"createdAt": "desc"}]
UPCOMING_TITLES = {
    "en": {"birthday": "{name}'s birthday", "anniversary": "{name}'s anniversary"},
    "ar": {"birthday": "عيد ميلاد {name}", "anniversary": "الذكرى السنوية لـ {name}"},
}
_KIND_ORDER = {"birthday": 0, "anniversary": 1, "importantDate": 2}
AVATAR_COLORS = ("#6366f1", "#8b5cf6", "#ec4899", "#f43f5e", "#f97316", "#f59e0b", "#10b981", "#14b8a6", "#0ea5e9", "#3b82f6")
NAME_TAKEN = "Someone with this name already exists"


def pick_color(name: str) -> str:
    """A steady colour from the name. JavaScript's `ch.charCodeAt(0)` reads the first
    UTF-16 unit of each code point: the high surrogate for characters beyond U+FFFF."""
    h = 0
    for ch in name:
        cp = ord(ch)
        unit = cp if cp < 0x10000 else 0xD800 + ((cp - 0x10000) >> 10)
        h = (h * 31 + unit) % 2**32
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


def _empty_to_null(value):
    return None if value is None or value.strip(_JS_SPACE) == "" else value


def _normalized(name: str) -> str:
    return normalize_name(name) or name  # a name that folds to nothing still needs a key


# ----- views ---------------------------------------------------------------------------


async def _counts(user_id: str, ids: list[str]) -> dict[str, dict[str, int]]:
    """Gift ideas, open gift ideas, important dates, linked notes and journal mentions per person."""
    out = {i: {"gifts": 0, "open": 0, "dates": 0, "notes": 0, "mentions": 0} for i in ids}
    if not ids:
        return out
    where = {"personId": {"in": ids}}
    for key, table, extra in (("gifts", db.giftidea, {}), ("open", db.giftidea, {"isPurchased": False}),
                              ("dates", db.importantdate, {}), ("notes", db.note, {}), ("mentions", db.entryperson, {})):
        for g in await table.group_by(by=["personId"], where={**where, **extra}, count={"_all": True}):
            out[g["personId"]][key] = g["_count"]["_all"]
    return out


def view(row, tz: str, counts: dict) -> dict:
    out = {f: getattr(row, f) for f in _FIELDS}
    next_birthday = days_birthday = turning_age = age = None
    if row.birthday:
        occ = next_occurrence(row.birthday, True, tz, row.birthdayHasYear)
        next_birthday, days_birthday, turning_age = occ["nextDate"], occ["daysUntil"], occ["years"]
        if row.birthdayHasYear:
            years_at_next = dates._utc(next_birthday).year - dates._utc(row.birthday).year
            age = max(0, years_at_next if days_birthday == 0 else years_at_next - 1)
    next_anniversary = days_anniversary = years = None
    if row.anniversary:
        occ = next_occurrence(row.anniversary, True, tz)
        next_anniversary, days_anniversary, years = occ["nextDate"], occ["daysUntil"], occ["years"]
    return {
        **out,
        "nextBirthday": next_birthday,
        "daysUntilBirthday": days_birthday,
        "turningAge": turning_age,
        "age": age,
        "isBirthdayToday": days_birthday == 0,
        "nextAnniversary": next_anniversary,
        "daysUntilAnniversary": days_anniversary,
        "years": years,
        "giftCount": counts["gifts"],
        "openGiftCount": counts["open"],
        "importantDateCount": counts["dates"],
        "noteCount": counts["notes"],
        "mentionCount": counts["mentions"],
    }


async def _view_one(user_id: str, row, tz: str) -> dict:
    return view(row, tz, (await _counts(user_id, [row.id]))[row.id])


def gift_view(row) -> dict:
    return {f: getattr(row, f) for f in _GIFT_FIELDS}


# ----- bodies --------------------------------------------------------------------------

PERSON_RULES = (
    ("name", False, True, text(100, 1)),
    ("relationship", True, True, text(60)),
    ("birthday", True, False, iso_date),
    ("birthdayHasYear", True, False, validators.bool_rules),
    ("anniversary", True, False, iso_date),
    ("email", True, True, lambda v, f: validators.email_rules(v, f, 200)),
    ("phone", True, True, text(40)),
    ("notes", True, False, text(5000)),
    ("avatarColor", True, False, validators.color_rules),
    ("reminderDaysBefore", True, False, number(0, 365)),
)


class PersonBody(RuleBody):
    RULES = PERSON_RULES
    name: object = None
    relationship: object = None
    birthday: object = None
    birthdayHasYear: object = None
    anniversary: object = None
    email: object = None
    phone: object = None
    notes: object = None
    avatarColor: object = None
    reminderDaysBefore: object = None


class PersonUpdateBody(PersonBody):
    RULES = tuple((name, True, trimmed, rule) for name, _optional, trimmed, rule in PERSON_RULES)


GIFT_RULES = (
    ("title", False, True, text(200, 1)),
    ("price", True, False, number(0, None, 2)),
    ("currency", True, False, choice(tuple(SUPPORTED_CURRENCIES))),
    ("url", True, True, lambda v, f: validators.url_rules(v, f, 2000)),
    ("occasion", True, True, text(80)),
    ("isPurchased", True, False, validators.bool_rules),
    ("notes", True, False, text(2000)),
)


class GiftBody(RuleBody):
    RULES = GIFT_RULES
    title: object = None
    price: object = None
    currency: object = None
    url: object = None
    occasion: object = None
    isPurchased: object = None
    notes: object = None


class GiftUpdateBody(GiftBody):
    RULES = tuple((name, True, trimmed, rule) for name, _optional, trimmed, rule in GIFT_RULES)


# ----- people --------------------------------------------------------------------------


async def _find(user_id: str, id: str):
    row = await db.person.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Person not found")
    return row


def _assert_not_future(birthday, has_year: bool, tz: str) -> None:
    if birthday and has_year and birthday > dates.today_date_only(tz):
        raise nest_error(400, "birthday cannot be in the future")


def _query_text(request: Request, name: str, maximum: int, errors: list) -> str | None:
    value = request.query_params.get(name)
    if value is not None and len(value.encode("utf-16-le")) // 2 > maximum:
        errors.append(f"{name} must be shorter than or equal to {maximum} characters")
    return value


@router.get("")
async def list_people(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("q", "relationship", "sort"))
    q = _query_text(request, "q", 100, errors)
    relationship = _query_text(request, "relationship", 60, errors)
    sort = request.query_params.get("sort")
    if sort is not None and sort not in ("name", "birthday"):
        errors.append("sort must be one of the following values: name, birthday")
    if errors:
        raise nest_error(400, errors)
    tz = (await ensure_os_profile(user)).timezone
    where: dict = {"userId": user.id}
    if relationship:
        where["relationship"] = {"equals": relationship, "mode": "insensitive"}
    if q:
        where["OR"] = [{f: {"contains": q, "mode": "insensitive"}} for f in ("name", "relationship", "email")]
    rows = await db.person.find_many(where=where, order={"name": "asc"})
    counts = await _counts(user.id, [r.id for r in rows])
    items = [view(r, tz, counts[r.id]) for r in rows]
    if sort == "birthday":
        no_birthday = 2**53 - 1  # Number.MAX_SAFE_INTEGER
        items.sort(key=lambda p: (no_birthday if p["daysUntilBirthday"] is None else p["daysUntilBirthday"], locale_key(p["name"])))
    return {"items": items, "total": len(items)}


@router.get("/upcoming")
async def upcoming(request: Request, user=Depends(plus_user)):
    """Birthdays, anniversaries and important dates in the next `days` days (default 60)."""
    errors = unknown_params(request, ("days",))
    days = _query_int(request, "days", errors, maximum=366, minimum=0)
    if errors:
        raise nest_error(400, errors)
    days = 60 if days is None else days
    settings = await ensure_os_profile(user)
    tz, titles = settings.timezone, UPCOMING_TITLES[to_lang(settings.language)]
    people = await db.person.find_many(where={"userId": user.id, "OR": [{"birthday": {"not": None}}, {"anniversary": {"not": None}}]})
    important = await db.importantdate.find_many(where={"userId": user.id}, include={"person": True})

    items = []

    def add(kind, source, person, title, occ, turning_age, years):
        if 0 <= occ["daysUntil"] <= days:
            items.append({
                "key": f"{kind}:{source}", "kind": kind, "sourceId": source,
                "personId": person.id if person else None, "personName": person.name if person else None,
                "avatarColor": person.avatarColor if person else None, "title": title, "date": occ["nextDate"],
                "daysUntil": occ["daysUntil"], "turningAge": turning_age, "years": years,
            })

    for p in people:
        if p.birthday:
            occ = next_occurrence(p.birthday, True, tz, p.birthdayHasYear)
            add("birthday", p.id, p, fmt(titles["birthday"], name=p.name), occ, occ["years"], None)
        if p.anniversary:
            occ = next_occurrence(p.anniversary, True, tz)
            add("anniversary", p.id, p, fmt(titles["anniversary"], name=p.name), occ, None, occ["years"])
    for d in important:
        occ = next_occurrence(d.date, d.repeatsYearly, tz)
        add("importantDate", d.id, d.person, d.title, occ, None, occ["years"])
    items.sort(key=lambda i: (i["daysUntil"], _KIND_ORDER[i["kind"]], locale_key(i["title"])))
    return {"items": items, "total": len(items)}


@router.post("", status_code=201)
async def create(body: PersonBody, response: Response, user=Depends(plus_user)):
    tz = (await ensure_os_profile(user)).timezone
    key = _normalized(body.name)
    existing = await db.person.find_unique(where={"userId_normalizedName": {"userId": user.id, "normalizedName": key}})
    if existing is not None:  # adding someone who exists opens that person
        response.status_code = 200
        return await _view_one(user.id, existing, tz)
    has_year = True if body.birthdayHasYear is None else body.birthdayHasYear
    birthday = dates.parse_date_only(body.birthday)
    if birthday and not has_year:
        birthday = with_placeholder_year(birthday)
    _assert_not_future(birthday, has_year, tz)
    try:
        row = await db.person.create(data={
            "userId": user.id,
            "name": body.name,
            "normalizedName": key,
            "relationship": _empty_to_null(body.relationship),
            "birthday": birthday,
            "birthdayHasYear": has_year,
            "anniversary": dates.parse_date_only(body.anniversary),
            "email": _empty_to_null(body.email),
            "phone": _empty_to_null(body.phone),
            "notes": _empty_to_null(body.notes),
            "avatarColor": body.avatarColor or pick_color(body.name),
            "reminderDaysBefore": body.reminderDaysBefore if body.sent("reminderDaysBefore") else 3,
        })
    except UniqueViolationError:  # made by a recording (or another tab) a moment ago
        response.status_code = 200
        row = await db.person.find_unique_or_raise(where={"userId_normalizedName": {"userId": user.id, "normalizedName": key}})
        return await _view_one(user.id, row, tz)
    await log_activity(user.id, "person.created", entity_id=row.id, name=row.name)
    return await _view_one(user.id, row, tz)


@router.get("/{id}")
async def get_person(id: str, user=Depends(plus_user)):
    """The person with their gift ideas, important dates and linked notes."""
    row = await _find(user.id, id)
    tz = (await ensure_os_profile(user)).timezone
    gifts = await db.giftidea.find_many(where={"personId": row.id}, order=_GIFT_ORDER)
    important = await db.importantdate.find_many(where={"userId": user.id, "personId": row.id}, include={"person": True})
    notes = await db.note.find_many(where={"userId": user.id, "workspaceId": None, "personId": row.id}, order={"updatedAt": "desc"}, take=100)
    return {
        **await _view_one(user.id, row, tz),
        "gifts": [gift_view(g) for g in gifts],
        "importantDates": sorted((date_view(d, tz) for d in important), key=sort_key),
        "linkedNotes": [{"id": n.id, "title": n.title, "updatedAt": n.updatedAt, "isArchived": n.isArchived} for n in notes],
    }


@router.patch("/{id}")
async def update(id: str, body: PersonUpdateBody, user=Depends(plus_user)):
    existing = await _find(user.id, id)
    tz = (await ensure_os_profile(user)).timezone
    if body.sent("name") and body.name is None:
        raise nest_error(400, "name cannot be empty")
    has_year = existing.birthdayHasYear if body.birthdayHasYear is None else body.birthdayHasYear
    birthday_sent = body.sent("birthday")
    birthday = dates.parse_date_only(body.birthday) if birthday_sent else None
    if not has_year:
        # the year is now unknown: normalise the new birthday, or the existing one
        base = birthday if birthday_sent else existing.birthday
        birthday, birthday_sent = (with_placeholder_year(base) if base else base), birthday_sent or base is not None
    _assert_not_future(birthday if birthday_sent else None, has_year, tz)

    data: dict = {}
    if body.name is not None:
        key = _normalized(body.name)
        if key != existing.normalizedName:
            if await db.person.count(where={"userId": user.id, "normalizedName": key, "id": {"not": existing.id}}):
                raise nest_error(409, NAME_TAKEN)
            data["normalizedName"] = key
            # the old name stays a known spelling, as the app's merge keeps them
            known = {key, *(normalize_name(a) for a in existing.aliases or [])}
            if normalize_name(existing.name) not in known:
                data["aliases"] = [*(existing.aliases or []), existing.name]
        data["name"] = body.name
    for field in ("relationship", "email", "phone", "notes"):
        if body.sent(field):
            data[field] = _empty_to_null(getattr(body, field))
    if birthday_sent:
        data["birthday"] = birthday
    if body.birthdayHasYear is not None:
        data["birthdayHasYear"] = body.birthdayHasYear
    if body.sent("anniversary"):
        data["anniversary"] = dates.parse_date_only(body.anniversary)
    for field in ("avatarColor", "reminderDaysBefore"):
        if body.sent(field):
            data[field] = getattr(body, field)
    try:
        row = await db.person.update(where={"id": existing.id}, data=data)
    except UniqueViolationError:
        raise nest_error(409, NAME_TAKEN) from None
    await log_activity(user.id, "person.updated", entity_id=row.id, name=row.name)
    return await _view_one(user.id, row, tz)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    """Deletes the person with their gift ideas and important dates; linked notes and
    debts stay, unlinked, and journal entries only lose the link."""
    existing = await _find(user.id, id)
    async with db.tx() as tx:
        await tx.reminder.update_many(
            where={"userId": user.id, "entityType": "PERSON", "entityId": existing.id},
            data={"entityType": None, "entityId": None},
        )
        await tx.person.delete(where={"id": existing.id})
    await log_activity(user.id, "person.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


# ----- gift ideas (scoped through the person) -------------------------------------------


async def _find_gift(user_id: str, person_id: str, gift_id: str):
    gift = await db.giftidea.find_first(where={"id": gift_id, "personId": person_id, "person": {"is": {"userId": user_id}}})
    if gift is None:
        raise nest_error(404, "Gift idea not found")
    return gift


@router.get("/{id}/gifts")
async def list_gifts(id: str, user=Depends(plus_user)):
    person = await _find(user.id, id)
    items = [gift_view(g) for g in await db.giftidea.find_many(where={"personId": person.id}, order=_GIFT_ORDER)]
    return {"items": items, "total": len(items)}


@router.post("/{id}/gifts", status_code=201)
async def create_gift(id: str, body: GiftBody, user=Depends(plus_user)):
    person = await _find(user.id, id)
    currency = body.currency
    if currency is None and body.price is not None:
        currency = (await ensure_os_profile(user)).defaultCurrency
    gift = await db.giftidea.create(data={
        "personId": person.id,
        "title": body.title,
        "price": None if body.price is None else as_decimal(body.price),
        "currency": currency,
        "url": _empty_to_null(body.url),
        "occasion": _empty_to_null(body.occasion),
        "isPurchased": bool(body.isPurchased),
        "notes": _empty_to_null(body.notes),
    })
    await log_activity(user.id, "person.giftAdded", entity_id=person.id, giftId=gift.id, title=gift.title)
    return gift_view(gift)


@router.patch("/{id}/gifts/{gift_id}")
async def update_gift(id: str, gift_id: str, body: GiftUpdateBody, user=Depends(plus_user)):
    gift = await _find_gift(user.id, id, gift_id)
    if body.sent("title") and body.title is None:
        raise nest_error(400, "title cannot be empty")
    data: dict = {}
    if body.title is not None:
        data["title"] = body.title
    if body.sent("price"):
        data["price"] = None if body.price is None else as_decimal(body.price)
    if body.sent("currency"):
        data["currency"] = body.currency
    elif body.price is not None and not gift.currency:
        data["currency"] = (await ensure_os_profile(user)).defaultCurrency
    for field in ("url", "occasion", "notes"):
        if body.sent(field):
            data[field] = _empty_to_null(getattr(body, field))
    if body.isPurchased is not None:
        data["isPurchased"] = body.isPurchased
    updated = await db.giftidea.update(where={"id": gift.id}, data=data)
    if body.isPurchased is not None and body.isPurchased != gift.isPurchased:
        verb = "giftPurchased" if body.isPurchased else "giftUnpurchased"
    else:
        verb = "giftUpdated"
    await log_activity(user.id, f"person.{verb}", entity_id=gift.personId, giftId=gift.id, title=updated.title)
    return gift_view(updated)


@router.post("/{id}/gifts/{gift_id}/toggle")
async def toggle_gift(id: str, gift_id: str, user=Depends(plus_user)):
    gift = await _find_gift(user.id, id, gift_id)
    updated = await db.giftidea.update(where={"id": gift.id}, data={"isPurchased": not gift.isPurchased})
    verb = "giftPurchased" if updated.isPurchased else "giftUnpurchased"
    await log_activity(user.id, f"person.{verb}", entity_id=gift.personId, giftId=gift.id, title=gift.title)
    return gift_view(updated)


@router.delete("/{id}/gifts/{gift_id}")
async def remove_gift(id: str, gift_id: str, user=Depends(plus_user)):
    gift = await _find_gift(user.id, id, gift_id)
    await db.giftidea.delete(where={"id": gift.id})
    await log_activity(user.id, "person.giftDeleted", entity_id=gift.personId, giftId=gift.id, title=gift.title)
    return {"ok": True}
