"""/api/household (port of vournalOS HouseholdService and household.access): a small group
that shares shopping lists and calendar events; everything else stays private.

Rules: one household per user, up to 8 members, single-use invite codes valid for 7
days. Leaving (or being removed) takes your own shared lists and events back out of the
household. Every member needs their own Plus: the /api gate already blocks one without.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends
from prisma.errors import UniqueViolationError

from app.db import db
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, text
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.rate_limit import rate_limited

router = nest_router("/household")

MAX_MEMBERS = 8
INVITE_DAYS = 7
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: codes are read aloud and typed on phones
_NOT_CODE = re.compile(r"[^A-Z0-9]")
_CODE = re.compile(r"^[A-Z0-9]{8}\Z")


def new_invite_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


# ----- access (household.access) -------------------------------------------------------


async def household_id_of(user_id: str) -> str | None:
    member = await db.householdmember.find_unique(where={"userId": user_id})
    return member.householdId if member else None


def list_access(user_id: str, household_id: str | None) -> dict:
    """Shopping lists the user may open: their own, plus the ones shared with their household."""
    return {"OR": [{"userId": user_id}, {"householdId": household_id}]} if household_id else {"userId": user_id}


async def settle_household(client, household_id: str) -> None:
    """After a member row is gone: the household goes with its last member, and if the
    owner went, the longest-standing member takes over."""
    rest = await client.householdmember.find_many(where={"householdId": household_id}, order={"joinedAt": "asc"})
    if not rest:
        await client.household.delete(where={"id": household_id})
    elif not any(m.role == "OWNER" for m in rest):
        await client.householdmember.update(where={"id": rest[0].id}, data={"role": "OWNER"})


# ----- bodies --------------------------------------------------------------------------


class NameBody(RuleBody):
    RULES = (("name", False, True, text(60, 1)),)
    name: object = None


def _code_rules(value, field):
    if isinstance(value, str):
        value = _NOT_CODE.sub("", value.upper())
    problems = [] if isinstance(value, str) and _CODE.match(value) else ["Enter the 8-character invite code"]
    return problems + validators.string_rules(value, field)


class JoinBody(RuleBody):
    """The code as shown to the inviter, e.g. "K7QM-4TXP": dashes, spaces and case are ignored."""

    RULES = (("code", False, False, _code_rules),)
    code: object = None

    def normalized(self) -> str:
        return _NOT_CODE.sub("", self.code.upper())


# ----- the household -------------------------------------------------------------------


async def mine(user_id: str) -> dict:
    me = await db.householdmember.find_unique(where={"userId": user_id}, include={"household": True})
    if me is None:
        return {"household": None, "maxMembers": MAX_MEMBERS}
    h = me.household
    members = await db.householdmember.find_many(where={"householdId": h.id}, order={"joinedAt": "asc"})
    profiles = {s.userId: s for s in await db.usersettings.find_many(where={"userId": {"in": [m.userId for m in members]}})}
    return {
        "maxMembers": MAX_MEMBERS,
        "household": {
            "id": h.id,
            "name": h.name,
            "createdAt": h.createdAt,
            "role": me.role,
            "sharedLists": await db.shoppinglist.count(where={"householdId": h.id}),
            "sharedEvents": await db.event.count(where={"householdId": h.id}),
            "members": [
                {
                    "userId": m.userId,
                    "name": profiles[m.userId].name if m.userId in profiles else None,
                    "avatarColor": profiles[m.userId].avatarColor if m.userId in profiles else None,
                    "role": m.role,
                    "joinedAt": m.joinedAt,
                    "isYou": m.userId == user_id,
                }
                for m in members
            ],
        },
    }


async def _require_member(user_id: str):
    me = await db.householdmember.find_unique(where={"userId": user_id})
    if me is None:
        raise nest_error(404, "You are not in a household")
    return me


async def _assert_not_member(user_id: str) -> None:
    if await db.householdmember.count(where={"userId": user_id}):
        raise nest_error(409, "You are already in a household. Leave it first to join another.")


async def _remove_membership(household_id: str, user_id: str) -> None:
    """Removes a member and takes THEIR shared rows back out of the household (they keep
    them, privately)."""
    async with db.tx() as tx:
        await tx.shoppinglist.update_many(where={"userId": user_id, "householdId": household_id}, data={"householdId": None})
        await tx.event.update_many(where={"userId": user_id, "householdId": household_id}, data={"householdId": None})
        await tx.householdmember.delete(where={"userId": user_id})
        await settle_household(tx, household_id)


@router.get("")
async def get_household(user=Depends(plus_user)):
    return await mine(user.id)


@router.post("", status_code=201)
async def create(body: NameBody, user=Depends(plus_user)):
    await _assert_not_member(user.id)
    try:
        async with db.tx() as tx:
            h = await tx.household.create(data={"name": body.name})
            await tx.householdmember.create(data={"householdId": h.id, "userId": user.id, "role": "OWNER"})
    except UniqueViolationError:
        raise nest_error(409, "You are already in a household") from None
    await log_activity(user.id, "household.created", entity_id=h.id)
    return await mine(user.id)


@router.patch("")
async def rename(body: NameBody, user=Depends(plus_user)):
    me = await _require_member(user.id)
    if me.role != "OWNER":
        raise nest_error(403, "Only the owner can rename the household")
    await db.household.update(where={"id": me.householdId}, data={"name": body.name})
    return await mine(user.id)


@router.post("/invite")
async def invite(user=Depends(plus_user)):
    """A fresh single-use code (the inviter's previous unused codes stop working)."""
    me = await _require_member(user.id)
    if await db.householdmember.count(where={"householdId": me.householdId}) >= MAX_MEMBERS:
        raise nest_error(400, f"A household can have up to {MAX_MEMBERS} members")
    code = new_invite_code()
    expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)
    async with db.tx() as tx:
        await tx.householdinvite.delete_many(where={"householdId": me.householdId, "createdById": user.id, "usedAt": None})
        await tx.householdinvite.create(
            data={"householdId": me.householdId, "createdById": user.id, "codeHash": hash_code(code), "expiresAt": expires_at}
        )
    return {"code": format_code(code), "expiresAt": expires_at}


