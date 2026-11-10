"""The email a week before the free months of Plus end (spec 2026-11-09).

Sent once per account (trialReminderSentAt), only while the trial is still what
they're on: someone who subscribed during it gets nothing. Each account is claimed
before its email goes out, so two runs never send it twice.
"""

import logging
from datetime import datetime, timedelta

from app import email as mailer
from app.db import db
from app.entitlements import is_paid

log = logging.getLogger("vournal.trial")

REMIND_BEFORE = timedelta(days=7)


async def send_trial_reminders(now: datetime) -> int:
    """Email every account whose trial ends within a week; the number sent."""
    due = await db.user.find_many(
        where={
            "trialEndsAt": {"gt": now, "lte": now + REMIND_BEFORE},
            "trialReminderSentAt": None,
            "planSource": "trial",
        }
    )
    sent = 0
    for user in due:
        if not is_paid(user):
            continue
        claimed = await db.user.update_many(
            where={"id": user.id, "trialReminderSentAt": None}, data={"trialReminderSentAt": now}
        )
        if not claimed:
            continue
        try:
            if await mailer.send_trial_ending(user.email, user.trialEndsAt):
                sent += 1
        except Exception:  # noqa: BLE001 — one account never stops the run
            log.exception("trial reminder for user %s failed", user.id)
    return sent
