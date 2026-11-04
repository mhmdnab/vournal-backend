"""/api/shopping (port of vournalOS ShoppingService, shopping.helpers and messages).
Lists can be shared with the owner's household: members open them and add or tick
items, only the owner edits the list itself."""

import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.currency import Converter, get_converter, round_money
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import Lang, default_name, fmt, plural, to_lang
from app.os.common.ownership import assert_many_owned, assert_owned
from app.os.common.query import _query_bool, _query_int, unknown_params
from app.os.profile import ensure_os_profile
from app.os.tasks import PRIORITIES
from app.os.transactions import as_decimal

router = nest_router("/shopping")

CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
PriorityName = Literal[PRIORITIES]  # type: ignore[valid-type]
_LIST_FIELDS = ("id", "name", "icon", "color", "currency", "isArchived", "sortOrder", "householdId", "createdAt", "updatedAt")
_ITEM_FIELDS = (
    "id", "listId", "name", "quantity", "unit", "estimatedPrice", "actualPrice", "currency", "store", "priority",
    "isPurchased", "purchasedAt", "notes", "sourceRecipeId", "sortOrder", "createdAt", "updatedAt",
)

# ----- messages (shopping.messages) ---------------------------------------------------------

_MESSAGES = {
    "en": {
        "expenseTitle": "Shopping: {list}",
        "expenseNotes": {"one": '{count} item from shopping list "{list}"', "other": '{count} items from shopping list "{list}"'},
    },
    "ar": {
        "expenseTitle": "تسوّق: {list}",
        "expenseNotes": {
            "one": "غرض واحد من قائمة التسوّق «{list}»",
            "two": "غرضان من قائمة التسوّق «{list}»",
            "few": "{count} أغراض من قائمة التسوّق «{list}»",
            "many": "{count} غرضًا من قائمة التسوّق «{list}»",
            "other": "{count} غرض من قائمة التسوّق «{list}»",
        },
    },
}


def expense_title(lang: Lang, list_name: str) -> str:
    return fmt(_MESSAGES[lang]["expenseTitle"], list=list_name)


def expense_notes(lang: Lang, count: int, list_name: str) -> str:
    return plural(lang, count, _MESSAGES[lang]["expenseNotes"], list=list_name)


# ----- helpers (shopping.helpers, household.access) -----------------------------------------


async def household_id_of(user_id: str) -> str | None:
    member = await db.householdmember.find_unique(where={"userId": user_id})
    return member.householdId if member else None


def list_access(user_id: str, household_id: str | None) -> dict:
    """Lists the user may open: their own, plus the ones shared with their household."""
    return {"OR": [{"userId": user_id}, {"householdId": household_id}]} if household_id else {"userId": user_id}


