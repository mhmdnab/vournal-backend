"""/api/meal-plan (port of vournalOS MealPlanService). Entries per day and meal; a week
can be copied, and the plan's recipes become a shopping list (scaled by servings,
merged by name and unit, topping up what's already on the list)."""

import math
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.jsparse import _JS_SPACE
from app.os.common.http import nest_error, nest_router
from app.os.common.query import unknown_params
from app.os.profile import ensure_os_profile
from app.os.shopping import ingredient_key, next_item_sort_order, resolve_shopping_list
from app.os.transactions import as_decimal

router = nest_router("/meal-plan")

MEAL_TYPES = ("BREAKFAST", "LUNCH", "DINNER", "SNACK")
MealTypeName = Literal[MEAL_TYPES]  # type: ignore[valid-type]
MAX_RANGE_DAYS = 366
_EPSILON = 2.220446049250313e-16
_FIELDS = ("id", "date", "mealType", "title", "recipeId", "servings", "notes", "createdAt", "updatedAt")


def _round2(n: float) -> float:
    """shopping.helpers round2: Math.round((n + Number.EPSILON) * 100) / 100."""
    return math.floor((n + _EPSILON) * 100 + 0.5) / 100


def _clamp_qty(n: float) -> float:
    return min(99_999_999, max(0.01, n))


def view(row) -> dict:
    out = {f: getattr(row, f) for f in _FIELDS}
    r = row.recipe
    out["recipe"] = {"id": r.id, "name": r.name, "servings": r.servings, "isFavorite": r.isFavorite} if r else None
    return out


def _parse(value: str):
    day = dates.parse_date_only(value)
    if day is None:
        raise nest_error(400, "Invalid date")
    return day


async def _range(user, start: str | None, end: str | None) -> tuple:
    if start:
        lo = _parse(start)
    else:
        settings = await ensure_os_profile(user)
        today = dates.today_date_only(settings.timezone)
        back = ((today.isoweekday() % 7) - settings.weekStartsOn + 7) % 7  # getUTCDay: 0 = Sunday
        lo = dates.add_days_date_only(today, -back)
    hi = _parse(end) if end else dates.add_days_date_only(lo, 6)
    if hi < lo:
        raise nest_error(400, '"to" must be on or after "from"')
    if dates.diff_days(lo, hi) > MAX_RANGE_DAYS:
        raise nest_error(400, "Date range too large (max 1 year)")
    return lo, hi


def _iso_problem(value, field: str) -> list[str]:
    return [] if dates.is_iso_date_string(value) else [f"{field} must be a valid ISO 8601 date string"]


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: StrictStr | None = None
    mealType: MealTypeName | None = None
    title: StrictStr | None = None
    recipeId: StrictStr | None = None
    servings: StrictInt | None = Field(default=None, ge=1, le=100)
    notes: StrictStr | None = Field(default=None, max_length=2000)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v is not None and (problems := _iso_problem(v, "date")):
            raise ValueError(problems[0])
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)


class CreateBody(_Fields):
    date: StrictStr


class UpdateBody(_Fields):
    pass


class CopyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fromWeekStart: StrictStr
    toWeekStart: StrictStr
    replace: StrictBool | None = None

    @field_validator("fromWeekStart", "toWeekStart")
    @classmethod
    def _dates(cls, v, info):
        if problems := _iso_problem(v, info.field_name):
            raise ValueError(problems[0])
        return v


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: StrictStr = Field(alias="from")
    to: StrictStr
    listId: StrictStr | None = None

    @field_validator("from_", "to")
    @classmethod
    def _dates(cls, v, info):
        field = "from" if info.field_name == "from_" else info.field_name
        if problems := _iso_problem(v, field):
            raise ValueError(problems[0])
        return v


async def _owned(user_id: str, id: str):
    row = await db.mealplanentry.find_first(where={"id": id, "userId": user_id}, include={"recipe": True})
    if row is None:
        raise nest_error(404, "Meal plan entry not found")
    return row