@router.post("/join")
async def join(body: JoinBody, user=Depends(plus_user)):
    # NestJS allowed 10 tries per 10 minutes per address; behind the dashboard's proxy every
    # web user shares one address, so the same allowance is kept per account
    if rate_limited("household-join", user.id, 10, 600):
        raise nest_error(429, "ThrottlerException: Too Many Requests")
    await _assert_not_member(user.id)
    invite_row = await db.householdinvite.find_unique(where={"codeHash": hash_code(body.normalized())})
    # one message for every failure: do not reveal whether a code ever existed
    invalid = nest_error(400, "This invite code is not valid or has expired")
    now = datetime.now(timezone.utc)
    if invite_row is None or invite_row.usedAt or invite_row.expiresAt < now:
        raise invalid
    try:
        async with db.tx() as tx:
            claimed = await tx.householdinvite.update_many(
                where={"id": invite_row.id, "usedAt": None}, data={"usedAt": now, "usedById": user.id}
            )
            if claimed != 1:
                raise invalid
            if await tx.householdmember.count(where={"householdId": invite_row.householdId}) >= MAX_MEMBERS:
                raise nest_error(400, f"This household is full ({MAX_MEMBERS} members)")
            await tx.householdmember.create(data={"householdId": invite_row.householdId, "userId": user.id, "role": "MEMBER"})
    except UniqueViolationError:
        raise nest_error(409, "You are already in a household") from None
    await log_activity(user.id, "household.joined", entity_id=invite_row.householdId)
    return await mine(user.id)


@router.post("/leave")
async def leave(user=Depends(plus_user)):
    me = await _require_member(user.id)
    await _remove_membership(me.householdId, user.id)
    await log_activity(user.id, "household.left", entity_id=me.householdId)
    return await mine(user.id)


@router.delete("/members/{member_user_id}")
async def remove_member(member_user_id: str, user=Depends(plus_user)):
    me = await _require_member(user.id)
    if me.role != "OWNER":
        raise nest_error(403, "Only the owner can remove members")
    if member_user_id == user.id:
        raise nest_error(400, 'Use "leave" to leave your household')
    target = await db.householdmember.find_first(where={"userId": member_user_id, "householdId": me.householdId})
    if target is None:
        raise nest_error(404, "Member not found")
    await _remove_membership(me.householdId, member_user_id)
    await log_activity(user.id, "household.memberRemoved", entity_id=me.householdId)
    return await mine(user.id)
