"""/api/notifications (port of vournalOS NotificationsService's API part). Making them is
notification_sync.py: a list (unless ?noSync=true) and the unread count run a lazy sync
first, as NestJS did, since the jobs may have slept; POST /sync runs a full one now."""

from datetime import datetime, timezone

from fastapi import Depends, Request

from app.db import db
from app.os.common.query import _query_bool, _query_int, unknown_params
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.notification_sync import lazy_sync, sync_for_user
from app.rate_limit import rate_limited

router = nest_router("/notifications")

DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 30, 200
_FIELDS = ("id", "title", "body", "link", "entityType", "entityId", "readAt", "pushedAt", "createdAt")


def _view(row) -> dict:
    return {f: getattr(row, f) for f in _FIELDS}


async def _owned(user_id: str, id: str):
    row = await db.notification.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Notification not found")
    return row


_LIST_NOTIFICATIONS_QUERY = ("unread", "noSync", "page", "pageSize")  # the NestJS query DTO's fields


@router.get("")
async def list_notifications(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, _LIST_NOTIFICATIONS_QUERY)
    unread = _query_bool(request, "unread", errors)
    no_sync = _query_bool(request, "noSync", errors)
    page = _query_int(request, "page", errors)
    page_size = _query_int(request, "pageSize", errors, MAX_PAGE_SIZE)
    if errors:
        raise nest_error(400, errors)
    if not no_sync:
        await lazy_sync(user)
    size = page_size or DEFAULT_PAGE_SIZE
    where = {"userId": user.id, **({"readAt": None} if unread else {})}
    rows = await db.notification.find_many(
        where=where,
        order=[{"readAt": {"sort": "desc", "nulls": "first"}}, {"createdAt": "desc"}],
        skip=((page or 1) - 1) * size,
        take=size,
    )
    return {
        "items": [_view(r) for r in rows],
        "total": await db.notification.count(where=where),
        "unreadCount": await db.notification.count(where={"userId": user.id, "readAt": None}),
    }


@router.get("/unread-count")
async def unread_count(user=Depends(plus_user)):
    await lazy_sync(user)
    return {"count": await db.notification.count(where={"userId": user.id, "readAt": None})}


@router.post("/sync")
async def sync(user=Depends(plus_user)):
    """A full pass now (10 a minute)."""
    if rate_limited("notifications-sync", user.id, 10, 60):
        raise nest_error(429, "ThrottlerException: Too Many Requests")
    return {"ok": True, "created": await sync_for_user(user, full=True)}


@router.post("/read-all")
async def read_all(user=Depends(plus_user)):
    count = await db.notification.update_many(
        where={"userId": user.id, "readAt": None}, data={"readAt": datetime.now(timezone.utc)}
    )
    return {"ok": True, "count": count}


@router.delete("")
async def clear_read(user=Depends(plus_user)):
    """Deletes every READ notification; unread ones stay."""
    count = await db.notification.delete_many(where={"userId": user.id, "readAt": {"not": None}})
    return {"ok": True, "count": count}


@router.post("/{id}/read")
async def mark_read(id: str, user=Depends(plus_user)):
    row = await _owned(user.id, id)
    updated = await db.notification.update(
        where={"id": row.id}, data={"readAt": row.readAt or datetime.now(timezone.utc)}
    )
    return _view(updated)


@router.post("/{id}/unread")
async def mark_unread(id: str, user=Depends(plus_user)):
    row = await _owned(user.id, id)
    return _view(await db.notification.update(where={"id": row.id}, data={"readAt": None}))


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    if await db.notification.delete_many(where={"id": id, "userId": user.id}) == 0:
        raise nest_error(404, "Notification not found")
    return {"ok": True}
