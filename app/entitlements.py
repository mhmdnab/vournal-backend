"""Subscription entitlements — the single source of truth for what a user is
entitled to RIGHT NOW (Step 14a).

Nothing reads `User.plan` directly. Everything goes through `effective_plan()`,
which returns the granted plan ONLY while the entitlement is live (no expiry, or
a future expiry) and otherwise falls back to `free`. That fail-safe is the whole
point: whatever grants a plan later (Paddle in Step 14b, or the dev tool) can't
leave access stuck-granted — it lapses to `free` the moment the entitlement
expires, rather than sticking forever.
"""

from __future__ import annotations

from datetime import datetime, timezone

# The tiers. Keep in lockstep with the Prisma `Plan` enum.
#   free — $0, the default for everyone
#   plus — ~$4/mo: the single paid plan (longer recordings, higher caps, and the
#          premium AI features when they land)
FREE = "free"
PLUS = "plus"
PLANS = (FREE, PLUS)

# The paid tier (lifts the free caps).
PAID_PLANS = (PLUS,)


def _plan_str(value) -> str:
    """Coerce a Prisma `Plan` enum member (a StrEnum) or a raw string to a plain
    lowercase plan string. Unknown/None → free."""
    if value is None:
        return FREE
    return str(getattr(value, "value", value))


def effective_plan(user) -> str:
    """The user's entitlement right now — the ONLY function that decides access.

    Returns the stored plan while it's live (`planExpiresAt` is null, i.e. no
    expiry, or is in the future); otherwise `free`. Fail-safe by design: access
    lapses to `free` when the entitlement expires instead of staying granted.
    """
    plan = _plan_str(getattr(user, "plan", None))
    if plan not in PAID_PLANS:
        return FREE  # free, or any unexpected value → free

    expires = getattr(user, "planExpiresAt", None)
    if expires is None:
        return plan  # granted without an expiry (e.g. a lifetime/manual grant)

    # Compare in aware UTC (DB datetimes may come back naive).
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return plan if expires > datetime.now(timezone.utc) else FREE


def is_paid(user) -> bool:
    """True if the user's live entitlement is any paid tier."""
    return effective_plan(user) in PAID_PLANS


# ---- Writes (build 5 billing) -------------------------------------------------
# The ONLY code that writes plan / planExpiresAt. Apple purchases, App Store Server
# Notifications and Paddle webhooks all come through here, so two sources can't
# fight: the later expiry always wins, and a source can only end its own grant.

SOURCES = ("apple", "paddle", "manual")


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def grant_plus(
    db,
    user_id: str,
    expires_at: datetime,
    source: str,
    external_id: str | None,
    purchased_at: datetime | None = None,
) -> None:
    """Grant Plus until `expires_at` — unless a live grant already runs longer.

    A lifetime grant (paid, no expiry) is never shortened. An Apple purchase made
    before the user's last refund/revocation is ignored, so a stale signed
    transaction (replayed by the device, or retried late by Apple) can't undo a
    refund. Apple reuses one original transaction id per Apple ID, so it moves to
    the account that bought most recently.
    """
    if source not in SOURCES:
        raise ValueError(f"Unknown entitlement source: {source}")
    user = await db.user.find_unique(where={"id": user_id})
    if user is None:
        return
    revoked = _aware(getattr(user, "appleRevokedAt", None))
    if source == "apple" and revoked and purchased_at and _aware(purchased_at) <= revoked:
        return
    expires_at = _aware(expires_at)
    lifetime = is_paid(user) and user.planExpiresAt is None
    current = _aware(user.planExpiresAt) if is_paid(user) else None
    governs = not lifetime and (current is None or expires_at > current)
    data: dict = {}
    if governs:
        data.update(plan=PLUS, planExpiresAt=expires_at, planSource=source)
    if external_id and source == "apple":
        await db.user.update_many(
            where={"appleOriginalTransactionId": external_id, "id": {"not": user_id}},
            data={"appleOriginalTransactionId": None},
        )
        data["appleOriginalTransactionId"] = external_id
    if external_id and source == "paddle" and (governs or not user.paddleSubscriptionId):
        data["paddleSubscriptionId"] = external_id
    if data:
        await db.user.update(where={"id": user_id}, data=data)


async def revoke_plus(
    db,
    user_id: str,
    source: str,
    external_id: str | None = None,
    refunded: bool = False,
) -> None:
    """End `source`'s grant now (refund, revoke, expiry, cancellation).

    Only the subscription named by `external_id` is ended: a cancelled duplicate
    or an old subscription leaves the current one alone, and a grant from another
    source that set the current expiry is left alone. `refunded` records the
    moment, so older Apple purchases can't grant Plus again.
    """
    user = await db.user.find_unique(where={"id": user_id})
    if user is None:
        return
    now = datetime.now(timezone.utc)
    if refunded and source == "apple":
        await db.user.update(where={"id": user_id}, data={"appleRevokedAt": now})
    stored = {
        "apple": user.appleOriginalTransactionId,
        "paddle": user.paddleSubscriptionId,
    }.get(source)
    if external_id and stored and external_id != stored:
        return
    if user.planSource != source or user.planExpiresAt is None:
        return
    await db.user.update(where={"id": user_id}, data={"planExpiresAt": now})


async def account_token_for(db, user_id: str) -> str:
    """The user's App Store appAccountToken (a UUID4), created once."""
    import uuid

    user = await db.user.find_unique(where={"id": user_id})
    if user.appAccountToken:
        return user.appAccountToken
    # Only set it if still unset, so two first calls can't overwrite a token a
    # purchase already carries; then read back whichever won.
    await db.user.update_many(
        where={"id": user_id, "appAccountToken": None},
        data={"appAccountToken": str(uuid.uuid4())},
    )
    return (await db.user.find_unique(where={"id": user_id})).appAccountToken


async def apply_pending_grants(db, user) -> None:
    """Grant any web purchase made with this email before the account existed."""
    now = datetime.now(timezone.utc)
    pending = await db.pendinggrant.find_many(
        where={"email": user.email.lower(), "appliedAt": None, "expiresAt": {"gt": now}}
    )
    for grant in pending:
        await grant_plus(db, user.id, grant.expiresAt, grant.source, grant.externalId)
        await db.pendinggrant.update(where={"id": grant.id}, data={"appliedAt": now})
