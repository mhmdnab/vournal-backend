"""/api/tags (port of vournalOS TagsService). Tags label tasks and notes."""

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from app.db import db
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router

router = nest_router("/tags")


async def view(row) -> dict:
    tagged = {"tags": {"some": {"id": row.id}}}
    return {
        "id": row.id,
        "name": row.name,
        "color": row.color,
        "createdAt": row.createdAt,
        "taskCount": await db.task.count(where=tagged),
        "noteCount": await db.note.count(where=tagged),
    }


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    color: StrictStr | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 40)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)


class CreateBody(_Fields):
    name: StrictStr

    @field_validator("name")
    @classmethod
    def _required_name(cls, v):
        return validators.trimmed(v, "name", 1, 40)


class UpdateBody(_Fields):
    pass


async def _owned(user_id: str, id: str):
    row = await db.tag.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Tag not found")
    return row


async def _assert_unique_name(user_id: str, name: str, exclude_id: str | None = None) -> None:
    where = {"userId": user_id, "name": {"equals": name, "mode": "insensitive"}}
    if exclude_id:
        where["id"] = {"not": exclude_id}
    if await db.tag.find_first(where=where):
        raise nest_error(409, "A tag with this name already exists")


@router.get("")
async def list_tags(user=Depends(plus_user)):
    rows = await db.tag.find_many(where={"userId": user.id}, order={"name": "asc"})
    return {"items": [await view(r) for r in rows], "total": len(rows)}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    await _assert_unique_name(user.id, body.name)
    row = await db.tag.create(data={"userId": user.id, "name": body.name, "color": body.color})
    await log_activity(user.id, "tag.created", entity_id=row.id, name=row.name)
    return await view(row)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    if "name" in sent and body.name is None:
        raise nest_error(400, "Invalid data")  # NestJS: Prisma rejected the null
    if "name" in sent and body.name != existing.name:
        await _assert_unique_name(user.id, body.name, id)
    data = {field: getattr(body, field) for field in ("name", "color") if field in sent}
    row = await db.tag.update(where={"id": existing.id}, data=data) if data else existing
    return await view(row)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.tag.delete(where={"id": existing.id})
    await log_activity(user.id, "tag.deleted", entity_id=id)
    return {"ok": True}
