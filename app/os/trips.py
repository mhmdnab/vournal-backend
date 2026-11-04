"""/api/trips and /api/public/trips (port of vournalOS TripsService, TripItemsService and
the packing templates): trips with their budget and spending, itinerary items, the
packing list, and the read-only share link.

A share link shows the plan only: never money, booking codes, private notes, the packing
list or tasks. It works with no session, and stops when its owner no longer has Plus
(spec "Public trip share links"); it goes with the account.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, Request

from app.db import db
from app.entitlements import effective_plan
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, choice, iso_date, nested, number, text
from app.os.common.currency import as_decimal, get_converter
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.jsparse import _JS_SPACE
from app.os.common.query import _query_bool, _query_list, unknown_params
from app.os.health_calc import _js_round
from app.os.profile import ensure_os_profile

router = nest_router("/trips")
public_router = nest_router("/public/trips")

TRIP_STATUSES = ("PLANNING", "BOOKED", "ONGOING", "COMPLETED", "CANCELLED")
ITEM_TYPES = ("TRANSPORT", "ACCOMMODATION", "RESERVATION", "PLACE", "ACTIVITY", "OTHER")
PACKING_PRESETS = ("basic", "beach", "city", "business", "winter")
_TRIP_FIELDS = ("id", "name", "destination", "startDate", "endDate", "status", "budget", "currency", "transportation",
                "accommodation", "notes", "color", "shareToken", "createdAt", "updatedAt")
_ITEM_FIELDS = ("id", "tripId", "type", "title", "startAt", "endAt", "location", "confirmationCode", "cost", "currency", "url",
                "notes", "isDone", "sortOrder", "createdAt", "updatedAt")
_PACKING_FIELDS = ("id", "tripId", "name", "quantity", "group", "isPacked", "sortOrder", "createdAt")
_ITEM_ORDER = [{"startAt": "asc"}, {"sortOrder": "asc"}, {"createdAt": "asc"}]  # nulls last, as Postgres sorts them
_PACKING_ORDER = [{"sortOrder": "asc"}, {"createdAt": "asc"}]
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}\Z")
_PACKING = json.loads((Path(__file__).parent / "data" / "packing_templates.json").read_text())  # golden/packing_golden.mjs


def _round2(n: float) -> float:
    return _js_round(n * 100) / 100


def _percent(part: float, whole: float) -> int:
    return int(_js_round(part / whole * 100))


# ----- packing templates ----------------------------------------------------------------


def packing_template(preset: str, lang: str) -> list[dict]:
    """The preset's items in the user's language, with each name in every language (to
    skip what's already on the list after a language switch)."""
    return [{"group": d["group"][lang], "name": d["name"][lang], "quantity": d["quantity"],
             "aliases": [d["name"]["en"].lower(), d["name"]["ar"].lower()]} for d in _PACKING["presets"][preset]]


def match_existing_group(template_group: str, existing: list[str]) -> str | None:
    """The label already used on the list for the same template group, in either
    language, so a second "Clothes" / "الملابس" group isn't made."""
    entry = next((g for g in _PACKING["groups"] if template_group in (g["en"], g["ar"])), None)
    if entry is None:
        return None
    for g in existing:
        if g.strip(_JS_SPACE).lower() in (entry["en"].lower(), entry["ar"].lower()):
            return g
    return None


# ----- views ---------------------------------------------------------------------------


def _phase(trip, tz: str) -> tuple[str, int, int]:
    until, until_end = dates.days_until(trip.startDate, tz), dates.days_until(trip.endDate, tz)
    return ("upcoming" if until > 0 else "ongoing" if until_end >= 0 else "past"), until, until_end


def decorate(trip, items, packing, spent_rows, fx, tz: str) -> dict:
    """A trip with its phase, budget use, packing progress and item counts."""
    currency = trip.currency or fx.default_currency
    phase, until, until_end = _phase(trip, tz)
    hint = trip.status
    if trip.status != "CANCELLED":
        if phase == "ongoing" and trip.status != "COMPLETED":
            hint = "ONGOING"
        elif phase == "past" and trip.status != "COMPLETED":
            hint = "COMPLETED"
        elif phase == "upcoming" and trip.status in ("ONGOING", "COMPLETED"):
            hint = "BOOKED"
    planned = 0.0
    for i in items:
        planned = planned + (0 if i.cost is None else fx.convert(i.cost, i.currency or currency, currency))
    spent = 0.0
    for amount, source in spent_rows:
        spent = spent + fx.convert(amount or 0, source, currency)
    planned, spent = _round2(planned), _round2(spent)
    budget = None if trip.budget is None else float(trip.budget)
    by_type = {t: 0 for t in ITEM_TYPES}
    for i in items:
        by_type[i.type] += 1
    packed = sum(1 for p in packing if p.isPacked)
    return {
        **{f: getattr(trip, f) for f in _TRIP_FIELDS},
        "currency": currency,
        "phase": phase,
        "statusHint": hint,
        "isOngoing": phase == "ongoing",
        "durationDays": dates.diff_days(trip.startDate, trip.endDate) + 1,
        "daysUntil": until,
        "daysUntilEnd": until_end,
        "currentDay": 1 - until if phase == "ongoing" else None,
        "plannedCost": planned,
        "spent": spent,
        "budgetRemaining": None if budget is None else _round2(budget - spent),
        "budgetUsedPercent": _percent(spent, budget) if budget else None,
        "isOverBudget": budget is not None and spent > budget,
        "packingProgress": {"packed": packed, "total": len(packing), "percent": _percent(packed, len(packing)) if packing else 0},
        "itemsByType": by_type,
        "itemsDone": sum(1 for i in items if i.isDone),
        "itemsTotal": len(items),
    }


def item_view(row) -> dict:
    return {f: getattr(row, f) for f in _ITEM_FIELDS}


def packing_view(row) -> dict:
    return {f: getattr(row, f) for f in _PACKING_FIELDS}


def _trip_days(start: datetime, end: datetime) -> list[str]:
    n = min(dates.diff_days(start, end), 365)
    return [dates.format_date_only(dates.add_days_date_only(start, i)) for i in range(n + 1)]


async def _spent_by_currency(user_id: str, trip_ids: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    if trip_ids:
        for g in await db.transaction.group_by(by=["tripId", "currency"], where={"userId": user_id, "type": "EXPENSE", "tripId": {"in": trip_ids}},
                                               sum={"amount": True}):
            out.setdefault(g["tripId"], []).append(((g.get("_sum") or {}).get("amount"), g["currency"]))
    return out


async def find_trip(user_id: str, id: str):
    row = await db.trip.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Trip not found")
    return row


async def summary(user, id: str) -> dict:
    """The decorated trip (a list row's shape), after item and packing changes."""
    trip = await db.trip.find_first(where={"id": id, "userId": user.id}, include={"items": True, "packingItems": True})
    if trip is None:
        raise nest_error(404, "Trip not found")
    tz = (await ensure_os_profile(user)).timezone
    spent = (await _spent_by_currency(user.id, [id])).get(id, [])
    return decorate(trip, trip.items, trip.packingItems, spent, await get_converter(user.id), tz)


# ----- bodies --------------------------------------------------------------------------


def _partial(rules: tuple) -> tuple:
    return tuple((name, True, trimmed, rule) for name, _optional, trimmed, rule in rules)


TRIP_RULES = (
    ("name", False, True, text(120, 1)),
    ("destination", True, True, text(200)),
    ("startDate", True, False, iso_date),
    ("endDate", True, False, iso_date),
    ("status", True, False, choice(TRIP_STATUSES)),
    ("budget", True, False, number(0, None, 2)),
    ("currency", True, False, choice(tuple(SUPPORTED_CURRENCIES))),
    ("transportation", True, False, text(2_000)),
    ("accommodation", True, False, text(2_000)),
    ("notes", True, False, text(20_000)),
    ("color", True, False, validators.color_rules),
)


class TripBody(RuleBody):
    RULES = TRIP_RULES
    name: object = None
    destination: object = None
    startDate: object = None
    endDate: object = None
    status: object = None
    budget: object = None
    currency: object = None
    transportation: object = None
    accommodation: object = None
    notes: object = None
    color: object = None


class TripUpdateBody(TripBody):
    RULES = _partial(TRIP_RULES)


ITEM_RULES = (
    ("title", False, True, text(200, 1)),
    ("type", True, False, choice(ITEM_TYPES)),
    ("startAt", True, False, iso_date),
    ("endAt", True, False, iso_date),
    ("location", True, True, text(300)),
    ("confirmationCode", True, True, text(100)),
    ("cost", True, False, number(0, None, 2)),
    ("currency", True, False, choice(tuple(SUPPORTED_CURRENCIES))),
    ("url", True, False, lambda v, f: validators.url_rules(v, f, 2_000)),
    ("notes", True, False, text(5_000)),
    ("isDone", True, False, validators.bool_rules),
    ("sortOrder", True, False, number(0, None)),
)


class ItemBody(RuleBody):
    RULES = ITEM_RULES
    title: object = None
    type: object = None
    startAt: object = None
    endAt: object = None
    location: object = None
    confirmationCode: object = None
    cost: object = None
    currency: object = None
    url: object = None
    notes: object = None
    isDone: object = None
    sortOrder: object = None


class ItemUpdateBody(ItemBody):
    RULES = _partial(ITEM_RULES)


PACKING_RULES = (
    ("name", False, True, text(120, 1)),
    ("quantity", True, False, number(1, 999)),
    ("group", True, True, text(60)),
    ("isPacked", True, False, validators.bool_rules),
    ("sortOrder", True, False, number(0, None)),
)


class PackingBody(RuleBody):
    RULES = PACKING_RULES
    name: object = None
    quantity: object = None
    group: object = None
    isPacked: object = None
    sortOrder: object = None


class PackingUpdateBody(PackingBody):
    RULES = _partial(PACKING_RULES)


class TemplateBody(RuleBody):
    RULES = (("preset", False, False, choice(PACKING_PRESETS)),)
    preset: object = None


def _not_empty_string(value, field):
    return ([] if value not in ("", None) else [f"{field} should not be empty"]) + validators.string_rules(value, field)


_REORDER_ENTRY = (
    ("id", False, False, _not_empty_string),
    ("sortOrder", False, False, number(0, None)),
    ("group", True, True, text(60)),  # packing only: move to another group (null = none)
)


def _reorder_items(value, field):
    """@IsArray() @ArrayMinSize(1) @ArrayMaxSize(1000) @ValidateNested({ each: true })."""
    each = nested(_REORDER_ENTRY)
    if not isinstance(value, list):
        return [*each(value, field), f"{field} must contain no more than 1000 elements",
                f"{field} must contain at least 1 elements", f"{field} must be an array"]
    sizes = [f"{field} must contain no more than 1000 elements"] if len(value) > 1000 else []
    sizes += [f"{field} must contain at least 1 elements"] if not value else []
    return sizes + each(value, field)


class ReorderBody(RuleBody):
    RULES = (("items", False, False, _reorder_items),)
    items: object = None

    def entries(self) -> list[dict]:
        return [{**e, "group": e["group"].strip(_JS_SPACE) if isinstance(e.get("group"), str) else e.get("group")} for e in self.items]


def _instant(value) -> datetime | None:
    return dates.parse_instant(value) if value else None


# ----- trips ---------------------------------------------------------------------------


@router.get("")
async def list_trips(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("when", "status", "q", "includeCancelled"))
    when = request.query_params.get("when")
    if when is not None and when not in ("all", "upcoming", "ongoing", "past", "active"):
        errors.append("when must be one of the following values: all, upcoming, ongoing, past, active")
    statuses = _query_list(request, "status", TRIP_STATUSES, errors)
    q = request.query_params.get("q")
    q = q.strip(_JS_SPACE) if q is not None else None
    if q is not None and len(q.encode("utf-16-le")) // 2 > 100:
        errors.append("q must be shorter than or equal to 100 characters")
    include_cancelled = _query_bool(request, "includeCancelled", errors)
    if errors:
        raise nest_error(400, errors)
    settings = await ensure_os_profile(user)
    today = dates.today_date_only(settings.timezone)
    where: dict = {"userId": user.id}
    both: list = []
    if statuses:
        where["status"] = {"in": statuses}
    if q:
        both.append({"OR": [{"name": {"contains": q, "mode": "insensitive"}}, {"destination": {"contains": q, "mode": "insensitive"}}]})
    when = when or "all"
    if when != "all" and not include_cancelled and not statuses:
        both.append({"status": {"not": "CANCELLED"}})
    if when == "upcoming":
        both.append({"startDate": {"gt": today}})
    if when == "ongoing":
        both.append({"startDate": {"lte": today}, "endDate": {"gte": today}})
    if when == "past":
        both.append({"endDate": {"lt": today}})
    if when == "active":
        both.append({"endDate": {"gte": today}})
    if both:
        where["AND"] = both
    trips = await db.trip.find_many(where=where, include={"items": True, "packingItems": True}, order={"startDate": "asc"})
    spent = await _spent_by_currency(user.id, [t.id for t in trips])
    fx = await get_converter(user.id)
    items = [decorate(t, t.items, t.packingItems, spent.get(t.id, []), fx, settings.timezone) for t in trips]
    rank = {"ongoing": 0, "upcoming": 1, "past": 2}
    # ongoing first, then upcoming (soonest first), then past (most recent first)
    items.sort(key=lambda t: (rank[t["phase"]], -t["startDate"].timestamp() if t["phase"] == "past" else t["startDate"].timestamp()))
    return {"items": items, "total": len(items)}


@router.post("", status_code=201)
async def create_trip(body: TripBody, user=Depends(plus_user)):
    settings = await ensure_os_profile(user)
    start = dates.parse_date_only(body.startDate) or dates.today_date_only(settings.timezone)
    end = dates.parse_date_only(body.endDate) or start
    if end < start:
        raise nest_error(400, "endDate must be on or after startDate")
    trip = await db.trip.create(data={
        "userId": user.id, "name": body.name, "destination": body.destination or "", "startDate": start, "endDate": end,
        "status": body.status or "PLANNING", "budget": None if body.budget is None else as_decimal(body.budget),
        "currency": body.currency or settings.defaultCurrency, "transportation": body.transportation,
        "accommodation": body.accommodation, "notes": body.notes, "color": body.color,
    })
    await log_activity(user.id, "trip.created", entity_id=trip.id, name=trip.name, destination=trip.destination,
                       startDate=dates.format_date_only(trip.startDate), endDate=dates.format_date_only(trip.endDate))
    return decorate(trip, [], [], [], await get_converter(user.id), settings.timezone)


@router.get("/{id}")
async def get_trip(id: str, user=Depends(plus_user)):
    trip = await find_trip(user.id, id)
    tz = (await ensure_os_profile(user)).timezone
    items = await db.tripitem.find_many(where={"tripId": id}, order=_ITEM_ORDER)
    packing = await db.packingitem.find_many(where={"tripId": id}, order=_PACKING_ORDER)
    trip_where = {"userId": user.id, "tripId": id}
    expenses = await db.transaction.find_many(where={**trip_where, "type": "EXPENSE"}, include={"category": True, "account": True},
                                              order={"date": "desc"}, take=100)
    sums = await db.transaction.group_by(by=["currency"], where={**trip_where, "type": "EXPENSE"}, sum={"amount": True}, count={"_all": True})
    events = await db.event.find_many(where=trip_where, order={"startAt": "asc"}, take=100)
    tasks = await db.task.find_many(where=trip_where, order=[{"status": "asc"}, {"dueAt": "asc"}], take=100)
    notes = await db.note.find_many(where={**trip_where, "isArchived": False}, order=[{"isPinned": "desc"}, {"updatedAt": "desc"}], take=50)
    fx = await get_converter(user.id)
    view = decorate(trip, items, packing, [((s.get("_sum") or {}).get("amount"), s["currency"]) for s in sums], fx, tz)
    now = datetime.now(timezone.utc)
    start_of_today = dates.start_of_day(now, tz)

    def overdue(t) -> bool:
        return t.status != "COMPLETED" and bool(t.dueAt) and (t.dueAt < now if t.hasDueTime else t.dueAt < start_of_today)

    return {
        **view,
        "days": _trip_days(trip.startDate, trip.endDate),
        "items": [{**item_view(i), "date": dates.local_date_string(i.startAt, tz) if i.startAt else None} for i in items],
        "packing": [packing_view(p) for p in packing],
        "expenses": [{
            "id": e.id, "type": e.type, "title": e.title, "amount": e.amount, "currency": e.currency, "date": e.date, "notes": e.notes,
            "paymentMethod": e.paymentMethod,
            "category": {"id": e.category.id, "name": e.category.name, "color": e.category.color, "icon": e.category.icon} if e.category else None,
            "account": {"id": e.account.id, "name": e.account.name} if e.account else None,
            "amountInTripCurrency": fx.convert(e.amount, e.currency, view["currency"]),
        } for e in expenses],
        "expensesCount": sum(s["_count"]["_all"] for s in sums),
        "events": [{f: getattr(e, f) for f in ("id", "title", "type", "startAt", "endAt", "allDay", "location", "rrule", "color")} for e in events],
        "tasks": [{**{f: getattr(t, f) for f in ("id", "title", "status", "priority", "dueAt", "hasDueTime", "completedAt")}, "isOverdue": overdue(t)}
                  for t in tasks],
        "notesList": [{"id": n.id, "title": n.title, "content": n.content.encode("utf-16-le")[:560].decode("utf-16-le", errors="ignore"),
                       "isPinned": n.isPinned, "updatedAt": n.updatedAt} for n in notes],
    }


@router.get("/{id}/summary")
async def get_summary(id: str, user=Depends(plus_user)):
    return await summary(user, id)


@router.patch("/{id}")
async def update_trip(id: str, body: TripUpdateBody, user=Depends(plus_user)):
    existing = await find_trip(user.id, id)
    start = dates.parse_date_only(body.startDate) if body.sent("startDate") else existing.startDate
    end = dates.parse_date_only(body.endDate) if body.sent("endDate") else existing.endDate
    if not start or not end:
        raise nest_error(400, "startDate and endDate are required")
    if end < start:
        raise nest_error(400, "endDate must be on or after startDate")
    data: dict = {"startDate": start, "endDate": end}
    for field in ("name", "status", "currency"):
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    if body.sent("destination"):
        data["destination"] = body.destination or ""
    if body.sent("budget"):
        data["budget"] = None if body.budget is None else as_decimal(body.budget)
    for field in ("transportation", "accommodation", "notes", "color"):
        if body.sent(field):
            data[field] = getattr(body, field)
    await db.trip.update(where={"id": existing.id}, data=data)
    if body.status is not None and body.status != existing.status:
        await log_activity(user.id, "trip.statusChanged", entity_id=id, name=body.name or existing.name, **{"from": existing.status, "to": body.status})
    else:
        await log_activity(user.id, "trip.updated", entity_id=id, name=body.name or existing.name)
    return await summary(user, id)


@router.delete("/{id}")
async def remove_trip(id: str, user=Depends(plus_user)):
    existing = await find_trip(user.id, id)
    await db.trip.delete(where={"id": existing.id})
    await log_activity(user.id, "trip.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


# ----- the share link -------------------------------------------------------------------


@router.post("/{id}/share")
async def share(id: str, user=Depends(plus_user)):
    """Turns sharing on (keeps the existing link when there is one)."""
    trip = await find_trip(user.id, id)
    token = trip.shareToken or secrets.token_urlsafe(24)
    if not trip.shareToken:
        await db.trip.update(where={"id": id}, data={"shareToken": token})
        await log_activity(user.id, "trip.shared", entity_id=id)
    return {"shareToken": token, "path": f"/share/trip/{token}"}


@router.delete("/{id}/share")
async def unshare(id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    await db.trip.update(where={"id": id}, data={"shareToken": None})
    await log_activity(user.id, "trip.unshared", entity_id=id)
    return {"ok": True}


@public_router.get("/{token}")
async def public_view(token: str):
    """What anyone with the link sees: the plan, and the owner's first name."""
    if not _TOKEN.match(token):
        raise nest_error(404, "Trip not found")
    trip = await db.trip.find_unique(where={"shareToken": token}, include={"user": True})
    if trip is None or effective_plan(trip.user) == "free":
        raise nest_error(404, "Trip not found")
    owner = await db.usersettings.find_unique(where={"userId": trip.userId})
    tz = owner.timezone if owner else "UTC"
    first_name = (owner.name or "").strip(_JS_SPACE).split()[0] if owner and (owner.name or "").strip(_JS_SPACE) else None
    items = await db.tripitem.find_many(where={"tripId": trip.id}, order=_ITEM_ORDER)
    return {
        "name": trip.name,
        "destination": trip.destination,
        "startDate": trip.startDate,
        "endDate": trip.endDate,
        "durationDays": dates.diff_days(trip.startDate, trip.endDate) + 1,
        "status": trip.status,
        "transportation": trip.transportation,
        "accommodation": trip.accommodation,
        "color": trip.color,
        "sharedBy": first_name,
        "timezone": tz,
        "days": _trip_days(trip.startDate, trip.endDate),
        "items": [{**{f: getattr(i, f) for f in ("id", "type", "title", "startAt", "endAt", "location", "url", "isDone")},
                   "date": dates.local_date_string(i.startAt, tz) if i.startAt else None} for i in items],
    }


# ----- itinerary items ------------------------------------------------------------------


async def _find_item(trip_id: str, item_id: str):
    row = await db.tripitem.find_first(where={"id": item_id, "tripId": trip_id})
    if row is None:
        raise nest_error(404, "Trip item not found")
    return row


def _assert_range(start, end) -> None:
    if start and end and end < start:
        raise nest_error(400, "endAt must be after startAt")


async def _assert_all_in(table, trip_id: str, ids: list[str], message: str) -> None:
    unique = list(dict.fromkeys(ids))
    if await table.count(where={"tripId": trip_id, "id": {"in": unique}}) != len(unique):
        raise nest_error(404, message)


@router.get("/{id}/items")
async def list_items(id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    items = [item_view(i) for i in await db.tripitem.find_many(where={"tripId": id}, order=_ITEM_ORDER)]
    return {"items": items, "total": len(items)}


@router.post("/{id}/items/reorder")
async def reorder_items(id: str, body: ReorderBody, user=Depends(plus_user)):
    await find_trip(user.id, id)
    entries = body.entries()
    await _assert_all_in(db.tripitem, id, [e["id"] for e in entries], "Trip item not found")
    async with db.tx() as tx:
        for e in entries:
            await tx.tripitem.update_many(where={"id": e["id"], "tripId": id}, data={"sortOrder": e["sortOrder"]})
    return {"ok": True}


@router.post("/{id}/items", status_code=201)
async def create_item(id: str, body: ItemBody, user=Depends(plus_user)):
    trip = await find_trip(user.id, id)
    start, end = _instant(body.startAt), _instant(body.endAt)
    _assert_range(start, end)
    sort_order = body.sortOrder
    if sort_order is None:
        last = await db.tripitem.find_first(where={"tripId": id}, order={"sortOrder": "desc"})
        sort_order = (last.sortOrder if last else -1) + 1
    item = await db.tripitem.create(data={
        "tripId": id, "type": body.type or "PLACE", "title": body.title, "startAt": start, "endAt": end, "location": body.location,
        "confirmationCode": body.confirmationCode, "cost": None if body.cost is None else as_decimal(body.cost),
        "currency": (body.currency or trip.currency) if body.cost is not None else body.currency,
        "url": body.url, "notes": body.notes, "isDone": bool(body.isDone), "sortOrder": sort_order,
    })
    await log_activity(user.id, "trip.itemCreated", entity_id=id, itemId=item.id, type=item.type, title=item.title)
    return item_view(item)


@router.patch("/{id}/items/{item_id}")
async def update_item(id: str, item_id: str, body: ItemUpdateBody, user=Depends(plus_user)):
    trip = await find_trip(user.id, id)
    existing = await _find_item(id, item_id)
    start = _instant(body.startAt) if body.sent("startAt") else existing.startAt
    end = _instant(body.endAt) if body.sent("endAt") else existing.endAt
    _assert_range(start, end)
    data: dict = {}
    for field in ("type", "title", "isDone", "sortOrder"):
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    if body.sent("startAt"):
        data["startAt"] = start
    if body.sent("endAt"):
        data["endAt"] = end
    for field in ("location", "confirmationCode", "url", "notes"):
        if body.sent(field):
            data[field] = getattr(body, field)
    if body.sent("cost"):
        data["cost"] = None if body.cost is None else as_decimal(body.cost)
    if body.sent("currency"):
        data["currency"] = body.currency
    elif body.cost is not None and not existing.currency:
        data["currency"] = trip.currency
    item = await db.tripitem.update(where={"id": existing.id}, data=data)
    await log_activity(user.id, "trip.itemUpdated", entity_id=id, itemId=item_id, title=item.title)
    return item_view(item)


@router.post("/{id}/items/{item_id}/toggle")
async def toggle_item(id: str, item_id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    existing = await _find_item(id, item_id)
    item = await db.tripitem.update(where={"id": existing.id}, data={"isDone": not existing.isDone})
    if item.isDone:
        await log_activity(user.id, "trip.itemDone", entity_id=id, itemId=item_id, title=item.title, type=item.type)
    return item_view(item)


@router.delete("/{id}/items/{item_id}")
async def remove_item(id: str, item_id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    existing = await _find_item(id, item_id)
    await db.tripitem.delete(where={"id": existing.id})
    await log_activity(user.id, "trip.itemDeleted", entity_id=id, itemId=item_id, title=existing.title)
    return {"ok": True}


# ----- packing list ---------------------------------------------------------------------


async def _find_packing(trip_id: str, item_id: str):
    row = await db.packingitem.find_first(where={"id": item_id, "tripId": trip_id})
    if row is None:
        raise nest_error(404, "Packing item not found")
    return row


async def _next_packing_order(trip_id: str) -> int:
    last = await db.packingitem.find_first(where={"tripId": trip_id}, order={"sortOrder": "desc"})
    return (last.sortOrder if last else -1) + 1


async def _packing_list(trip_id: str) -> dict:
    items = await db.packingitem.find_many(where={"tripId": trip_id}, order=_PACKING_ORDER)
    packed = sum(1 for i in items if i.isPacked)
    return {"items": [packing_view(i) for i in items], "total": len(items),
            "progress": {"packed": packed, "total": len(items), "percent": _percent(packed, len(items)) if items else 0}}


@router.get("/{id}/packing")
async def list_packing(id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    return await _packing_list(id)


@router.post("/{id}/packing/template")
async def apply_template(id: str, body: TemplateBody, user=Depends(plus_user)):
    """Adds the preset's common items in the user's language, skipping names already on
    the list (in either language), so re-applying after a language switch adds nothing twice."""
    await find_trip(user.id, id)
    lang = to_lang((await ensure_os_profile(user)).language)
    existing = await db.packingitem.find_many(where={"tripId": id})
    names = {e.name.strip(_JS_SPACE).lower() for e in existing}
    groups = list(dict.fromkeys(e.group for e in existing if e.group))
    order = await _next_packing_order(id)
    to_add = []
    for t in packing_template(body.preset, lang):
        if any(a in names for a in t["aliases"]):
            continue
        names.update(t["aliases"])
        to_add.append(t)
    if to_add:
        await db.packingitem.create_many(data=[
            {"tripId": id, "name": t["name"], "group": match_existing_group(t["group"], groups) or t["group"],
             "quantity": t["quantity"] or 1, "sortOrder": order + n}
            for n, t in enumerate(to_add)
        ])
    await log_activity(user.id, "trip.packingTemplateApplied", entity_id=id, preset=body.preset, added=len(to_add))
    return {"added": len(to_add), **await _packing_list(id)}


@router.post("/{id}/packing/reorder")
async def reorder_packing(id: str, body: ReorderBody, user=Depends(plus_user)):
    await find_trip(user.id, id)
    entries = body.entries()
    await _assert_all_in(db.packingitem, id, [e["id"] for e in entries], "Packing item not found")
    async with db.tx() as tx:
        for e, raw in zip(entries, body.items):
            data = {"sortOrder": e["sortOrder"], **({"group": e["group"] or None} if "group" in raw else {})}
            await tx.packingitem.update_many(where={"id": e["id"], "tripId": id}, data=data)
    return {"ok": True}


async def _set_all_packed(user, id: str, packed: bool) -> dict:
    await find_trip(user.id, id)
    return {"ok": True, "count": await db.packingitem.update_many(where={"tripId": id}, data={"isPacked": packed})}


@router.post("/{id}/packing/pack-all")
async def pack_all(id: str, user=Depends(plus_user)):
    return await _set_all_packed(user, id, True)


@router.post("/{id}/packing/unpack-all")
async def unpack_all(id: str, user=Depends(plus_user)):
    return await _set_all_packed(user, id, False)


@router.post("/{id}/packing", status_code=201)
async def create_packing(id: str, body: PackingBody, user=Depends(plus_user)):
    await find_trip(user.id, id)
    sort_order = body.sortOrder if body.sortOrder is not None else await _next_packing_order(id)
    row = await db.packingitem.create(data={
        "tripId": id, "name": body.name, "quantity": body.quantity or 1, "group": body.group or None,
        "isPacked": bool(body.isPacked), "sortOrder": sort_order,
    })
    return packing_view(row)


@router.patch("/{id}/packing/{item_id}")
async def update_packing(id: str, item_id: str, body: PackingUpdateBody, user=Depends(plus_user)):
    await find_trip(user.id, id)
    existing = await _find_packing(id, item_id)
    data: dict = {}
    for field in ("name", "quantity", "isPacked", "sortOrder"):
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    if body.sent("group"):
        data["group"] = body.group or None
    return packing_view(await db.packingitem.update(where={"id": existing.id}, data=data))


@router.post("/{id}/packing/{item_id}/toggle")
async def toggle_packing(id: str, item_id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    existing = await _find_packing(id, item_id)
    return packing_view(await db.packingitem.update(where={"id": existing.id}, data={"isPacked": not existing.isPacked}))


@router.delete("/{id}/packing/{item_id}")
async def remove_packing(id: str, item_id: str, user=Depends(plus_user)):
    await find_trip(user.id, id)
    existing = await _find_packing(id, item_id)
    await db.packingitem.delete(where={"id": existing.id})
    return {"ok": True}
