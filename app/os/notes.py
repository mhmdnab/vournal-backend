"""/api/notes (port of vournalOS NotesService). Markdown notes with tags and links to a
project, trip, course or person; lists carry a plain-text excerpt instead of the body."""

import re
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from app.db import db
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.space import Space, assert_in_space, assert_personal_links_allowed, current_space
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.jsparse import _JS_SPACE
from app.os.common.ownership import assert_many_owned, assert_owned
from app.os.common.query import _query_bool, _query_int, unknown_params

router = nest_router("/notes")

DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 50, 200
EXCERPT_LEN = 200
_FIELDS = (
    "id", "title", "content", "color", "isPinned", "isFavorite", "isArchived", "categoryId", "projectId", "tripId",
    "courseId", "personId", "createdAt", "updatedAt",
)
_INCLUDE = {"category": True, "tags": {"order_by": {"name": "asc"}}, "project": True, "trip": True, "course": True, "person": True}

# JavaScript's regex classes: \s (WhiteSpace + LineTerminator), and ^ under the m flag
_S = "[" + re.escape(_JS_SPACE) + "]"
_LINE_START = r"(?:^|(?<=[\r  ]))"
_EXCERPT_STEPS = (
    (re.compile(r"```[\s\S]*?```"), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(_LINE_START + _S + r"{0,3}(#{1,6}|>|[-*+]|[0-9]+\.)" + _S + "+", re.M), ""),
    (re.compile(r"\[( |x|X)\]" + _S + "*"), ""),
    (re.compile(r"(\*\*|__|~~|\*|_)"), ""),
    (re.compile(_S + "+"), " "),
)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def make_excerpt(md: str) -> str:
    """Plain-text excerpt: common markdown stripped, whitespace collapsed, cut at about 200."""
    text = md
    for pattern, replacement in _EXCERPT_STEPS:
        text = pattern.sub(replacement, text)
    text = text.strip(_JS_SPACE)
    if _utf16_len(text) <= EXCERPT_LEN:
        return text
    head = text.encode("utf-16-le")[: EXCERPT_LEN * 2].decode("utf-16-le", errors="ignore")
    return head.rstrip(_JS_SPACE) + "…"


def word_count(md: str) -> int:
    body = md.strip(_JS_SPACE)
    return len(re.split(_S + "+", body)) if body else 0


def _pick(row, *fields):
    return None if row is None else {f: getattr(row, f) for f in fields}


def view(row) -> dict:
    out = {f: getattr(row, f) for f in _FIELDS}
    out.update(
        category=_pick(row.category, "id", "name", "color", "icon"),
        tags=[_pick(t, "id", "name", "color") for t in row.tags or []],
        project=_pick(row.project, "id", "name"),
        trip=_pick(row.trip, "id", "name"),
        course=_pick(row.course, "id", "name"),
        person=_pick(row.person, "id", "name"),
    )
    return out


def list_item(row) -> dict:
    out = view(row)
    content = out.pop("content")
    return {**out, "excerpt": make_excerpt(content), "wordCount": word_count(content)}


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr | None = None
    content: StrictStr | None = Field(default=None, max_length=100_000)
    color: StrictStr | None = None
    isPinned: StrictBool | None = None
    isFavorite: StrictBool | None = None
    isArchived: StrictBool | None = None
    categoryId: StrictStr | None = None
    tagIds: list[StrictStr] | None = Field(default=None, max_length=50)
    projectId: StrictStr | None = None
    tripId: StrictStr | None = None
    courseId: StrictStr | None = None
    personId: StrictStr | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)


class CreateBody(_Fields):
    title: StrictStr


class UpdateBody(_Fields):
    pass


def _unique(ids):
    return list(dict.fromkeys(ids))


async def _assert_links(space: Space, body: _Fields) -> None:
    """Categories, tags, trips, courses and people are personal; a project is in the same space."""
    assert_personal_links_allowed(space, body)
    user_id = space.user.id
    if body.categoryId:
        category = await db.category.find_first(where={"id": body.categoryId, "userId": user_id})
        if category is None:
            raise nest_error(404, "Category not found")
        if category.scope != "NOTE":
            raise nest_error(400, "Category must be a NOTE category")
    await assert_many_owned("tag", _unique(body.tagIds) if body.tagIds else None, user_id)
    await assert_in_space("project", body.projectId, space)
    for model, value in (("trip", body.tripId), ("course", body.courseId), ("person", body.personId)):
        await assert_owned(model, value, user_id)


