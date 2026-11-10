"""The scheduled jobs vournalOS ran with @nestjs/schedule, on the same schedules (UTC):

- notifications-tick, every minute: reminders, task and event reminders, habits, the digest;
- notifications-sweep, every 15 minutes: bills, debts, documents, maintenance, people, budgets;
- notifications-cleanup, at 03:00: read notifications older than 90 days;
- assistant-undo-cleanup, at 03:05: the chat's undo copies older than 24 hours;
- entry-review-cleanup, at 03:05: app recordings left unsaved on the review card for 7 days;
- finance-recurring-transactions and finance-recurring-autolog, hourly;
- market-rates, at minute 17 every 6 hours;
- plus-trial-reminders, at 09:00: the email a week before the free months of Plus end.

Started from the app's startup; VOURNAL_OS_JOBS=0 switches them off. Each run holds a
Postgres advisory lock for as long as it runs (in a transaction, so a crash or a dropped
connection releases it), so two processes never run a job at the same time. One may repeat
a run the other just finished (there's no run record), which is harmless: every job is safe
to repeat (dedupe keys, conditional updates). Accounts without Plus are skipped, and one failing account never stops a run.

Two lanes, each running one job at a time: the tick alone, so the long jobs never make a
reminder late, and everything else. Each lane holds one connection for its lock."""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.db import db
from app.entitlements import FREE, effective_plan
from app.os import market_rates, notification_sync, recurring_payments, transactions

log = logging.getLogger("vournal.os")

_LOCK_WAIT = timedelta(seconds=10)


async def run_locked(name: str, work: Callable[[], Awaitable[object]], timeout: timedelta = timedelta(minutes=30)) -> bool:
    """Runs `work` unless another process holds this job's lock; True when it ran here.
    An error in `work` is logged, never raised."""
    ran = False
    try:
        async with db.tx(max_wait=_LOCK_WAIT, timeout=timeout) as tx:
            rows = await tx.query_raw("SELECT pg_try_advisory_xact_lock(hashtext($1)) AS ok", f"vournal-os-job:{name}")
            if not rows[0]["ok"]:
                return False
            ran = True
            try:
                await work()
            except Exception:  # noqa: BLE001 — logged; the lock goes with the transaction
                log.exception("job %s failed", name)
    except Exception:  # noqa: BLE001 — no connection, or the run outlived its transaction
        log.exception("job %s: lock transaction failed", name)
    return ran


async def _plus_users(ids) -> list:
    """The accounts among `ids` whose Plus is live, as the web checks it on every request."""
    if not ids:
        return []
    users = await db.user.find_many(where={"id": {"in": list(ids)}})
    return [u for u in users if effective_plan(u) != FREE]


async def _each(job: str, users, step) -> None:
    for user in users:
        try:
            await step(user)
        except Exception:  # noqa: BLE001 — the next account still runs
            log.exception("%s failed for user %s", job, user.id)


async def notifications_tick() -> None:
    users = await _plus_users(await notification_sync.timed_candidate_users(datetime.now(timezone.utc)))
    await _each("notifications-tick", users, lambda u: notification_sync.sync_for_user(u, full=False))


async def notifications_sweep() -> None:
    users = await _plus_users(await notification_sync.daily_candidate_users(datetime.now(timezone.utc)))
    await _each("notifications-sweep", users, lambda u: notification_sync.sync_for_user(u, full=True))


async def notifications_cleanup() -> None:
    await notification_sync.cleanup()


async def assistant_undo_cleanup() -> None:
    from app.os import assistant_snapshot

    await assistant_snapshot.clear_old(datetime.now(timezone.utc) - timedelta(hours=24))


async def entry_review_cleanup() -> None:
    from app import review

    await review.discard_stale()


async def finance_repeat() -> None:
    heads = await db.transaction.find_many(where={"rrule": {"not": None}, "date": {"lte": datetime.now(timezone.utc)}}, distinct=["userId"])
    await _each("finance-recurring-transactions", await _plus_users({t.userId for t in heads}), lambda u: transactions.materialize_due(u.id))


async def finance_autolog() -> None:
    # +1 day: accounts ahead of UTC are already "tomorrow"; the exact day is checked per account
    margin = datetime.now(timezone.utc) + timedelta(days=1)
    due = await db.recurringpayment.find_many(where={"status": "ACTIVE", "autoLogExpense": True, "nextDate": {"lte": margin}}, distinct=["userId"])
    await _each("finance-recurring-autolog", await _plus_users({p.userId for p in due}), lambda u: recurring_payments.auto_log_due(u.id))


async def plus_trial_reminders() -> None:
    from app.trial_reminders import send_trial_reminders

    await send_trial_reminders(datetime.now(timezone.utc))


async def refresh_market_rates() -> None:
    await market_rates.refresh()


@dataclass(frozen=True)
class Job:
    name: str
    due: Callable[[datetime], bool]
    run: Callable[[], Awaitable[None]]
    lane: str = "other"
    timeout: timedelta = timedelta(minutes=30)


JOBS = (
    Job("notifications-tick", lambda t: True, notifications_tick, lane="tick", timeout=timedelta(minutes=5)),
    Job("notifications-sweep", lambda t: t.minute % 15 == 0, notifications_sweep),
    Job("notifications-cleanup", lambda t: (t.hour, t.minute) == (3, 0), notifications_cleanup),
    Job("assistant-undo-cleanup", lambda t: (t.hour, t.minute) == (3, 5), assistant_undo_cleanup),
    Job("entry-review-cleanup", lambda t: (t.hour, t.minute) == (3, 5), entry_review_cleanup),
    Job("market-rates", lambda t: t.minute == 17 and t.hour % 6 == 0, refresh_market_rates),
    Job("finance-recurring-transactions", lambda t: t.minute == 0, finance_repeat),
    Job("finance-recurring-autolog", lambda t: t.minute == 0, finance_autolog),
    Job("plus-trial-reminders", lambda t: (t.hour, t.minute) == (9, 0), plus_trial_reminders),
)
_BY_NAME = {j.name: j for j in JOBS}


def due_jobs(now: datetime) -> list[str]:
    """The jobs that run in the minute `now` falls in (UTC)."""
    minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return [j.name for j in JOBS if j.due(minute)]


_lanes: dict[str, asyncio.Lock] = {}
_waiting: set[str] = set()  # queued or running here: a job is never queued twice


async def _run(job: Job) -> None:
    try:
        async with _lanes.setdefault(job.lane, asyncio.Lock()):
            await run_locked(job.name, job.run, job.timeout)
    finally:
        _waiting.discard(job.name)


async def _loop() -> None:
    while True:
        now = datetime.now(timezone.utc)
        # a second into the next minute, so a slow clock never runs a minute twice
        await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 1)
        for name in due_jobs(datetime.now(timezone.utc)):
            if name not in _waiting:
                _waiting.add(name)
                asyncio.ensure_future(_run(_BY_NAME[name]))


def start() -> asyncio.Task | None:
    """From the app's startup; None when VOURNAL_OS_JOBS=0."""
    if os.environ.get("VOURNAL_OS_JOBS", "").strip() == "0":
        log.info("vournal OS jobs switched off (VOURNAL_OS_JOBS=0)")
        return None
    return asyncio.ensure_future(_loop())