async def resolve_shopping_list(user_id: str, list_id: str | None, household_id: str | None = None) -> dict:
    """The list a new item goes into (shared with the meal plan): the given one (yours or
    shared with you), else your first active list, else a new "Grocery" list."""
    if list_id:
        access = {"OR": [{"userId": user_id}, {"householdId": household_id}]} if household_id else {"userId": user_id}
        found = await db.shoppinglist.find_first(where={"id": list_id, **access})
        if found is None:
            raise nest_error(404, "Shopping list not found")
        return {"id": found.id, "name": found.name, "currency": found.currency, "created": False}
    first = await db.shoppinglist.find_first(where={"userId": user_id, "isArchived": False}, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    if first:
        return {"id": first.id, "name": first.name, "currency": first.currency, "created": False}
    settings = await db.usersettings.find_unique(where={"userId": user_id})
    made = await db.shoppinglist.create(
        data={
            "userId": user_id, "name": default_name("Grocery", to_lang(settings.language if settings else None)),
            "icon": "shopping-basket", "color": "#22c55e", "currency": settings.defaultCurrency if settings else None,
        }
    )
    return {"id": made.id, "name": made.name, "currency": made.currency, "created": True}


async def next_item_sort_order(list_id: str) -> int:
    """The end of a list (by list, not creator: a shared list has several people's items)."""
    top = await db.shoppingitem.find_first(where={"listId": list_id}, order={"sortOrder": "desc"})
    return (top.sortOrder if top else -1) + 1


def ingredient_key(name: str, unit: str | None) -> str:
    """Case-insensitive merge key for (name, unit)."""
    return f"{re.sub(r'\s+', ' ', name.strip().lower())}|{(unit or '').strip().lower()}"


# ----- views ---------------------------------------------------------------------------------


def _totals(target: str, items, fx: Converter) -> dict:
    estimated = actual = remaining = 0
    purchased = 0
    for it in items:
        currency = it.currency or target
        est = fx.convert(it.estimatedPrice, currency, target) if it.estimatedPrice is not None else 0
        estimated += est
        if it.isPurchased:
            purchased += 1
            if it.actualPrice is not None:
                actual += fx.convert(it.actualPrice, currency, target)
        else:
            remaining += est
    return {
        "totalsCurrency": target,
        "itemCount": len(items),
        "purchasedCount": purchased,
        "remainingCount": len(items) - purchased,
        "estimatedTotal": round_money(estimated),
        "actualTotal": round_money(actual),
        "remainingEstimated": round_money(remaining),
    }


async def _owner_names(lists) -> dict[str, str | None]:
    ids = list({l.userId for l in lists})
    return {s.userId: s.name for s in await db.usersettings.find_many(where={"userId": {"in": ids}})} if ids else {}


def _list_view(user_id: str, row, totals: dict, names: dict) -> dict:
    is_owner = row.userId == user_id
    return {
        **{f: getattr(row, f) for f in _LIST_FIELDS}, **totals,
        "isShared": bool(row.householdId), "isOwner": is_owner, "ownerName": None if is_owner else names.get(row.userId),
    }


async def _with_totals(user_id: str, row) -> dict:
    fx = await get_converter(user_id)
    items = await db.shoppingitem.find_many(where={"listId": row.id})
    return _list_view(user_id, row, _totals(row.currency or fx.default_currency, items, fx), await _owner_names([row]))


def _item_view(row) -> dict:
    out = {f: getattr(row, f) for f in _ITEM_FIELDS}
    out["sourceRecipe"] = {"id": row.sourceRecipe.id, "name": row.sourceRecipe.name} if row.sourceRecipe else None
    return out


async def _own_list(user_id: str, id: str):
    row = await db.shoppinglist.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Shopping list not found")
    return row


async def _accessible_list(user_id: str, id: str):
    row = await db.shoppinglist.find_first(where={"id": id, **list_access(user_id, await household_id_of(user_id))})
    if row is None:
        raise nest_error(404, "Shopping list not found")
    return row


async def _item(user_id: str, id: str):
    """Anyone with access to the list may edit its items."""
    row = await db.shoppingitem.find_first(
        where={"id": id, "list": {"is": list_access(user_id, await household_id_of(user_id))}}, include={"sourceRecipe": True}
    )
    if row is None:
        raise nest_error(404, "Shopping item not found")
    return row


# ----- bodies --------------------------------------------------------------------------------


class _ListFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    icon: StrictStr | None = None
    color: StrictStr | None = None
    currency: CurrencyCode | None = None
    isArchived: StrictBool | None = None
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 60)

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        if v is not None and len(v) > 50:
            raise ValueError("icon must be shorter than or equal to 50 characters")
        return validators.icon(v)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)


class ListCreate(_ListFields):
    name: StrictStr


class ListUpdate(_ListFields):
    shared: StrictBool | None = None


class ExpenseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accountId: StrictStr | None = None
    categoryId: StrictStr | None = None
    date: StrictStr | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError("date must be a valid ISO 8601 date string")
        return v


def _price(v, field):
    return validators.money(v, field, 0)


class _ItemFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listId: StrictStr | None = None
    name: StrictStr | None = None
    quantity: object = None
    unit: StrictStr | None = None
    estimatedPrice: object = None
    actualPrice: object = None
    currency: CurrencyCode | None = None
    store: StrictStr | None = None
    priority: PriorityName | None = None
    isPurchased: StrictBool | None = None
    notes: StrictStr | None = Field(default=None, max_length=2000)
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 200)

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, v):
        if v is not None and validators.money(v, "quantity", 0.01) > 99_999_999:
            raise ValueError("quantity must not be greater than 99999999")
        return v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        return validators.trimmed(v, "unit", 0, 30)

    @field_validator("store")
    @classmethod
    def _store(cls, v):
        return validators.trimmed(v, "store", 0, 80)

    @field_validator("estimatedPrice", "actualPrice")
    @classmethod
    def _prices(cls, v, info):
        return _price(v, info.field_name)


