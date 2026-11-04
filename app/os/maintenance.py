"""/api/maintenance (port of vournalOS MaintenanceService and maintenance.templates):
things that come back on a schedule (an oil change every 6 months), their history, and
one-click templates in the user's language.

Deleting an item deletes its attachments too; their files live in R2 here (NestJS kept
the bytes in the row), so the objects are removed after the commit.
"""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, Request

from app.db import db
from app.os import attachments
from app.os import finance_utils as fu
from app.os.common import dates, rrule, validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, choice, iso_date, number, text
from app.os.common.currency import as_decimal
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.jsparse import _JS_SPACE
from app.os.common.ownership import assert_owned
from app.os.common.query import _query_bool, _query_list, unknown_params
from app.os.health_calc import _js_round
from app.os.profile import ensure_os_profile

router = nest_router("/maintenance")

AREAS = ("HOME", "CAR", "PERSONAL", "HEALTH", "OTHER")
STATUSES = ("overdue", "dueSoon", "upcoming")
DUE_SOON_DAYS = 7
_DATA = json.loads((Path(__file__).parent / "data" / "maintenance_templates.json").read_text())  # golden/maintenance_golden.mjs
TEMPLATES = {t["key"]: t for t in _DATA["templates"]}
_ITEM_FIELDS = ("id", "title", "description", "area", "rrule", "lastDoneAt", "nextDueAt", "reminderDaysBefore", "estimatedCost",
                "currency", "categoryId", "isActive", "notes", "createdAt", "updatedAt")
_LOG_FIELDS = ("id", "itemId", "doneAt", "cost", "currency", "notes", "transactionId", "createdAt")


def _title_keys(t: dict) -> list[str]:
    """A template's title in every language, lower-cased (a language switch never duplicates)."""
    return [t["title"]["en"].strip(_JS_SPACE).lower(), t["title"]["ar"].strip(_JS_SPACE).lower()]


def _localized(t: dict, lang: str) -> dict:
    doc = t.get("suggestDocument")
    return {
        "title": t["title"][lang],
        "description": t["description"][lang] if t.get("description") else None,
        "groupLabel": _DATA["groupLabels"][t["group"]][lang],
        "suggestDocument": {"type": doc["type"], "name": doc["name"][lang]} if doc else None,
    }


# ----- views ---------------------------------------------------------------------------


def item_view(row, tz: str, lang: str) -> dict:
    days = dates.days_until(row.nextDueAt, tz)
    overdue = days < 0
    soon = not overdue and days <= DUE_SOON_DAYS
    category = row.category
    return {
        **{f: getattr(row, f) for f in _ITEM_FIELDS},
        "category": {"id": category.id, "name": category.name, "color": category.color, "icon": category.icon} if category else None,
        "logCount": len(row.logs or []),
        "daysUntilDue": days,
        "isOverdue": overdue,
        "isDueSoon": soon,
        "status": "overdue" if overdue else "dueSoon" if soon else "upcoming",
        "frequencyLabel": rrule.describe(row.rrule, lang),
    }


def log_view(row) -> dict:
    t = row.transaction
    return {**{f: getattr(row, f) for f in _LOG_FIELDS},
            "transaction": {f: getattr(t, f) for f in ("id", "amount", "currency", "date", "accountId", "categoryId")} if t else None}


_INCLUDE = {"category": True, "logs": True}


async def _context(user) -> tuple:
    settings = await ensure_os_profile(user)
    return settings, settings.timezone, to_lang(settings.language)