async def _recipe_name(user_id: str, recipe_id: str) -> str:
    recipe = await db.recipe.find_first(where={"id": recipe_id, "userId": user_id})
    if recipe is None:
        raise nest_error(404, "Recipe not found")
    return recipe.name


@router.get("")
async def list_entries(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, ("from", "to"))
    for field in ("from", "to"):
        if q.get(field) is not None:
            errors.extend(_iso_problem(q.get(field), field))
    if errors:
        raise nest_error(400, errors)
    lo, hi = await _range(user, q.get("from"), q.get("to"))
    rows = await db.mealplanentry.find_many(
        where={"userId": user.id, "date": {"gte": lo, "lte": hi}}, include={"recipe": True},
        order=[{"date": "asc"}, {"mealType": "asc"}, {"createdAt": "asc"}],
    )
    return {"items": [view(r) for r in rows], "total": len(rows), "from": dates.format_date_only(lo), "to": dates.format_date_only(hi)}


@router.post("/copy-week")
async def copy_week(body: CopyBody, user=Depends(plus_user)):
    """Copies the 7 days from fromWeekStart onto the same weekdays from toWeekStart."""
    src, dst = _parse(body.fromWeekStart), _parse(body.toWeekStart)
    offset = dates.diff_days(src, dst)
    if offset == 0:
        raise nest_error(400, "Source and target weeks are the same")
    src_end, dst_end = dates.add_days_date_only(src, 6), dates.add_days_date_only(dst, 6)
    source = await db.mealplanentry.find_many(
        where={"userId": user.id, "date": {"gte": src, "lte": src_end}}, order=[{"date": "asc"}, {"mealType": "asc"}, {"createdAt": "asc"}]
    )
    target = {"userId": user.id, "date": {"gte": dst, "lte": dst_end}}
    async with db.tx() as tx:
        removed = await tx.mealplanentry.delete_many(where=target) if body.replace else 0
        existing = [] if body.replace else await tx.mealplanentry.find_many(where=target)
        seen = {f"{dates.format_date_only(e.date)}|{e.mealType}|{e.title.lower()}" for e in existing}
        data, skipped = [], 0
        for e in source:
            day = dates.add_days_date_only(dates._utc(e.date), offset)
            key = f"{dates.format_date_only(day)}|{e.mealType}|{e.title.lower()}"
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            data.append({"userId": user.id, "date": day, "mealType": e.mealType, "title": e.title, "recipeId": e.recipeId, "servings": e.servings, "notes": e.notes})
        if data:
            await tx.mealplanentry.create_many(data=data)
    await log_activity(
        user.id, "mealPlan.weekCopied", entity_id=None, fromWeekStart=body.fromWeekStart[:10], toWeekStart=body.toWeekStart[:10], copied=len(data)
    )
    return {"ok": True, "copied": len(data), "skipped": skipped, "removed": removed}


