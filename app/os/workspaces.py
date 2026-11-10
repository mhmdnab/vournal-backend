"""/api/workspaces and /api/invites/workspace (spec 2026-11-09-workspaces-design).

A workspace shares tasks, calendar, projects, notes and files among its members, next to
each person's Personal space. A person can be in several. Roles: one OWNER, any number of
ADMINs and MEMBERs. Admins invite (by email, accepted by the account with that confirmed
address, 14 days), remove members and edit the workspace; only the owner promotes admins,
transfers ownership and deletes it. Every member needs Plus, as with households.

Leaving or being removed keeps everything you added in the workspace; your tasks there
become unassigned. When the owner goes, the longest-standing admin takes over, else the
longest-standing member; with no one left the workspace and its items are deleted.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends

from app import email as mailer
from app.db import db
from app.os.common import validators
from app.os.common.body import RuleBody, choice, text
from app.os.common.deps import plus_user, web_user
from app.os.common.http import nest_error, nest_router
from app.os.common.space import ADMIN_ROLES, member_role
from app.os.household import hash_code, new_invite_code

router = nest_router("")

INVITE_DAYS = 14
ROLES = ("OWNER", "ADMIN", "MEMBER")
NOT_FOUND = "Workspace not found"
WRONG_EMAIL = "This invite is for another email address."


def web_url() -> str:
    return (os.environ.get("VOURNAL_WEB_URL") or "https://life.vournal.app").rstrip("/")


def _role(value) -> str:
    return str(getattr(value, "value", value))


# ----- bodies --------------------------------------------------------------------------


class WorkspaceBody(RuleBody):
    RULES = (
        ("name", False, True, text(60, 1)),
        ("icon", True, True, text(40)),
        ("color", True, True, validators.color_rules),
    )
    name: object = None
    icon: object = None
    color: object = None


class WorkspacePatch(RuleBody):
    RULES = (
        ("name", True, True, text(60, 1)),
        ("icon", True, True, text(40)),
        ("color", True, True, validators.color_rules),
    )
    name: object = None
    icon: object = None
    color: object = None


class InviteBody(RuleBody):
    RULES = (
        ("email", False, True, lambda v, f: validators.email_rules(v, f, 320)),
        ("role", True, False, choice(("ADMIN", "MEMBER"))),
    )
    email: object = None
    role: object = None


class RoleBody(RuleBody):
    RULES = (("role", False, False, choice(("ADMIN", "MEMBER"))),)
    role: object = None


class TransferBody(RuleBody):
    RULES = (("userId", False, False, validators.string_rules),)
    userId: object = None


class DeleteBody(RuleBody):
    RULES = (("confirmName", False, False, validators.string_rules),)
    confirmName: object = None


class MeBody(RuleBody):
    RULES = (("showInCalendar", False, False, validators.bool_rules),)
    showInCalendar: object = None


# ----- access --------------------------------------------------------------------------


async def _membership(workspace_id: str, user_id: str):
    member = await db.workspacemember.find_unique(
        where={"workspaceId_userId": {"workspaceId": workspace_id, "userId": user_id}}
    )
    if member is None:
        raise nest_error(404, NOT_FOUND)
    return member


async def _admin(workspace_id: str, user_id: str):
    member = await _membership(workspace_id, user_id)
    if _role(member.role) not in ADMIN_ROLES:
        raise nest_error(404, NOT_FOUND)
    return member


async def _owner(workspace_id: str, user_id: str):
    member = await _membership(workspace_id, user_id)
    if _role(member.role) != "OWNER":
        raise nest_error(404, NOT_FOUND)
    return member


async def _profiles(user_ids: list[str]) -> dict:
    return {s.userId: s for s in await db.usersettings.find_many(where={"userId": {"in": user_ids}})}


async def _display_name(user) -> str:
    settings = await db.usersettings.find_unique(where={"userId": user.id})
    return (settings.name if settings is not None and settings.name else None) or user.email


# ----- ownership and endings -----------------------------------------------------------


async def attachment_keys(client, workspace_id: str) -> list[str]:
    return [a.storageKey for a in await client.attachment.find_many(where={"workspaceId": workspace_id})]


async def delete_workspace(workspace_id: str) -> None:
    """Its files from storage first (best effort), then the workspace and everything in it."""
    from app import storage

    await storage.delete_objects(await attachment_keys(db, workspace_id))
    await db.workspace.delete(where={"id": workspace_id})


async def settle_workspace(client, workspace_id: str) -> bool:
    """After a member row is gone: hand ownership on if the owner went. False when no one is
    left (the caller deletes the workspace)."""
    rest = await client.workspacemember.find_many(where={"workspaceId": workspace_id}, order={"joinedAt": "asc"})
    if not rest:
        return False
    if not any(_role(m.role) == "OWNER" for m in rest):
        admins = [m for m in rest if _role(m.role) == "ADMIN"]
        heir = (admins or rest)[0]
        await client.workspacemember.update(where={"id": heir.id}, data={"role": "OWNER"})
    return True


async def remove_member(workspace_id: str, user_id: str) -> None:
    """They leave; what they added stays. Their tasks there become unassigned."""
    async with db.tx() as tx:
        await tx.task.update_many(where={"workspaceId": workspace_id, "assigneeId": user_id}, data={"assigneeId": None})
        await tx.workspacemember.delete(
            where={"workspaceId_userId": {"workspaceId": workspace_id, "userId": user_id}}
        )
        alive = await settle_workspace(tx, workspace_id)
    if not alive:
        await delete_workspace(workspace_id)


async def settle_workspaces_for_deleted_user(user_id: str) -> None:
    """Before an account is deleted: the work it added stays in its workspaces. Ownership
    passes on first if it owned one; what it created there is re-owned to the (new) owner,
    so the user cascade doesn't take it (createdById still says who added it); its tasks
    there become unassigned. A workspace with no one left goes, with its files."""
    memberships = await db.workspacemember.find_many(where={"userId": user_id})
    member_of = {m.workspaceId for m in memberships}
    for m in memberships:
        workspace_id = m.workspaceId
        async with db.tx() as tx:
            await tx.task.update_many(where={"workspaceId": workspace_id, "assigneeId": user_id}, data={"assigneeId": None})
            await tx.workspacemember.delete(where={"id": m.id})
            alive = await settle_workspace(tx, workspace_id)
            if alive:
                owner = await tx.workspacemember.find_first(where={"workspaceId": workspace_id, "role": "OWNER"})
                for model in ("task", "event", "project", "note", "attachment"):
                    await getattr(tx, model).update_many(
                        where={"workspaceId": workspace_id, "userId": user_id}, data={"userId": owner.userId}
                    )
        if not alive:
            await delete_workspace(workspace_id)
    # what it added to workspaces it had already left stays too, with each one's owner
    for model in ("task", "event", "project", "note", "attachment"):
        rows = await getattr(db, model).find_many(where={"userId": user_id, "workspaceId": {"not": None}})
        for workspace_id in {r.workspaceId for r in rows} - member_of:
            owner = await db.workspacemember.find_first(where={"workspaceId": workspace_id, "role": "OWNER"})
            if owner is not None:
                await getattr(db, model).update_many(
                    where={"workspaceId": workspace_id, "userId": user_id}, data={"userId": owner.userId}
                )


# ----- views ---------------------------------------------------------------------------


def _workspace_view(w, member, count: int) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "icon": w.icon,
        "color": w.color,
        "role": _role(member.role),
        "showInCalendar": member.showInCalendar,
        "memberCount": count,
        "createdAt": w.createdAt,
    }


async def mine(user_id: str) -> list[dict]:
    rows = await db.workspacemember.find_many(where={"userId": user_id}, include={"workspace": True}, order={"joinedAt": "asc"})
    out = []
    for m in rows:
        count = await db.workspacemember.count(where={"workspaceId": m.workspaceId})
        out.append(_workspace_view(m.workspace, m, count))
    return out


# ----- routes: workspaces --------------------------------------------------------------


@router.get("/workspaces")
async def list_workspaces(user=Depends(plus_user)):
    return await mine(user.id)


@router.post("/workspaces", status_code=201)
async def create(body: WorkspaceBody, user=Depends(plus_user)):
    async with db.tx() as tx:
        w = await tx.workspace.create(data={"name": body.name, "icon": body.icon, "color": body.color})
        member = await tx.workspacemember.create(data={"workspaceId": w.id, "userId": user.id, "role": "OWNER"})
    return _workspace_view(w, member, 1)


@router.patch("/workspaces/{id}")
async def update(id: str, body: WorkspacePatch, user=Depends(plus_user)):
    member = await _admin(id, user.id)
    data = {k: getattr(body, k) for k in ("name", "icon", "color") if body.sent(k)}
    if "name" in data and data["name"] is None:
        raise nest_error(400, "name should not be empty")
    w = await db.workspace.update(where={"id": id}, data=data) if data else await db.workspace.find_unique(where={"id": id})
    return _workspace_view(w, member, await db.workspacemember.count(where={"workspaceId": id}))


@router.delete("/workspaces/{id}")
async def remove(id: str, body: DeleteBody, user=Depends(plus_user)):
    await _owner(id, user.id)
    w = await db.workspace.find_unique(where={"id": id})
    if body.confirmName.strip() != w.name.strip():
        raise nest_error(400, "Type the workspace's name to delete it")
    await delete_workspace(id)
    return {"ok": True}


@router.patch("/workspaces/{id}/me")
async def update_me(id: str, body: MeBody, user=Depends(plus_user)):
    member = await _membership(id, user.id)
    await db.workspacemember.update(where={"id": member.id}, data={"showInCalendar": body.showInCalendar})
    return {"ok": True}


@router.get("/workspaces/{id}/members")
async def members(id: str, user=Depends(plus_user)):
    me = await _membership(id, user.id)
    rows = await db.workspacemember.find_many(where={"workspaceId": id}, include={"user": True}, order={"joinedAt": "asc"})
    profiles = await _profiles([m.userId for m in rows])
    out = {
        "members": [
            {
                "userId": m.userId,
                "name": profiles[m.userId].name if m.userId in profiles else None,
                "avatarColor": profiles[m.userId].avatarColor if m.userId in profiles else None,
                "email": m.user.email,
                "role": _role(m.role),
                "joinedAt": m.joinedAt,
                "isYou": m.userId == user.id,
            }
            for m in rows
        ],
        "invites": [],
    }
    if _role(me.role) in ADMIN_ROLES:
        now = datetime.now(timezone.utc)
        invites = await db.workspaceinvite.find_many(
            where={"workspaceId": id, "usedAt": None, "expiresAt": {"gt": now}}, order={"createdAt": "asc"}
        )
        out["invites"] = [{"id": i.id, "email": i.email, "role": _role(i.role), "expiresAt": i.expiresAt} for i in invites]
    return out


@router.post("/workspaces/{id}/invites", status_code=201)
async def invite(id: str, body: InviteBody, user=Depends(plus_user)):
    me = await _admin(id, user.id)
    # only the owner makes admins, by invite as by promotion
    if body.role == "ADMIN" and _role(me.role) != "OWNER":
        raise nest_error(404, NOT_FOUND)
    email = body.email.strip().lower()
    existing = await db.user.find_unique(where={"email": email})
    if existing and await member_role(id, existing.id):
        raise nest_error(409, "They're already in this workspace")
    code = new_invite_code() + new_invite_code()  # 16 characters: it travels in a link
    now = datetime.now(timezone.utc)
    async with db.tx() as tx:
        # one live invite per address: a new one replaces the old
        await tx.workspaceinvite.update_many(where={"workspaceId": id, "email": email, "usedAt": None}, data={"usedAt": now})
        row = await tx.workspaceinvite.create(
            data={
                "workspaceId": id,
                "email": email,
                "role": body.role or "MEMBER",
                "codeHash": hash_code(code),
                "invitedById": user.id,
                "expiresAt": now + timedelta(days=INVITE_DAYS),
            }
        )
    w = await db.workspace.find_unique(where={"id": id})
    await mailer.send_workspace_invite(email, w.name, await _display_name(user), f"{web_url()}/invite/workspace/{code}")
    return {"id": row.id, "email": row.email, "role": _role(row.role), "expiresAt": row.expiresAt}


@router.delete("/workspaces/{id}/invites/{invite_id}")
async def cancel_invite(id: str, invite_id: str, user=Depends(plus_user)):
    await _admin(id, user.id)
    await db.workspaceinvite.delete_many(where={"id": invite_id, "workspaceId": id})
    return {"ok": True}


@router.patch("/workspaces/{id}/members/{member_user_id}")
async def change_role(id: str, member_user_id: str, body: RoleBody, user=Depends(plus_user)):
    await _owner(id, user.id)
    target = await _membership(id, member_user_id)
    if _role(target.role) == "OWNER":
        raise nest_error(400, "Transfer ownership to change the owner's role")
    await db.workspacemember.update(where={"id": target.id}, data={"role": body.role})
    return {"ok": True}


@router.post("/workspaces/{id}/transfer")
async def transfer(id: str, body: TransferBody, user=Depends(plus_user)):
    me = await _owner(id, user.id)
    target = await _membership(id, body.userId)
    if target.id == me.id:
        return {"ok": True}
    async with db.tx() as tx:
        await tx.workspacemember.update(where={"id": me.id}, data={"role": "ADMIN"})
        await tx.workspacemember.update(where={"id": target.id}, data={"role": "OWNER"})
    return {"ok": True}


@router.delete("/workspaces/{id}/members/{member_user_id}")
async def remove_someone(id: str, member_user_id: str, user=Depends(plus_user)):
    me = await _admin(id, user.id)
    target = await _membership(id, member_user_id)
    if _role(target.role) == "OWNER" or (_role(target.role) == "ADMIN" and _role(me.role) != "OWNER"):
        raise nest_error(404, "Member not found")
    await remove_member(id, member_user_id)
    return {"ok": True}


@router.post("/workspaces/{id}/leave")
async def leave(id: str, user=Depends(plus_user)):
    await _membership(id, user.id)
    if await db.workspacemember.count(where={"workspaceId": id}) <= 1:
        # leaving would end it, and everything in it: that's Delete, with its typed name
        raise nest_error(400, "You're the only member. Delete the workspace instead.")
    await remove_member(id, user.id)
    return {"ok": True}


# ----- routes: accepting an invite -----------------------------------------------------


async def _live_invite(code: str):
    now = datetime.now(timezone.utc)
    row = await db.workspaceinvite.find_unique(where={"codeHash": hash_code(code.strip())}, include={"workspace": True})
    if row is None or row.usedAt is not None or row.expiresAt <= now:
        raise nest_error(404, "This invite is not valid or has expired")
    return row


@router.get("/invites/workspace/{code}")
async def preview(code: str, user=Depends(web_user)):
    row = await _live_invite(code)
    inviter = await db.user.find_unique(where={"id": row.invitedById})
    return {
        "workspaceName": row.workspace.name,
        "invitedBy": await _display_name(inviter) if inviter else None,
        "email": row.email,
        "role": _role(row.role),
        "emailMatches": user.email.lower() == row.email,
    }


@router.post("/invites/workspace/{code}/accept")
async def accept(code: str, user=Depends(plus_user)):
    row = await _live_invite(code)
    if user.email.lower() != row.email or user.emailVerifiedAt is None:
        raise nest_error(403, WRONG_EMAIL if user.email.lower() != row.email else "Confirm your email first.")
    now = datetime.now(timezone.utc)
    async with db.tx() as tx:
        claimed = await tx.workspaceinvite.update_many(where={"id": row.id, "usedAt": None}, data={"usedAt": now, "usedById": user.id})
        if not claimed:
            raise nest_error(404, "This invite is not valid or has expired")
        if not await tx.workspacemember.find_unique(
            where={"workspaceId_userId": {"workspaceId": row.workspaceId, "userId": user.id}}
        ):
            await tx.workspacemember.create(data={"workspaceId": row.workspaceId, "userId": user.id, "role": row.role})
    inviter = await db.user.find_unique(where={"id": row.invitedById})
    if inviter is not None:
        await mailer.send_invite_accepted(inviter.email, await _display_name(user), row.workspace.name)
    return {"id": row.workspaceId, "name": row.workspace.name}
