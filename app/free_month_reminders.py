"""The email a few days before a store's free month of Plus ends (owner 2026-11-24).

The stores tell us everything: Apple's signed transaction says it's the introductory free
trial and when it ends; its renewal info says whether it will renew; Paddle says "trialing"
and whether a cancel is scheduled. The billing handlers record that here, and a daily job
sends one email per free month, in the version that fits ("it renews" or "it ends").
"""

from __future__ import annotations

import logging
import os
import zoneinfo
from datetime import datetime, timedelta, timezone

from app import email as mailer
from app.db import db
from app.entitlements import is_paid

log = logging.getLogger("vournal.free_month")

REMIND_BEFORE = timedelta(days=3)
TOO_LATE = timedelta(hours=20)  # a reminder that would land on the last day isn't sent
APPLE_MANAGE_URL = "https://apps.apple.com/account/subscriptions"


def is_apple_free_trial(transaction) -> bool:
    """Apple's introductory offer (offerType 1) given as a free trial."""
    return (
        getattr(transaction, "rawOfferType", None) == 1
        and getattr(transaction, "rawOfferDiscountType", None) == "FREE_TRIAL"
        and not getattr(transaction, "revocationDate", None)
    )


async def record_free_month(user_id: str, ends_at: datetime, auto_renew: bool | None = None) -> None:
    """Remember when this free month ends. A different end date is a new free month, so its
    reminder can go out again."""
    user = await db.user.find_unique(where={"id": user_id})
    if user is None:
        return
    data: dict = {"planIntroEndsAt": ends_at}
    known = user.planIntroEndsAt
    if known is None or abs(_aware(known) - _aware(ends_at)) > timedelta(minutes=1):
        data["planIntroReminderSentAt"] = None
    if auto_renew is not None:
        data["planAutoRenew"] = auto_renew
    await db.user.update(where={"id": user_id}, data=data)


async def record_auto_renew(user_id: str, on: bool) -> None:
    await db.user.update(where={"id": user_id}, data={"planAutoRenew": on})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _local(when: datetime, zone: str | None) -> datetime:
    try:
        return when.astimezone(zoneinfo.ZoneInfo(zone or "UTC"))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return when.astimezone(timezone.utc)


def _days_left(ends: datetime, now: datetime) -> int:
    return max(1, round((ends - now) / timedelta(days=1)))


async def send_free_month_reminders(now: datetime) -> int:
    """Email every account whose store free month ends within 3 days; the number sent."""
    due = await db.user.find_many(
        where={
            "planIntroEndsAt": {"gt": now + TOO_LATE, "lte": now + REMIND_BEFORE},
            "planIntroReminderSentAt": None,
            "planSource": {"in": ["apple", "paddle"]},
        }
    )
    sent = 0
    for user in due:
        if not is_paid(user):
            continue  # refunded, revoked or lapsed: no free month to talk about
        claimed = await db.user.update_many(
            where={"id": user.id, "planIntroReminderSentAt": None}, data={"planIntroReminderSentAt": now}
        )
        if not claimed:
            continue  # another run took it
        ends = _aware(user.planIntroEndsAt)
        apple = user.planSource == "apple"
        manage = APPLE_MANAGE_URL if apple else (os.environ.get("PADDLE_MANAGE_URL", "").strip() or "https://paddle.net")
        try:
            if await mailer.send_free_month_ending(
                user.email,
                _local(ends, user.timezone),
                _days_left(ends, now),
                renews=user.planAutoRenew is not False,
                billed_by="apple" if apple else "paddle",
                manage_url=manage,
            ):
                sent += 1
            else:
                # not delivered (Resend down or not set up): the next run tries again
                await db.user.update(where={"id": user.id}, data={"planIntroReminderSentAt": None})
        except Exception:  # noqa: BLE001 — one account never stops the run
            log.exception("free month reminder for user %s failed", user.id)
    return sent
