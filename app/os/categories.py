"""/api/categories (port of vournalOS CategoriesService, its reference module).

Sub-categories are one level deep and share their parent's scope. Names are
unique per scope and parent, case-insensitively: the database index can't catch
it when the parent is NULL (NULLs are distinct), so it's checked here.
"""

from typing import Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.ownership import assert_many_owned
from app.os.common.query import string_list

router = nest_router("/categories")

SCOPES = (
    "TASK",
    "EVENT",
    "EXPENSE",
    "INCOME",
    "NOTE",
    "GOAL",
    "HABIT",
    "PROJECT",
    "MAINTENANCE",
    "DOCUMENT",
    "SHOPPING",
    "GENERAL",
)
Scope = Literal[SCOPES]  # type: ignore[valid-type]
_FIELDS = ("id", "scope", "name", "color", "icon", "parentId", "sortOrder", "isDefault", "createdAt", "updatedAt")


def view(row) -> dict:
    out = {name: getattr(row, name) for name in _FIELDS}
    out["isSubcategory"] = row.parentId is not None
    return out


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    color: StrictStr | None = None
    icon: StrictStr | None = None
    parentId: StrictStr | None = None
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 60)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        return validators.icon(v)


class CreateBody(_Fields):
    scope: Scope
    name: StrictStr

    @field_validator("name")
    @classmethod
    def _required_name(cls, v):
        return validators.trimmed(v, "name", 1, 60)


class UpdateBody(_Fields):
    """Scope is fixed once created."""


class ReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1)
    sortOrder: StrictInt = Field(ge=0)


class ReorderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReorderItem] = Field(min_length=1, max_length=500)


async def _owned(user_id: str, id: str):
    row = await db.category.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Category not found")
    return row


async def _assert_valid_parent(user_id: str, parent_id: str, scope: str) -> None:
    parent = await db.category.find_first(where={"id": parent_id, "userId": user_id})
    if parent is None:
        raise nest_error(404, "Category not found")
    if parent.scope != scope:
        raise nest_error(400, "Parent category must have the same scope")
    if parent.parentId:
        raise nest_error(400, "Sub-categories can only be one level deep")


async def _assert_unique_name(user_id: str, scope: str, name: str, parent_id, exclude_id=None) -> None:
    where = {
        "userId": user_id,
        "scope": scope,
        "parentId": parent_id,
        "name": {"equals": name, "mode": "insensitive"},
    }
    if exclude_id:
        where["id"] = {"not": exclude_id}
    if await db.category.find_first(where=where):
        raise nest_error(409, "A category with this name already exists")


@router.get("")
async def list_categories(scope: list[str] | None = Query(default=None), user=Depends(plus_user)):
    scopes = string_list(scope)
    if scopes and any(s not in SCOPES for s in scopes):
        raise nest_error(400, [f"each value in scope must be one of the following values: {', '.join(SCOPES)}"])
    where = {"userId": user.id, **({"scope": {"in": scopes}} if scopes else {})}
    rows = await db.category.find_many(
        where=where, order=[{"scope": "asc"}, {"sortOrder": "asc"}, {"name": "asc"}]
    )
    return {"items": [view(r) for r in rows], "total": len(rows)}


@router.post("/reorder")
async def reorder(body: ReorderBody, user=Depends(plus_user)):
    await assert_many_owned("category", [i.id for i in body.items], user.id)
    async with db.tx() as tx:
        for item in body.items:
            await tx.category.update_many(
                where={"id": item.id, "userId": user.id}, data={"sortOrder": item.sortOrder}
            )
    return {"ok": True}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    parent_id = body.parentId
    if parent_id:
        await _assert_valid_parent(user.id, parent_id, body.scope)
    await _assert_unique_name(user.id, body.scope, body.name, parent_id)
    sort_order = body.sortOrder
    if sort_order is None:
        last = await db.category.find_first(
            where={"userId": user.id, "scope": body.scope, "parentId": parent_id},
            order={"sortOrder": "desc"},
        )
        sort_order = (last.sortOrder if last else -1) + 1
    row = await db.category.create(
        data={
            "userId": user.id,
            "scope": body.scope,
            "name": body.name,
            "color": body.color,
            "icon": body.icon,
            "parentId": parent_id,
            "sortOrder": sort_order,
        }
    )
    await log_activity(user.id, "category.created", entity_id=row.id, name=row.name, scope=row.scope)
    return view(row)


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return view(await _owned(user.id, id))


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    # null means "clear", which these columns can't be (NestJS failed in Prisma: 400).
    if any(field in sent and getattr(body, field) is None for field in ("name", "sortOrder")):
        raise invalid_data()
    parent_id = body.parentId if "parentId" in sent else existing.parentId
    if "parentId" in sent and body.parentId is not None:
        if body.parentId == id:
            raise nest_error(400, "A category cannot be its own parent")
        await _assert_valid_parent(user.id, body.parentId, existing.scope)
        if await db.category.count(where={"userId": user.id, "parentId": id}) > 0:
            raise nest_error(400, "Sub-categories can only be one level deep")
    name = body.name if "name" in sent else existing.name
    if name != existing.name or parent_id != existing.parentId:
        await _assert_unique_name(user.id, existing.scope, name, parent_id, id)
    data = {field: getattr(body, field) for field in ("name", "color", "icon", "parentId", "sortOrder") if field in sent}
    row = await db.category.update(where={"id": existing.id}, data=data) if data else existing
    await log_activity(user.id, "category.updated", entity_id=id)
    return view(row)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.category.delete(where={"id": existing.id})
    await log_activity(user.id, "category.deleted", entity_id=id, name=existing.name)
    return {"ok": True}
