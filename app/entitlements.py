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
