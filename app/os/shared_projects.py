"""One project shared view-only by email (spec 2026-11-09-workspaces-design, "Shared project").

- Sharing: the project's owner (Personal) or an admin of its workspace invites an email;
  the account with that confirmed address accepts the emailed link.
- Guests see the project, its tasks, notes, events and their files, read-only, from any
  space. They don't need Plus: the routes under /api/shared/projects (and accepting an
  invite) are the only /api routes that take a signed-in account without it.
- Anything not shared with the caller answers 404.
"""

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, Response

from app import email as mailer
from app import storage
from app.db import db
from app.os.common import validators
from app.os.common.body import RuleBody
from app.os.common.deps import web_user
from app.os.common.http import nest_error, nest_router
from app.os.common.space import Space, current_space
from app.os.household import hash_code, new_invite_code
from app.os.workspaces import _display_name, web_url

router = nest_router("")

NOT_FOUND = "Project not found"
_PROJECT = ("id", "name", "description", "status", "color", "icon", "startDate", "deadline", "workspaceId")
_TASK = ("id", "title", "status", "priority", "dueAt", "hasDueTime", "completedAt", "parentId", "description")
_NOTE = ("id", "title", "content", "color", "updatedAt")
_EVENT = ("id", "title", "startAt", "endAt", "allDay", "location", "description", "rrule")
_FILE = ("id", "fileName", "mimeType", "size", "entityType", "entityId", "createdAt")


def _pick(row, fields) -> dict:
    return {f: getattr(row, f) for f in fields}


class ShareBody(RuleBody):
    RULES = (("email", False, True, lambda v, f: validators.email_rules(v, f, 320)),)
    email: object = None


async def _shareable(space: Space, project_id: str):
    """A project the caller may share: theirs in Personal, or one in a workspace they admin."""
    project = await db.project.find_first(where={"id": project_id, **space.where})
    if project is None or not space.is_admin:
        raise nest_error(404, NOT_FOUND)
    return project


async def _shared_with(user, project_id: str):
    share = await db.projectshare.find_first(
        where={"projectId": project_id, "userId": user.id, "acceptedAt": {"not": None}}, include={"project": True}
    )
    if share is None:
        raise nest_error(404, NOT_FOUND)
    return share.project


def _share_view(row) -> dict:
    return {"id": row.id, "email": row.email, "accepted": row.acceptedAt is not None, "createdAt": row.createdAt}


# ----- the sharer ----------------------------------------------------------------------


@router.post("/projects/{id}/shares", status_code=201)
async def share(id: str, body: ShareBody, space: Space = Depends(current_space)):
    project = await _shareable(space, id)
    email = body.email.strip().lower()
    if email == space.user.email.lower():
        raise nest_error(400, "That's you")
    if await db.projectshare.find_unique(where={"projectId_email": {"projectId": id, "email": email}}):
        raise nest_error(409, "It's already shared with them")
    code = new_invite_code() + new_invite_code()
    row = await db.projectshare.create(
        data={"projectId": id, "email": email, "codeHash": hash_code(code), "invitedById": space.user.id}
    )
    await mailer.send_project_share(email, project.name, await _display_name(space.user), f"{web_url()}/invite/project/{code}")
    return _share_view(row)


@router.get("/projects/{id}/shares")
async def list_shares(id: str, space: Space = Depends(current_space)):
    await _shareable(space, id)
    rows = await db.projectshare.find_many(where={"projectId": id}, order={"createdAt": "asc"})
    return [_share_view(r) for r in rows]


@router.delete("/projects/{id}/shares/{share_id}")
async def unshare(id: str, share_id: str, space: Space = Depends(current_space)):
    await _shareable(space, id)
    await db.projectshare.delete_many(where={"id": share_id, "projectId": id})
    return {"ok": True}


# ----- the guest -----------------------------------------------------------------------


async def _live_share(code: str):
    row = await db.projectshare.find_unique(where={"codeHash": hash_code(code.strip())}, include={"project": True})
    if row is None or row.acceptedAt is not None:
        raise nest_error(404, "This invite is not valid or has been used")
    return row


@router.get("/invites/project/{code}")
async def preview(code: str, user=Depends(web_user)):
    row = await _live_share(code)
    inviter = await db.user.find_unique(where={"id": row.invitedById})
    return {
        "projectName": row.project.name,
        "invitedBy": await _display_name(inviter) if inviter else None,
        "email": row.email,
        "emailMatches": user.email.lower() == row.email,
    }


@router.post("/invites/project/{code}/accept")
async def accept(code: str, user=Depends(web_user)):
    row = await _live_share(code)
    if user.email.lower() != row.email:
        raise nest_error(403, "This invite is for another email address.")
    if user.emailVerifiedAt is None:
        raise nest_error(403, "Confirm your email first.")
    claimed = await db.projectshare.update_many(
        where={"id": row.id, "acceptedAt": None}, data={"userId": user.id, "acceptedAt": datetime.now(timezone.utc)}
    )
    if not claimed:
        raise nest_error(404, "This invite is not valid or has been used")
    return {"id": row.projectId, "name": row.project.name}


@router.get("/shared/projects")
async def shared_with_me(user=Depends(web_user)):
    rows = await db.projectshare.find_many(
        where={"userId": user.id, "acceptedAt": {"not": None}}, include={"project": {"include": {"workspace": True}}},
        order={"acceptedAt": "desc"},
    )
    out = []
    for r in rows:
        p = r.project
        owner = await db.user.find_unique(where={"id": p.userId})
        out.append({
            "id": p.id, "name": p.name, "color": p.color, "icon": p.icon,
            "ownerName": await _display_name(owner) if owner else None,
            "workspaceName": p.workspace.name if p.workspace else None,
        })
    return out


@router.get("/shared/projects/{id}")
async def shared_project(id: str, user=Depends(web_user)):
    project = await _shared_with(user, id)
    same = {"projectId": id, "workspaceId": project.workspaceId}
    tasks = await db.task.find_many(where=same, order=[{"status": "asc"}, {"position": "asc"}], take=500)
    notes = await db.note.find_many(where={**same, "isArchived": False}, order={"updatedAt": "desc"}, take=200)
    events = await db.event.find_many(where=same, order={"startAt": "asc"}, take=200)
    files = await db.attachment.find_many(
        where={"OR": [
            {"entityType": "PROJECT", "entityId": id},
            {"entityType": "TASK", "entityId": {"in": [t.id for t in tasks]}},
            {"entityType": "NOTE", "entityId": {"in": [n.id for n in notes]}},
            {"entityType": "EVENT", "entityId": {"in": [e.id for e in events]}},
        ]},
        order={"createdAt": "desc"},
    )
    return {
        "project": _pick(project, _PROJECT),
        "tasks": [_pick(t, _TASK) for t in tasks],
        "notes": [_pick(n, _NOTE) for n in notes],
        "events": [_pick(e, _EVENT) for e in events],
        "attachments": [_pick(f, _FILE) for f in files],
        "readOnly": True,
    }


@router.get("/shared/projects/{id}/attachments/{attachment_id}/download")
async def shared_download(id: str, attachment_id: str, user=Depends(web_user)):
    body = await shared_project(id, user)
    file = next((f for f in body["attachments"] if f["id"] == attachment_id), None)
    if file is None:
        raise nest_error(404, "Attachment not found")
    row = await db.attachment.find_unique(where={"id": attachment_id})
    data = await storage.get_object(row.storageKey)
    if data is None:
        raise nest_error(404, "Attachment not found")
    return Response(
        content=data,
        media_type=row.mimeType,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.fileName, safe=chr(39) + '!()*-._~')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