async def _owned(space: Space, id: str):
    row = await db.note.find_first(where={"id": id, **space.where}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Note not found")
    return row


_LIST_QUERY = (  # the NestJS query DTO's fields (its own first, then the inherited pagination)
    "q", "categoryId", "tagId", "favorite", "archived", "pinned", "projectId", "tripId", "courseId", "personId", "sort",
    "page", "pageSize",
)
SORTS = ("updatedAt", "createdAt", "title")


@router.get("")
async def list_notes(request: Request, space: Space = Depends(current_space)):
    user = space.user
    q = request.query_params
    errors = unknown_params(request, _LIST_QUERY)
    text = q.get("q")
    if text is not None and len(text) > 200:
        errors.append("q must be shorter than or equal to 200 characters")
    favorite = _query_bool(request, "favorite", errors)
    archived = _query_bool(request, "archived", errors)
    pinned = _query_bool(request, "pinned", errors)
    sort = q.get("sort")
    if sort is not None and sort not in SORTS:
        errors.append(f"sort must be one of the following values: {', '.join(SORTS)}")
    page = _query_int(request, "page", errors)
    page_size = _query_int(request, "pageSize", errors, MAX_PAGE_SIZE)
    if errors:
        raise nest_error(400, errors)
    where: dict = {**space.where, "isArchived": bool(archived)}
    if favorite is not None:
        where["isFavorite"] = favorite
    if pinned is not None:
        where["isPinned"] = pinned
    for field in ("categoryId", "projectId", "tripId", "courseId", "personId"):
        if q.get(field):
            where[field] = q.get(field)
    if q.get("tagId"):
        where["tags"] = {"some": {"id": q.get("tagId")}}
    term = (text or "").strip(_JS_SPACE)
    if term:
        where["OR"] = [{"title": {"contains": term, "mode": "insensitive"}}, {"content": {"contains": term, "mode": "insensitive"}}]
    sort = sort or "updatedAt"
    order = [{"isPinned": "desc"}, {"title": "asc"} if sort == "title" else {sort: "desc"}]
    page, size = page or 1, page_size or DEFAULT_PAGE_SIZE
    rows = await db.note.find_many(where=where, include=_INCLUDE, order=order, skip=(page - 1) * size, take=size)
    total = await db.note.count(where=where)
    return {"items": [list_item(r) for r in rows], "total": total}


@router.post("", status_code=201)
async def create(body: CreateBody, space: Space = Depends(current_space)):
    user = space.user
    await _assert_links(space, body)
    row = await db.note.create(
        data={
            **space.create_fields(),
            "title": body.title,
            "content": body.content or "",
            "color": body.color,
            "isPinned": bool(body.isPinned),
            "isFavorite": bool(body.isFavorite),
            "isArchived": bool(body.isArchived),
            "categoryId": body.categoryId,
            "projectId": body.projectId,
            "tripId": body.tripId,
            "courseId": body.courseId,
            "personId": body.personId,
            **({"tags": {"connect": [{"id": i} for i in _unique(body.tagIds)]}} if body.tagIds else {}),
        },
        include=_INCLUDE,
    )
    await log_activity(user.id, "note.created", entity_id=row.id, title=row.title)
    return view(row)


@router.get("/{id}")
async def get(id: str, space: Space = Depends(current_space)):
    user = space.user
    return view(await _owned(space, id))


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, space: Space = Depends(current_space)):
    user = space.user
    existing = await _owned(space, id)
    await _assert_links(space, body)
    sent = body.model_fields_set
    if "title" in sent and body.title is None:
        raise invalid_data()  # Prisma refused null on a required column
    data: dict = {}
    for field in ("title", "color", "categoryId", "projectId", "tripId", "courseId", "personId"):
        if field in sent:
            data[field] = getattr(body, field)
    if "content" in sent:
        data["content"] = body.content if body.content is not None else ""
    for flag in ("isPinned", "isFavorite", "isArchived"):
        if getattr(body, flag) is not None:  # null leaves it as it is
            data[flag] = getattr(body, flag)
    if body.tagIds is not None:
        data["tags"] = {"set": [{"id": i} for i in _unique(body.tagIds)]}
    # NestJS always ran the update (and so moved updatedAt), even with nothing to change
    row = await db.note.update(where={"id": existing.id}, data=data or {"title": existing.title}, include=_INCLUDE)
    verb = "updated"
    for flag, on, off in (("isArchived", "archived", "unarchived"), ("isPinned", "pinned", "unpinned"), ("isFavorite", "favorited", "unfavorited")):
        value = getattr(body, flag)
        if flag in sent and value != getattr(existing, flag):  # null counts as "off" here, as in NestJS
            verb = on if value else off
            break
    await log_activity(user.id, f"note.{verb}", entity_id=id, title=row.title)
    return view(row)


@router.delete("/{id}")
async def remove(id: str, space: Space = Depends(current_space)):
    user = space.user
    existing = await _owned(space, id)
    if not space.can_delete(existing):
        raise nest_error(404, "Note not found")
    await db.note.delete(where={"id": existing.id})
    await log_activity(user.id, "note.deleted", entity_id=id, title=existing.title)
    return {"ok": True}
