"""/api/recipes (port of vournalOS RecipesService). Ingredients are replaced as a set on
update; lists show total minutes and how often the recipe is on the meal plan."""

import re

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.query import _query_bool, unknown_params
from app.os.recurring_payments import _URL
from app.os.transactions import as_decimal

router = nest_router("/recipes")

_FIELDS = ("id", "name", "description", "instructions", "servings", "prepMinutes", "cookMinutes", "sourceUrl", "isFavorite", "createdAt", "updatedAt")
_INCLUDE = {"ingredients": {"order_by": [{"sortOrder": "asc"}, {"id": "asc"}]}}
_PROTOCOL = re.compile(r"^https?://", re.IGNORECASE)


async def _meal_counts(ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    groups = await db.mealplanentry.group_by(by=["recipeId"], where={"recipeId": {"in": ids}}, count={"_all": True})
    return {g["recipeId"]: g["_count"]["_all"] for g in groups}


def view(row, meal_count: int) -> dict:
    out = {f: getattr(row, f) for f in _FIELDS}
    ingredients = row.ingredients or []
    out["ingredients"] = [{f: getattr(i, f) for f in ("id", "name", "quantity", "unit", "sortOrder")} for i in ingredients]
    no_time = row.prepMinutes is None and row.cookMinutes is None
    out.update(
        totalMinutes=None if no_time else (row.prepMinutes or 0) + (row.cookMinutes or 0),
        mealCount=meal_count,
        ingredientCount=len(ingredients),
    )
    return out


class Ingredient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    quantity: object = None
    unit: StrictStr | None = None
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("quantity")
    @classmethod
    def _quantity(cls, v):
        if v is not None and validators.money(v, "quantity", 0) > 99_999_999:
            raise ValueError("quantity must not be greater than 99999999")
        return v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        return validators.trimmed(v, "unit", 0, 30)


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    description: StrictStr | None = Field(default=None, max_length=2000)
    instructions: StrictStr | None = Field(default=None, max_length=20000)
    servings: StrictInt | None = Field(default=None, ge=1, le=100)
    prepMinutes: StrictInt | None = Field(default=None, ge=0, le=10000)
    cookMinutes: StrictInt | None = Field(default=None, ge=0, le=10000)
    sourceUrl: StrictStr | None = None
    isFavorite: StrictBool | None = None
    ingredients: list[Ingredient] | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 120)

    @field_validator("sourceUrl")
    @classmethod
    def _url(cls, v):
        if v is None:
            return v
        problems = []
        if len(v) > 2000:
            problems.append("sourceUrl must be shorter than or equal to 2000 characters")
        if not (_PROTOCOL.match(v) and _URL.match(v)):  # IsUrl({ require_protocol: true, protocols: http, https })
            problems.append("sourceUrl must be a URL address")
        if problems:
            raise ValueError("\n".join(problems))
        return v


class CreateBody(_Fields):
    name: StrictStr


class UpdateBody(_Fields):
    pass


def _ingredient_rows(items: list[Ingredient]) -> list[dict]:
    return [
        {"name": i.name, "quantity": None if i.quantity is None else as_decimal(i.quantity), "unit": i.unit or None,
         "sortOrder": i.sortOrder if i.sortOrder is not None else n}
        for n, i in enumerate(items)
    ]


async def _owned(user_id: str, id: str):
    row = await db.recipe.find_first(where={"id": id, "userId": user_id}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Recipe not found")
    return row


async def _view(row) -> dict:
    return view(row, (await _meal_counts([row.id])).get(row.id, 0))


@router.get("")
async def list_recipes(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, ("q", "favorite"))
    if q.get("q") is not None and len(q.get("q")) > 200:
        errors.append("q must be shorter than or equal to 200 characters")
    favorite = _query_bool(request, "favorite", errors)
    if errors:
        raise nest_error(400, errors)
    where: dict = {"userId": user.id}
    if favorite is not None:
        where["isFavorite"] = favorite
    if q.get("q"):
        contains = {"contains": q.get("q"), "mode": "insensitive"}
        where["OR"] = [{"name": contains}, {"description": contains}, {"ingredients": {"some": {"name": contains}}}]
    rows = await db.recipe.find_many(where=where, include=_INCLUDE, order=[{"isFavorite": "desc"}, {"name": "asc"}])
    counts = await _meal_counts([r.id for r in rows])
    return {"items": [view(r, counts.get(r.id, 0)) for r in rows], "total": len(rows)}


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await _view(await _owned(user.id, id))


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    data = {
        "userId": user.id, "name": body.name, "description": body.description, "instructions": body.instructions,
        "servings": body.servings, "prepMinutes": body.prepMinutes, "cookMinutes": body.cookMinutes,
        "sourceUrl": body.sourceUrl or None, "isFavorite": bool(body.isFavorite),
    }
    if body.ingredients:
        data["ingredients"] = {"create": _ingredient_rows(body.ingredients)}
    row = await db.recipe.create(data=data, include=_INCLUDE)
    await log_activity(user.id, "recipe.created", entity_id=row.id, name=row.name)
    return await _view(row)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    if "name" in sent and body.name is None:
        raise invalid_data()  # Prisma refused null on a required column
    data: dict = {f: getattr(body, f) for f in ("name", "description", "instructions", "servings", "prepMinutes", "cookMinutes") if f in sent}
    if "sourceUrl" in sent:
        data["sourceUrl"] = None if body.sourceUrl == "" else body.sourceUrl
    if body.isFavorite is not None:
        data["isFavorite"] = body.isFavorite
    async with db.tx() as tx:
        if body.ingredients is not None:  # replaces the whole set
            await tx.recipeingredient.delete_many(where={"recipeId": existing.id})
            if body.ingredients:
                await tx.recipeingredient.create_many(data=[{**r, "recipeId": existing.id} for r in _ingredient_rows(body.ingredients)])
        row = await tx.recipe.update(where={"id": existing.id}, data=data or {"name": existing.name}, include=_INCLUDE)
    await log_activity(user.id, "recipe.updated", entity_id=id, name=row.name)
    return await _view(row)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.recipe.delete(where={"id": existing.id})
    await log_activity(user.id, "recipe.deleted", entity_id=id, name=existing.name)
    return {"ok": True}