class ItemCreate(_ItemFields):
    name: StrictStr


class ItemUpdate(_ItemFields):
    pass


class ReorderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1)
    sortOrder: StrictInt = Field(ge=0)


class ReorderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReorderEntry] = Field(min_length=1, max_length=1000)


# ----- lists ---------------------------------------------------------------------------------


@router.get("/lists")
async def list_lists(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("includeArchived", "archived"))
    include_archived = _query_bool(request, "includeArchived", errors)
    archived = _query_bool(request, "archived", errors)
    if errors:
        raise nest_error(400, errors)
    where: dict = {"AND": [list_access(user.id, await household_id_of(user.id))]}
    if archived is not None:
        where["isArchived"] = archived
    elif not include_archived:
        where["isArchived"] = False
    lists = await db.shoppinglist.find_many(where=where, order=[{"isArchived": "asc"}, {"sortOrder": "asc"}, {"createdAt": "asc"}])
    if not lists:
        return {"items": [], "total": 0}
    items = await db.shoppingitem.find_many(where={"listId": {"in": [l.id for l in lists]}})
    fx = await get_converter(user.id)
    by_list: dict[str, list] = {}
    for it in items:
        by_list.setdefault(it.listId, []).append(it)
    names = await _owner_names(lists)
    views = [_list_view(user.id, l, _totals(l.currency or fx.default_currency, by_list.get(l.id, []), fx), names) for l in lists]
    return {"items": views, "total": len(views)}


@router.post("/lists/reorder")
async def reorder_lists(body: ReorderBody, user=Depends(plus_user)):
    await assert_many_owned("shoppinglist", [i.id for i in body.items], user.id)
    async with db.batch_() as batch:
        for item in body.items:
            batch.shoppinglist.update_many(where={"id": item.id, "userId": user.id}, data={"sortOrder": item.sortOrder})
    return {"ok": True}


@router.post("/lists", status_code=201)
async def create_list(body: ListCreate, user=Depends(plus_user)):
    sort_order = body.sortOrder
    if sort_order is None:
        top = await db.shoppinglist.find_first(where={"userId": user.id}, order={"sortOrder": "desc"})
        sort_order = (top.sortOrder if top else -1) + 1
    row = await db.shoppinglist.create(
        data={"userId": user.id, "name": body.name, "icon": body.icon, "color": body.color, "currency": body.currency,
              "isArchived": bool(body.isArchived), "sortOrder": sort_order}
    )
    await log_activity(user.id, "shoppingList.created", entity_id=row.id, name=row.name)
    return await _with_totals(user.id, row)


@router.get("/lists/{id}")
async def get_list(id: str, user=Depends(plus_user)):
    return await _with_totals(user.id, await _accessible_list(user.id, id))


@router.patch("/lists/{id}")
async def update_list(id: str, body: ListUpdate, user=Depends(plus_user)):
    existing = await _own_list(user.id, id)
    sent = body.model_fields_set
    data: dict = {}
    if "shared" in sent:
        household = await household_id_of(user.id) if body.shared else None
        if body.shared and not household:
            raise nest_error(400, "Create or join a household first to share a list")
        data["householdId"] = household
    if any(f in sent and getattr(body, f) is None for f in ("name", "isArchived", "sortOrder")):
        raise invalid_data()  # Prisma refused null on a required column
    for field in ("name", "icon", "color", "currency", "isArchived", "sortOrder"):
        if field in sent:
            data[field] = getattr(body, field)
    row = await db.shoppinglist.update(where={"id": existing.id}, data=data or {"name": existing.name})
    verb = "updated"
    if "isArchived" in sent and body.isArchived != existing.isArchived:
        verb = "archived" if body.isArchived else "unarchived"
    await log_activity(user.id, f"shoppingList.{verb}", entity_id=id, name=row.name)
    return await _with_totals(user.id, row)


