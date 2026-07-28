"""Usage metering + cost-protection quotas (Step 11).

Every audio entry costs real money (STT + a Sonnet extraction call), and the
endpoint is publicly reachable — so we bound cost by metering TWO things together
(entry count alone bounds nothing: a 10-min recording costs ~3x a 3-min one):

  - entries per calendar month, per user, from a configurable tier limit, and
  - a max recording length, rejected before any provider call.

Plus a whole-app **daily** cap on new audio — the 3am backstop when something is
hammering the endpoint. Everything FAILS CLOSED: if a quota can't be determined,
reject rather than risk unbounded spend. Errors are structured (a machine-readable
`code`) so the clients can render a calm limit state.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException

from app.entitlements import DAILY, FREE, REFLECTION, effective_plan

log = logging.getLogger("vournal.quota")

# Per-user monthly entry limits, by EFFECTIVE plan (Step 14a-fix — final pricing):
#   free       — a low cap, so the free tier can't run up cost.
#   daily      — sized for one entry a day with headroom; a fair-use CEILING, not
#                a target, that stops a bot/heavy recorder from inverting economics.
#   reflection — same generous ceiling as daily (its edge is AI features + length).
FREE_MONTHLY_ENTRY_LIMIT = int(os.environ.get("FREE_MONTHLY_ENTRY_LIMIT", "15"))
DAILY_MONTHLY_ENTRY_LIMIT = int(os.environ.get("DAILY_MONTHLY_ENTRY_LIMIT", "150"))
REFLECTION_MONTHLY_ENTRY_LIMIT = int(os.environ.get("REFLECTION_MONTHLY_ENTRY_LIMIT", "150"))

# Longest recording accepted (seconds), rejected before any provider call — per
# plan: free a short ceiling, daily a bit longer, reflection the longest.
FREE_MAX_RECORDING_SECONDS = int(os.environ.get("FREE_MAX_RECORDING_SECONDS", "90"))  # 1.5 min
DAILY_MAX_RECORDING_SECONDS = int(os.environ.get("DAILY_MAX_RECORDING_SECONDS", "120"))  # 2 min
REFLECTION_MAX_RECORDING_SECONDS = int(os.environ.get("REFLECTION_MAX_RECORDING_SECONDS", "300"))  # 5 min

# Per-plan lookup tables (keyed by the effective plan string; FREE is the fallback
# for any unexpected value — fail to the tightest caps).
_MONTHLY_ENTRY_LIMIT = {
    FREE: FREE_MONTHLY_ENTRY_LIMIT,
    DAILY: DAILY_MONTHLY_ENTRY_LIMIT,
    REFLECTION: REFLECTION_MONTHLY_ENTRY_LIMIT,
}
_MAX_RECORDING_SECONDS = {
    FREE: FREE_MAX_RECORDING_SECONDS,
    DAILY: DAILY_MAX_RECORDING_SECONDS,
    REFLECTION: REFLECTION_MAX_RECORDING_SECONDS,
}

# Whole-app daily cap on NEW audio entries — the kill switch (plan-independent).
GLOBAL_DAILY_AUDIO_CAP = int(os.environ.get("GLOBAL_DAILY_AUDIO_CAP", "500"))


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _next_month_start(now: datetime) -> datetime:
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def user_monthly_limit(user) -> int:
    """The monthly entry limit for the user's EFFECTIVE plan."""
    return _MONTHLY_ENTRY_LIMIT.get(effective_plan(user), FREE_MONTHLY_ENTRY_LIMIT)


def max_recording_seconds(user) -> int:
    """The longest recording accepted for the user's EFFECTIVE plan."""
    return _MAX_RECORDING_SECONDS.get(effective_plan(user), FREE_MAX_RECORDING_SECONDS)


def current_month_used(user) -> int:
    """Entries counted in the CURRENT calendar-month window. Returns 0 when the
    stored window is stale (it rolls to the new month on the next write), so the
    number the client shows matches what the quota will actually enforce."""
    now = datetime.now(timezone.utc)
    start = getattr(user, "usagePeriodStart", None)
    used = getattr(user, "usagePeriodEntries", 0) or 0
    if start is None or start < _month_start(now):
        return 0
    return used


def usage_snapshot(user) -> dict:
    """Everything the client needs to render the plan/usage surface, all derived
    from the user's EFFECTIVE plan so it can't drift from what's enforced."""
    now = datetime.now(timezone.utc)
    return {
        "monthlyLimit": user_monthly_limit(user),
        "monthlyUsed": current_month_used(user),
        "maxRecordingSeconds": max_recording_seconds(user),
        "resetAt": _iso_z(_next_month_start(now)),
    }


def reject_if_recording_too_long(duration_sec, user) -> None:
    """Reject an over-length recording BEFORE any upload/STT, per the user's plan."""
    limit = max_recording_seconds(user)
    if duration_sec is not None and duration_sec > limit:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "RECORDING_TOO_LONG",
                "maxSeconds": limit,
                "seconds": int(duration_sec),
            },
        )


async def check_global_daily_cap(db) -> None:
    """The kill switch: refuse new audio once the whole app has processed
    GLOBAL_DAILY_AUDIO_CAP recordings today. Logs loudly. Fails closed."""
    now = datetime.now(timezone.utc)
    try:
        count = await db.entry.count(
            where={"source": "VOICE", "createdAt": {"gte": _day_start(now)}}
        )
    except Exception:  # noqa: BLE001 — can't determine -> reject
        log.error("kill-switch: could not count today's audio entries; rejecting")
        raise HTTPException(status_code=503, detail={"code": "SERVICE_BUSY"})
    if count >= GLOBAL_DAILY_AUDIO_CAP:
        log.error(
            "KILL SWITCH TRIPPED: %d audio entries today >= cap %d — refusing new audio.",
            count,
            GLOBAL_DAILY_AUDIO_CAP,
        )
        raise HTTPException(status_code=503, detail={"code": "SERVICE_BUSY"})


async def reserve_entry_quota(db, user) -> None:
    """Reserve one entry against the user's monthly limit, rolling the window at
    the calendar month. Raises 429 (structured) when over. Fails closed."""
    now = datetime.now(timezone.utc)
    m0 = _month_start(now)
    limit = user_monthly_limit(user)

    start = getattr(user, "usagePeriodStart", None)
    used = getattr(user, "usagePeriodEntries", 0) or 0
    new_window = start is None or start < m0
    if new_window:
        used = 0

    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "MONTHLY_LIMIT_REACHED",
                "limit": limit,
                "used": used,
                "resetAt": _iso_z(_next_month_start(now)),
            },
        )

    try:
        if new_window:
            await db.user.update(
                where={"id": user.id},
                data={"usagePeriodStart": m0, "usagePeriodEntries": 1},
            )
        else:
            await db.user.update(
                where={"id": user.id},
                data={"usagePeriodEntries": {"increment": 1}},
            )
    except Exception:  # noqa: BLE001 — couldn't record the reservation -> reject
        log.error("quota: failed to reserve entry for user %s; rejecting", user.id)
        raise HTTPException(status_code=503, detail={"code": "QUOTA_UNAVAILABLE"})