@router.post("/generate-shopping")
async def generate_shopping(body: GenerateBody, user=Depends(plus_user)):
    lo, hi = await _range(user, body.from_, body.to)
    target = await resolve_shopping_list(user.id, body.listId)
    if target["created"]:
        await log_activity(user.id, "shoppingList.created", entity_id=target["id"], name=target["name"])
    entries = await db.mealplanentry.find_many(
        where={"userId": user.id, "date": {"gte": lo, "lte": hi}, "recipeId": {"not": None}},
        include={"recipe": {"include": {"ingredients": {"order_by": {"sortOrder": "asc"}}}}},
        order=[{"date": "asc"}, {"mealType": "asc"}],
    )
    agg: dict[str, dict] = {}
    for e in entries:
        r = e.recipe
        if r is None:
            continue
        factor = e.servings / r.servings if e.servings and r.servings else 1
        for ing in r.ingredients or []:
            name = ing.name.strip(_JS_SPACE)
            if not name:
                continue
            unit = (ing.unit or "").strip(_JS_SPACE) or None
            key = ingredient_key(name, unit)
            qty = float(ing.quantity) * factor if ing.quantity is not None else 0
            if key in agg:
                agg[key]["quantity"] += qty
                agg[key]["hasQty"] = agg[key]["hasQty"] or ing.quantity is not None
            else:
                agg[key] = {"name": name, "unit": unit, "quantity": qty, "hasQty": ing.quantity is not None, "recipeId": r.id}
    if not agg:
        return {"added": 0, "merged": 0, "listId": target["id"], "listName": target["name"], "recipeCount": 0}
    existing = await db.shoppingitem.find_many(where={"userId": user.id, "listId": target["id"], "isPurchased": False}, order={"sortOrder": "asc"})
    by_key: dict = {}
    for item in existing:
        by_key.setdefault(ingredient_key(item.name, item.unit), item)
    sort_order = await next_item_sort_order(target["id"])
    creates, updates = [], []
    for key, a in agg.items():
        counted = a["hasQty"] and a["quantity"] > 0
        hit = by_key.get(key)
        if hit:
            updates.append((hit.id, _clamp_qty(_round2(float(hit.quantity) + (a["quantity"] if counted else 1)))))
        else:
            creates.append({
                "userId": user.id, "listId": target["id"], "name": a["name"], "unit": a["unit"],
                "quantity": as_decimal(_clamp_qty(_round2(a["quantity"])) if counted else 1),
                "sourceRecipeId": a["recipeId"], "sortOrder": sort_order,
            })
            sort_order += 1
    async with db.batch_() as batch:  # one atomic request, as NestJS's $transaction([...])
        for item_id, quantity in updates:
            batch.shoppingitem.update(where={"id": item_id}, data={"quantity": as_decimal(quantity)})
        if creates:
            batch.shoppingitem.create_many(data=creates)
    recipe_count = len({e.recipe.id for e in entries if e.recipe})
    await log_activity(
        user.id, "mealPlan.shoppingGenerated", entity_id=None, listId=target["id"], added=len(creates), merged=len(updates),
        **{"from": dates.format_date_only(lo)}, to=dates.format_date_only(hi),
    )
    return {"added": len(creates), "merged": len(updates), "listId": target["id"], "listName": target["name"], "recipeCount": recipe_count}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    day = _parse(body.date)
    title = body.title
    if body.recipeId:
        name = await _recipe_name(user.id, body.recipeId)  # the ownership check, title or not
        title = title or name
    if not title:
        raise nest_error(400, "title is required when no recipe is selected")
    row = await db.mealplanentry.create(
        data={"userId": user.id, "date": day, "mealType": body.mealType or "DINNER", "title": title, "recipeId": body.recipeId,
              "servings": body.servings, "notes": body.notes},
        include={"recipe": True},
    )
    await log_activity(user.id, "mealPlan.created", entity_id=row.id, title=row.title, mealType=row.mealType, date=body.date[:10])
    return view(row)


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return view(await _owned(user.id, id))


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    title = body.title
    if body.recipeId and body.recipeId != existing.recipeId:
        name = await _recipe_name(user.id, body.recipeId)
        if "title" not in sent:  # another recipe and no new title: follow the recipe's name
            title = name
    data: dict = {}
    if body.date:
        data["date"] = _parse(body.date)
    if body.mealType is not None:
        data["mealType"] = body.mealType
    if title:
        data["title"] = title
    for field in ("recipeId", "servings", "notes"):
        if field in sent:
            data[field] = getattr(body, field)
    row = await db.mealplanentry.update(where={"id": existing.id}, data=data or {"title": existing.title}, include={"recipe": True})
    await log_activity(user.id, "mealPlan.updated", entity_id=id, title=row.title)
    return view(row)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.mealplanentry.delete(where={"id": existing.id})
    await log_activity(user.id, "mealPlan.deleted", entity_id=id, title=existing.title)
    return {"ok": True}