@router.delete("/lists/{id}")
async def remove_list(id: str, user=Depends(plus_user)):
    existing = await _own_list(user.id, id)
    await db.shoppinglist.delete(where={"id": existing.id})  # items cascade
    await log_activity(user.id, "shoppingList.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


@router.post("/lists/{id}/clear-purchased")
async def clear_purchased(id: str, user=Depends(plus_user)):
    row = await _accessible_list(user.id, id)
    deleted = await db.shoppingitem.delete_many(where={"listId": row.id, "isPurchased": True})
    await log_activity(user.id, "shoppingList.cleared", entity_id=id, deleted=deleted)
    return {"ok": True, "deleted": deleted}


@router.post("/lists/{id}/log-expense")
async def log_expense(id: str, body: ExpenseBody, user=Depends(plus_user)):
    """An EXPENSE transaction for the list's actual total (purchased items)."""
    row = await _own_list(user.id, id)
    totals = await _with_totals(user.id, row)
    if totals["actualTotal"] <= 0:
        raise nest_error(400, "Nothing to log: no purchased items with an actual price")
    await assert_owned("account", body.accountId, user.id)
    if body.categoryId:
        category = await db.category.find_first(where={"id": body.categoryId, "userId": user.id})
        if category is None:
            raise nest_error(404, "Category not found")
        if category.scope != "EXPENSE":
            raise nest_error(400, "Category must be an EXPENSE category")
    lang = to_lang((await ensure_os_profile(user)).language)
    title = expense_title(lang, row.name)
    tx = await db.transaction.create(
        data={
            "userId": user.id, "type": "EXPENSE", "title": title.encode("utf-16-le")[:400].decode("utf-16-le", errors="ignore"),
            "amount": as_decimal(totals["actualTotal"]), "currency": totals["totalsCurrency"],
            "date": dates.parse_instant(body.date) if body.date else datetime.now(timezone.utc),
            "accountId": body.accountId, "categoryId": body.categoryId,
            "notes": expense_notes(lang, totals["purchasedCount"], row.name),
        }
    )
    await log_activity(
        user.id, "transaction.created", entity_id=tx.id, type="EXPENSE", amount=totals["actualTotal"], currency=tx.currency,
        title=tx.title, source="shopping",
    )
    await log_activity(user.id, "shoppingList.expenseLogged", entity_id=row.id, transactionId=tx.id, amount=totals["actualTotal"])
    transaction = {f: getattr(tx, f) for f in ("id", "type", "title", "amount", "currency", "date", "accountId", "categoryId")}
    return {"transaction": transaction, "list": totals}


# ----- items ---------------------------------------------------------------------------------


@router.get("/items")
async def list_items(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, ("listId", "purchased", "store", "q", "limit"))
    purchased = _query_bool(request, "purchased", errors)
    for field, top in (("store", 80), ("q", 200)):
        if q.get(field) is not None and len(q.get(field)) > top:
            errors.append(f"{field} must be shorter than or equal to {top} characters")
    limit = _query_int(request, "limit", errors, 2000)
    if errors:
        raise nest_error(400, errors)
    where: dict = {"list": {"is": list_access(user.id, await household_id_of(user.id))}}
    if q.get("listId"):
        where["listId"] = q.get("listId")
    if purchased is not None:
        where["isPurchased"] = purchased
    if q.get("store"):
        where["store"] = {"equals": q.get("store"), "mode": "insensitive"}
    if q.get("q"):
        contains = {"contains": q.get("q"), "mode": "insensitive"}
        where["OR"] = [{"name": contains}, {"notes": contains}, {"store": contains}]
    rows = await db.shoppingitem.find_many(
        where=where, include={"sourceRecipe": True}, order=[{"isPurchased": "asc"}, {"sortOrder": "asc"}, {"createdAt": "asc"}], take=limit or 1000
    )
    return {"items": [_item_view(r) for r in rows], "total": await db.shoppingitem.count(where=where)}


@router.post("/items/reorder")
async def reorder_items(body: ReorderBody, user=Depends(plus_user)):
    access = {"list": {"is": list_access(user.id, await household_id_of(user.id))}}
    ids = list(dict.fromkeys(i.id for i in body.items))
    if await db.shoppingitem.count(where={"id": {"in": ids}, **access}) != len(ids):
        raise nest_error(404, "Shopping item not found")
    async with db.batch_() as batch:
        for item in body.items:
            batch.shoppingitem.update_many(where={"id": item.id, **access}, data={"sortOrder": item.sortOrder})
    return {"ok": True}


@router.post("/items", status_code=201)
async def create_item(body: ItemCreate, user=Depends(plus_user)):
    target = await resolve_shopping_list(user.id, body.listId, await household_id_of(user.id))
    if target["created"]:
        await log_activity(user.id, "shoppingList.created", entity_id=target["id"], name=target["name"])
    sort_order = body.sortOrder if body.sortOrder is not None else await next_item_sort_order(target["id"])
    purchased = bool(body.isPurchased)
    row = await db.shoppingitem.create(
        data={
            "userId": user.id, "listId": target["id"], "name": body.name,
            "quantity": as_decimal(body.quantity if body.quantity is not None else 1), "unit": body.unit or None,
            "estimatedPrice": None if body.estimatedPrice is None else as_decimal(body.estimatedPrice),
            "actualPrice": None if body.actualPrice is None else as_decimal(body.actualPrice),
            "currency": body.currency, "store": body.store or None, "priority": body.priority or "MEDIUM",
            "isPurchased": purchased, "purchasedAt": datetime.now(timezone.utc) if purchased else None,
            "notes": body.notes, "sortOrder": sort_order,
        },
        include={"sourceRecipe": True},
    )
    await log_activity(user.id, "shoppingItem.created", entity_id=row.id, name=row.name, listId=target["id"])
    return _item_view(row)


@router.get("/items/{id}")
async def get_item(id: str, user=Depends(plus_user)):
    return _item_view(await _item(user.id, id))


@router.patch("/items/{id}")
async def update_item(id: str, body: ItemUpdate, user=Depends(plus_user)):
    existing = await _item(user.id, id)
    sent = body.model_fields_set
    data: dict = {}
    sort_order = body.sortOrder
    if body.listId and body.listId != existing.listId:
        await _accessible_list(user.id, body.listId)  # the target must be yours or shared with you
        data["listId"] = body.listId
        if sort_order is None:
            sort_order = await next_item_sort_order(body.listId)
    if "name" in sent and body.name is None:
        raise invalid_data()
    if "isPurchased" in sent and body.isPurchased != existing.isPurchased:  # null clears purchasedAt, as in NestJS
        data["purchasedAt"] = datetime.now(timezone.utc) if body.isPurchased else None
    if "name" in sent:
        data["name"] = body.name
    if "quantity" in sent:
        data["quantity"] = as_decimal(body.quantity if body.quantity is not None else 1)
    if "unit" in sent:
        data["unit"] = None if body.unit == "" else body.unit
    if "store" in sent:
        data["store"] = None if body.store == "" else body.store
    for field in ("estimatedPrice", "actualPrice"):
        if field in sent:
            data[field] = None if getattr(body, field) is None else as_decimal(getattr(body, field))
    for field in ("currency", "notes"):
        if field in sent:
            data[field] = getattr(body, field)
    if body.priority is not None:
        data["priority"] = body.priority
    if body.isPurchased is not None:
        data["isPurchased"] = body.isPurchased
    if sort_order is not None:
        data["sortOrder"] = sort_order
    row = await db.shoppingitem.update(where={"id": existing.id}, data=data or {"name": existing.name}, include={"sourceRecipe": True})
    await log_activity(user.id, "shoppingItem.updated", entity_id=id, name=row.name)
    return _item_view(row)


@router.post("/items/{id}/toggle")
async def toggle_item(id: str, user=Depends(plus_user)):
    existing = await _item(user.id, id)
    purchased = not existing.isPurchased
    row = await db.shoppingitem.update(
        where={"id": existing.id}, data={"isPurchased": purchased, "purchasedAt": datetime.now(timezone.utc) if purchased else None},
        include={"sourceRecipe": True},
    )
    await log_activity(user.id, "shoppingItem.purchased" if purchased else "shoppingItem.unpurchased", entity_id=id, name=row.name, listId=row.listId)
    return _item_view(row)


@router.delete("/items/{id}")
async def remove_item(id: str, user=Depends(plus_user)):
    existing = await _item(user.id, id)
    await db.shoppingitem.delete(where={"id": existing.id})
    await log_activity(user.id, "shoppingItem.deleted", entity_id=id, name=existing.name)
    return {"ok": True}