async def _find(user_id: str, id: str):
    row = await db.maintenanceitem.find_first(where={"id": id, "userId": user_id}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Maintenance item not found")
    return row


async def _assert_maintenance_category(user_id: str, category_id) -> None:
    if not category_id:
        return
    category = await db.category.find_first(where={"id": category_id, "userId": user_id})
    if category is None:
        raise nest_error(404, "Category not found")
    if category.scope != "MAINTENANCE":
        raise nest_error(400, "Category must have scope MAINTENANCE")


# ----- bodies --------------------------------------------------------------------------


def _rrule_rules(value, field):
    """@IsRRule()."""
    problem = rrule.validate_rrule(value)
    return [f"{field} {problem}"] if problem else []


def _partial(rules: tuple) -> tuple:
    return tuple((name, True, trimmed, rule) for name, _optional, trimmed, rule in rules)


ITEM_RULES = (
    ("title", False, True, text(120, 1)),
    ("description", True, False, text(2_000)),
    ("area", True, False, choice(AREAS)),
    ("rrule", False, False, _rrule_rules),
    ("nextDueAt", True, False, iso_date),
    ("lastDoneAt", True, False, iso_date),
    ("reminderDaysBefore", True, False, number(0, 365)),
    ("estimatedCost", True, False, number(0, None, 2)),
    ("currency", True, False, choice(tuple(SUPPORTED_CURRENCIES))),
    ("categoryId", True, False, validators.string_rules),
    ("isActive", True, False, validators.bool_rules),
    ("notes", True, False, text(5_000)),
)


class ItemBody(RuleBody):
    RULES = ITEM_RULES
    title: object = None
    description: object = None
    area: object = None
    rrule: object = None
    nextDueAt: object = None
    lastDoneAt: object = None
    reminderDaysBefore: object = None
    estimatedCost: object = None
    currency: object = None
    categoryId: object = None
    isActive: object = None
    notes: object = None


class ItemUpdateBody(ItemBody):
    RULES = _partial(ITEM_RULES)


class DoneBody(RuleBody):
    RULES = (
        ("date", True, False, iso_date),
        ("cost", True, False, number(0, None, 2)),
        ("currency", True, False, choice(tuple(SUPPORTED_CURRENCIES))),
        ("notes", True, False, text(2_000)),
        ("logExpense", True, False, validators.bool_rules),
        ("accountId", True, False, validators.string_rules),
    )
    date: object = None
    cost: object = None
    currency: object = None
    notes: object = None
    logExpense: object = None
    accountId: object = None


def _js_string(value) -> str:
    """String(value), as @Type(() => String) turned each key into one."""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if isinstance(value, float) and value.is_integer() and math.isfinite(value) else str(value)
    if isinstance(value, list):
        return ",".join("" if v is None else _js_string(v) for v in value)
    return "[object Object]"


def _keys_rules(value, field):
    """@IsArray() @ArrayMinSize(1) @ArrayMaxSize(50) @IsString({ each: true }) @Type(() => String)."""
    if not isinstance(value, list):
        each = [] if isinstance(value, str) else [f"each value in {field} must be a string"]
        return [*each, f"{field} must contain no more than 50 elements", f"{field} must contain at least 1 elements", f"{field} must be an array"]
    return ([f"{field} must contain no more than 50 elements"] if len(value) > 50 else []) + (
        [f"{field} must contain at least 1 elements"] if not value else [])


class TemplatesBody(RuleBody):
    RULES = (("keys", False, False, _keys_rules), ("startDate", True, False, iso_date))
    keys: object = None
    startDate: object = None


# ----- items ---------------------------------------------------------------------------


@router.get("")
async def list_items(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("area", "status", "active", "q", "categoryId"))
    areas = _query_list(request, "area", AREAS, errors)
    statuses = _query_list(request, "status", STATUSES, errors)
    active = _query_bool(request, "active", errors)
    q = request.query_params.get("q")
    if q is not None and len(q.encode("utf-16-le")) // 2 > 100:
        errors.append("q must be shorter than or equal to 100 characters")
    if errors:
        raise nest_error(400, errors)
    _, tz, lang = await _context(user)
    today = dates.today_date_only(tz)
    soon = dates.add_days_date_only(today, DUE_SOON_DAYS)
    where: dict = {"userId": user.id}
    if areas:
        where["area"] = {"in": areas}
    if active is not None:
        where["isActive"] = active
    if request.query_params.get("categoryId"):
        where["categoryId"] = request.query_params["categoryId"]
    if q:
        where["OR"] = [{f: {"contains": q, "mode": "insensitive"}} for f in ("title", "description", "notes")]
    if statuses:
        ranges = {"overdue": {"nextDueAt": {"lt": today}}, "dueSoon": {"nextDueAt": {"gte": today, "lte": soon}},
                  "upcoming": {"nextDueAt": {"gt": soon}}}
        where["AND"] = [{"OR": [ranges[s] for s in statuses]}]
    rows = await db.maintenanceitem.find_many(where=where, include=_INCLUDE, order=[{"nextDueAt": "asc"}, {"title": "asc"}])
    return {"items": [item_view(r, tz, lang) for r in rows], "total": len(rows)}


@router.get("/templates")
async def list_templates(user=Depends(plus_user)):
    """Templates in the user's language; `added` = an item with that title exists (either language)."""
    _, _, lang = await _context(user)
    titles = {i.title.strip(_JS_SPACE).lower() for i in await db.maintenanceitem.find_many(where={"userId": user.id})}
    items = []
    for t in _DATA["templates"]:
        words = _localized(t, lang)
        items.append({
            "key": t["key"], "group": t["group"], "groupLabel": words["groupLabel"], "title": words["title"], "area": t["area"],
            "rrule": t["rrule"], "frequencyLabel": rrule.describe(t["rrule"], lang), "description": words["description"],
            "reminderDaysBefore": t.get("reminderDaysBefore", 3), "suggestDocument": words["suggestDocument"],
            "added": not t.get("suggestDocument") and any(k in titles for k in _title_keys(t)),
        })
    return {"items": items, "total": len(items)}


@router.post("/templates", status_code=201)
async def add_templates(body: TemplatesBody, user=Depends(plus_user)):
    """Creates the chosen templates as items titled in the user's language."""
    _, tz, lang = await _context(user)
    keys = [_js_string(k) for k in body.keys]
    unknown = [k for k in keys if k not in TEMPLATES]
    if unknown:
        raise nest_error(400, f"Unknown template key(s): {', '.join(unknown)}")
    next_due = dates.parse_date_only(body.startDate) or dates.today_date_only(tz)
    titles = {i.title.strip(_JS_SPACE).lower() for i in await db.maintenanceitem.find_many(where={"userId": user.id})}
    categories = await db.category.find_many(where={"userId": user.id, "scope": "MAINTENANCE", "parentId": None})
    by_name: dict[str, str] = {}
    for c in categories:
        by_name[c.name.lower()] = c.id  # the last of equal names wins, as a Map built from the list did
    created, skipped = [], []
    for key in dict.fromkeys(keys):
        t = TEMPLATES[key]
        words = _localized(t, lang)
        if t.get("suggestDocument"):
            skipped.append({"key": key, "reason": "useDocuments", "suggestDocument": words["suggestDocument"]})
            continue
        if any(k in titles for k in _title_keys(t)):
            skipped.append({"key": key, "reason": "exists"})
            continue
        category_id = next((by_name[k] for k in _DATA["categoryKeys"].get(t.get("categoryName"), []) if k in by_name), None)
        row = await db.maintenanceitem.create(data={
            "userId": user.id, "title": words["title"], "description": words["description"], "area": t["area"], "rrule": t["rrule"],
            "nextDueAt": next_due, "reminderDaysBefore": t.get("reminderDaysBefore", 3), "categoryId": category_id,
        }, include=_INCLUDE)
        titles.add(words["title"].strip(_JS_SPACE).lower())
        created.append(item_view(row, tz, lang))
        await log_activity(user.id, "maintenance.created", entity_id=row.id, title=row.title, area=row.area, template=key)
    return {"items": created, "total": len(created), "skipped": skipped}


@router.post("", status_code=201)
async def create(body: ItemBody, user=Depends(plus_user)):
    settings, tz, lang = await _context(user)
    await _assert_maintenance_category(user.id, body.categoryId)
    rule = rrule.normalize_rrule(body.rrule)
    last_done = dates.parse_date_only(body.lastDoneAt)
    next_due = dates.parse_date_only(body.nextDueAt)
    if next_due is None:
        next_due = (rrule.next_occurrence(rule, last_done, last_done) or dates.today_date_only(tz)) if last_done else dates.today_date_only(tz)
    currency = body.currency or (settings.defaultCurrency if body.estimatedCost is not None else None)
    row = await db.maintenanceitem.create(data={
        "userId": user.id, "title": body.title, "description": body.description, "area": body.area or "HOME", "rrule": rule,
        "lastDoneAt": last_done, "nextDueAt": next_due,
        "reminderDaysBefore": body.reminderDaysBefore if body.sent("reminderDaysBefore") else 3,
        "estimatedCost": None if body.estimatedCost is None else as_decimal(body.estimatedCost), "currency": currency,
        "categoryId": body.categoryId, "isActive": True if body.isActive is None else body.isActive, "notes": body.notes,
    }, include=_INCLUDE)
    await log_activity(user.id, "maintenance.created", entity_id=row.id, title=row.title, area=row.area)
    return item_view(row, tz, lang)


@router.get("/{id}")
async def get_item(id: str, user=Depends(plus_user)):
    _, tz, lang = await _context(user)
    return item_view(await _find(user.id, id), tz, lang)


@router.patch("/{id}")
async def update(id: str, body: ItemUpdateBody, user=Depends(plus_user)):
    existing = await _find(user.id, id)
    settings, tz, lang = await _context(user)
    await _assert_maintenance_category(user.id, body.categoryId)
    if body.sent("nextDueAt") and body.nextDueAt is None:
        raise nest_error(400, "nextDueAt cannot be empty")
    if body.sent("rrule") and body.rrule is None:
        raise nest_error(400, "rrule cannot be empty")
    if any(body.sent(f) and getattr(body, f) is None for f in ("title", "area", "isActive")):
        raise invalid_data()  # Prisma refused null on a required column
    data: dict = {}
    for field in ("title", "description", "area", "reminderDaysBefore", "categoryId", "isActive", "notes"):
        if body.sent(field):
            data[field] = getattr(body, field)
    if body.sent("rrule"):
        data["rrule"] = rrule.normalize_rrule(body.rrule)
    if body.sent("lastDoneAt"):
        data["lastDoneAt"] = dates.parse_date_only(body.lastDoneAt)
    if body.sent("nextDueAt") and (parsed := dates.parse_date_only(body.nextDueAt)):
        data["nextDueAt"] = parsed
    if body.sent("estimatedCost"):
        data["estimatedCost"] = None if body.estimatedCost is None else as_decimal(body.estimatedCost)
    if body.sent("currency"):
        data["currency"] = body.currency
    elif body.estimatedCost is not None and not existing.currency:
        data["currency"] = settings.defaultCurrency
    row = await db.maintenanceitem.update(where={"id": existing.id}, data=data, include=_INCLUDE)
    await log_activity(user.id, "maintenance.updated", entity_id=row.id, title=row.title)
    return item_view(row, tz, lang)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _find(user.id, id)
    async with db.tx() as tx:
        # the user's reminders stay, just unlinked from the deleted item
        await tx.reminder.update_many(where={"userId": user.id, "entityType": "MAINTENANCE", "entityId": existing.id},
                                      data={"entityType": None, "entityId": None})
        keys = await attachments.delete_rows(tx, user.id, "MAINTENANCE", [existing.id])
        await tx.maintenanceitem.delete(where={"id": existing.id})
    await attachments.delete_objects(keys)
    await log_activity(user.id, "maintenance.deleted", entity_id=id, title=existing.title)
    return {"ok": True}


# ----- done / history ------------------------------------------------------------------


async def _expense_category(user_id: str, area: str) -> str | None:
    """The first EXPENSE category named like the area's candidates (either language)."""
    candidates = _DATA["expenseCandidates"].get(area, [])
    if not candidates:
        return None
    found = await db.category.find_many(where={"userId": user_id, "scope": "EXPENSE", "OR": [
        {"name": {"equals": name, "mode": "insensitive"}} for name in candidates]})
    for name in candidates:
        matches = [c for c in found if c.name.lower() == name.lower()]
        top = next((c for c in matches if c.parentId is None), matches[0] if matches else None)
        if top:
            return top.id
    return None


def _date_only_to_instant(day: datetime, today: datetime, tz: str) -> datetime:
    """A transaction's moment: now when it's today, else 12:00 local that day."""
    return datetime.now(timezone.utc) if day == today else fu.date_only_to_noon(day, dates.valid_zone(tz))


@router.post("/{id}/done")
async def mark_done(id: str, body: DoneBody, user=Depends(plus_user)):
    existing = await _find(user.id, id)
    settings, tz, lang = await _context(user)
    await assert_owned("account", body.accountId, user.id)
    today = dates.today_date_only(tz)
    done_at = dates.parse_date_only(body.date) or today
    cost = body.cost
    currency = (body.currency or existing.currency or settings.defaultCurrency) if cost is not None else None
    # only the latest completion moves the schedule on; an older one just adds history
    latest = existing.lastDoneAt is None or done_at >= existing.lastDoneAt
    next_due = rrule.next_occurrence(existing.rrule, done_at, done_at, date_only=True) if latest else None
    log_expense = bool(body.logExpense) and cost is not None and cost > 0
    category_id = await _expense_category(user.id, existing.area) if log_expense else None
    async with db.tx() as tx:
        transaction_id = None
        if log_expense:
            trx = await tx.transaction.create(data={
                "userId": user.id, "type": "EXPENSE", "title": existing.title, "amount": as_decimal(cost), "currency": currency,
                "date": _date_only_to_instant(done_at, today, tz), "categoryId": category_id, "accountId": body.accountId,
                "notes": body.notes,
            })
            transaction_id = trx.id
        log = await tx.maintenancelog.create(data={
            "itemId": existing.id, "userId": user.id, "doneAt": done_at, "cost": None if cost is None else as_decimal(cost),
            "currency": currency, "notes": body.notes, "transactionId": transaction_id,
        }, include={"transaction": True})
        if latest:
            # a series that has ended switches the item off
            data = {"lastDoneAt": done_at, **({"nextDueAt": next_due} if next_due else {"isActive": False})}
            item = await tx.maintenanceitem.update(where={"id": existing.id}, data=data, include=_INCLUDE)
        else:
            item = await tx.maintenanceitem.find_unique_or_raise(where={"id": existing.id}, include=_INCLUDE)
    if transaction_id:
        await log_activity(user.id, "transaction.created", entity_id=transaction_id, type="EXPENSE", amount=cost, currency=currency,
                           source="maintenance")
    await log_activity(user.id, "maintenance.done", entity_id=existing.id, title=existing.title, doneAt=dates.format_date_only(done_at),
                       cost=cost, currency=currency, transactionId=transaction_id)
    return {"item": item_view(item, tz, lang), "log": log_view(log)}


@router.get("/{id}/logs")
async def logs(id: str, user=Depends(plus_user)):
    item = await _find(user.id, id)
    rows = await db.maintenancelog.find_many(where={"userId": user.id, "itemId": item.id}, include={"transaction": True},
                                             order=[{"doneAt": "desc"}, {"createdAt": "desc"}])
    total = 0.0
    for r in rows:
        total = total + (float(r.cost) if r.cost else 0)
    return {"items": [log_view(r) for r in rows], "total": len(rows), "totalCost": _js_round(total * 100) / 100}


@router.delete("/{id}/logs/{log_id}")
async def remove_log(id: str, log_id: str, user=Depends(plus_user)):
    """Removes a history entry (its expense, if any, stays: delete it in Finance)."""
    item = await _find(user.id, id)
    log = await db.maintenancelog.find_first(where={"id": log_id, "userId": user.id, "itemId": item.id})
    if log is None:
        raise nest_error(404, "Maintenance log not found")
    await db.maintenancelog.delete(where={"id": log.id})
    latest = await db.maintenancelog.find_first(where={"userId": user.id, "itemId": item.id}, order={"doneAt": "desc"})
    await db.maintenanceitem.update(where={"id": item.id}, data={"lastDoneAt": latest.doneAt if latest else None})
    await log_activity(user.id, "maintenance.logDeleted", entity_id=item.id, logId=log_id)
    return {"ok": True}
