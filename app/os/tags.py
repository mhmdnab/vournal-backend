"""/api/tags (port of vournalOS TagsService). Tags label tasks and notes."""

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from app.db import db
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router

router = nest_router("/tags")


async def _counts(ids: list[str]) -> dict[str, tuple[int, int]]:
    """Tasks and notes per tag in two grouped queries, whatever the number of tags
    (Prisma's implicit join tables: _TagToTask.A and _NoteToTag.B hold the tag)."""
    if not ids:
        return {}
    tasks = await db.query_raw('SELECT "A" AS id, COUNT(*)::int AS n FROM "_TagToTask" WHERE "A" = ANY($1) GROUP BY "A"', ids)
    notes = await db.query_raw('SELECT "B" AS id, COUNT(*)::int AS n FROM "_NoteToTag" WHERE "B" = ANY($1) GROUP BY "B"', ids)
    by_tasks, by_notes = {r["id"]: r["n"] for r in tasks}, {r["id"]: r["n"] for r in notes}
    return {i: (by_tasks.get(i, 0), by_notes.get(i, 0)) for i in ids}


async def view(row, counts: dict | None = None) -> dict:
    task_count, note_count = (counts if counts is not None else await _counts([row.id]))[row.id]
    return {
        "id": row.id,
        "name": row.name,
        "color": row.color,
        "createdAt": row.createdAt,
        "taskCount": task_count,
        "noteCount": note_count,
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
    counts = await _counts([r.id for r in rows])
    return {"items": [await view(r, counts) for r in rows], "total": len(rows)}


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
        raise invalid_data()  # NestJS: Prisma rejected the null
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
