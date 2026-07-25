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

log = logging.getLogger("vournal.quota")

# Per-user monthly entry limits by tier. Everyone is on "beta" for now; the
# mechanism (user_monthly_limit) is wired so a paid tier can raise it later.
BETA_MONTHLY_ENTRY_LIMIT = int(os.environ.get("BETA_MONTHLY_ENTRY_LIMIT", "60"))
FAIR_USE_MONTHLY_LIMIT = int(os.environ.get("FAIR_USE_MONTHLY_LIMIT", "300"))  # future paid tier

# Longest recording accepted (seconds), rejected before upload.
MAX_RECORDING_SECONDS = int(os.environ.get("MAX_RECORDING_SECONDS", "600"))  # 10 min

# Whole-app daily cap on NEW audio entries — the kill switch.
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
    """The monthly entry limit for a user's tier. Beta for everyone today; this is
    the seam where a paid tier would return FAIR_USE_MONTHLY_LIMIT instead."""
    return BETA_MONTHLY_ENTRY_LIMIT


def reject_if_recording_too_long(duration_sec) -> None:
    """Reject an over-length recording BEFORE any upload/STT."""
    if duration_sec is not None and duration_sec > MAX_RECORDING_SECONDS:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "RECORDING_TOO_LONG",
                "maxSeconds": MAX_RECORDING_SECONDS,
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
