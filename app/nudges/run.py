"""The nudge job (design 2026-11-18), run every minute by app/os/jobs.py.

For each person with a phone and nudges on: find the moment due in their phone's time zone, check
the switches, quiet hours and the daily cap, write the text (AI for Plus users who agreed, rules
otherwise), record it in NudgeLog *before* sending (the unique row stops a second worker), and
send it to each of their phones through Expo. A nudge no phone took is sent again, same text,
every 5 minutes while its window lasts. Dead tokens are deleted from the tickets, and from
the receipts checked once, 20 minutes to 24 hours later. NUDGES_ENABLED=0 stops everything;
NUDGES_AI=0 makes every nudge rule-based."""

import logging
import os
from time import monotonic as time_now
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from prisma.errors import UniqueViolationError

from app import push_expo
from app.db import db
from app.entitlements import FREE, effective_plan
from app.nudges.rules import Nudge, app_facts, free_text, life_facts, plus_fallback_text
from app.nudges.schedule import CAP_FREE, CAP_PLUS, allowed, could_be_due, due_moment, moments_for
from app.nudges.writer import WriterFailed, journal_summaries, write
from app.os.common.dates import valid_zone
from app.os.settings import normalize_prefs

log = logging.getLogger("vournal")

SWITCH = {"morning": "nudgePlan", "midday": "nudgeSuggestions", "evening": "nudgeCheckIns"}
RECENT_DAYS = 3
RECEIPTS_AFTER, RECEIPTS_UNTIL = timedelta(minutes=20), timedelta(hours=24)
RETRY_AFTER = timedelta(minutes=5)
LOG_DAYS, DEVICE_DAYS = 30, 60


def _day_value(day) -> datetime:
    return datetime.combine(day, time(0), tzinfo=timezone.utc)


async def _text(user, settings, moment, zone, today) -> tuple[Nudge | None, str]:
    app = await app_facts(user, zone, today)
    lang = "ar" if settings.language == "ar" else "en"
    if effective_plan(user) == FREE:
        return free_text(moment, app, lang), "rules"
    use_ai = settings.nudgeAiConsentAt is not None and os.environ.get("NUDGES_AI", "1") != "0"
    if moment == "midday" and not use_ai:
        return None, "rules"  # a midday nudge is AI-written or nothing: no dashboard read either
    life = await life_facts(user)
    fallback = plus_fallback_text(moment, app, life, lang)
    if moment == "midday" and not life["triggers"]:
        return None, "rules"
    if moment == "morning" and fallback is None:
        return None, "rules"  # an empty day: nothing to plan, and no AI call
    if not use_ai:
        return fallback, "rules"
    since = _day_value(today - timedelta(days=RECENT_DAYS))
    recent = [r.body for r in await db.nudgelog.find_many(where={"userId": user.id, "date": {"gte": since}}, order={"createdAt": "desc"}, take=9)]
    summaries = await journal_summaries(user, settings, zone, today) if moment == "evening" else []
    try:
        return await write(moment, app=app, life=life, recent=recent, summaries=summaries, language=settings.language), "ai"
    except WriterFailed:
        return fallback, "rules"


def _zone_of(devices: list) -> ZoneInfo:
    return ZoneInfo(valid_zone(max(devices, key=lambda d: d.lastSeenAt).timezone))


async def _send(user, devices: list, row, nudge: Nudge, client) -> None:
    data = {"kind": "nudge", "opens": nudge.opens, "prompt": nudge.prompt, "nudgeId": row.id, "userId": user.id}
    messages = [{"to": d.token, "title": nudge.title, "body": nudge.body, "sound": "default", "data": data} for d in devices]
    tickets = await push_expo.send(messages, client=client)
    ok = [f"{t['id']}|{d.token}" for d, t in zip(devices, tickets) if t["status"] == "ok"]
    dead = [d.token for d, t in zip(devices, tickets) if t.get("error") == "DeviceNotRegistered"]
    for t in tickets:
        if t["status"] != "ok" and t.get("error") != "DeviceNotRegistered":
            log.warning("nudge push failed for user %s: %s", user.id, t.get("error") or "request failed")
    if dead:
        await db.pushdevice.delete_many(where={"token": {"in": dead}})
    if ok:
        await db.nudgelog.update(where={"id": row.id}, data={"ticketIds": ok})


async def _retry(user, devices: list, row, now: datetime, client) -> bool:
    """A nudge no phone took (Expo down, a failed request) is sent again with the same text, every
    RETRY_AFTER while its window lasts: no second AI call. The claim moves createdAt on, so two
    workers never both resend it."""
    if row.ticketIds:
        return False  # delivered
    claimed = await db.nudgelog.update_many(where={"id": row.id, "createdAt": {"lte": now - RETRY_AFTER}}, data={"createdAt": now})
    if not claimed:
        return False
    await _send(user, devices, row, Nudge(row.title, row.body, row.opens, row.prompt), client)  # type: ignore[arg-type]
    return True


async def _for_user(user, devices: list, settings, now: datetime, client=None) -> bool:
    if settings is None:
        return False
    local = now.astimezone(_zone_of(devices))
    prefs = normalize_prefs(settings.notificationPrefs)
    if not prefs["nudges"]:
        return False
    today = local.date()
    moment = due_moment(local, prefs["nudgeMorningTime"])
    plus = effective_plan(user) != FREE
    if moment is None or not allowed(moment, local) or moment not in moments_for(plus) or not prefs[SWITCH[moment]]:
        return False
    sent = await db.nudgelog.find_first(where={"userId": user.id, "date": _day_value(today), "moment": moment})
    if sent:
        return await _retry(user, devices, sent, now, client)
    if await db.nudgelog.count(where={"userId": user.id, "date": _day_value(today)}) >= (CAP_PLUS if plus else CAP_FREE):
        return False
    nudge, source = await _text(user, settings, moment, local.tzinfo, today)
    if nudge is None:
        return False
    try:
        row = await db.nudgelog.create(data={"userId": user.id, "date": _day_value(today), "moment": moment, "title": nudge.title,
                                             "body": nudge.body, "opens": nudge.opens, "prompt": nudge.prompt, "source": source,
                                             "createdAt": now})  # the job's clock: the retry and receipt timings read it
    except UniqueViolationError:
        return False  # another worker got there first
    await _send(user, devices, row, nudge, client)
    return True


async def tick(now: datetime) -> int:
    """Sends the nudges due at `now` (UTC); returns how many people got one."""
    if os.environ.get("NUDGES_ENABLED", "1") == "0":
        return 0
    by_user: dict[str, list] = defaultdict(list)
    for d in await db.pushdevice.find_many():
        by_user[d.userId].append(d)
    # most minutes, for most people, nothing could be due: no settings read at all
    due = [uid for uid, devices in by_user.items() if could_be_due(now.astimezone(_zone_of(devices)))]
    sent = 0
    if due:
        settings_of = {s.userId: s for s in await db.usersettings.find_many(where={"userId": {"in": due}})}
        started = time_now()
        async with push_expo.new_client() as client:  # one connection pool for the whole tick
            for user in await db.user.find_many(where={"id": {"in": due}}):
                # the AI writer can take a while: each person's moment is read at the time it runs
                at = now + timedelta(seconds=time_now() - started)
                try:
                    sent += await _for_user(user, by_user[user.id], settings_of.get(user.id), at, client)
                except Exception:  # noqa: BLE001 — the next person still gets theirs
                    log.exception("nudge failed for user %s", user.id)
    try:
        await check_receipts(now)
    except Exception:  # noqa: BLE001
        log.exception("nudge receipts check failed")
    return sent


async def check_receipts(now: datetime) -> None:
    """Once per nudge, 20 minutes to 24 hours after it was sent: dead tokens are deleted."""
    rows = await db.nudgelog.find_many(where={"receiptsCheckedAt": None, "ticketIds": {"isEmpty": False},
                                              "createdAt": {"gte": now - RECEIPTS_UNTIL, "lte": now - RECEIPTS_AFTER}})
    if not rows:
        return
    token_of = {}
    for r in rows:
        for pair in r.ticketIds:
            ticket, _, token = pair.partition("|")
            token_of[ticket] = token
    found = await push_expo.receipts(list(token_of))
    if found is None:
        return  # Expo didn't answer: try again on a later tick, within the 24 hours receipts are kept
    dead = [token_of[t] for t, rec in found.items() if t in token_of and rec.get("error") == "DeviceNotRegistered"]
    for rec in found.values():
        if rec.get("status") != "ok" and rec.get("error") != "DeviceNotRegistered":
            log.warning("nudge push receipt error: %s", rec.get("error") or "unknown")
    if dead:
        await db.pushdevice.delete_many(where={"token": {"in": dead}})
    await db.nudgelog.update_many(where={"id": {"in": [r.id for r in rows]}}, data={"receiptsCheckedAt": now})


async def cleanup(now: datetime) -> None:
    await db.nudgelog.delete_many(where={"createdAt": {"lt": now - timedelta(days=LOG_DAYS)}})
    await db.pushdevice.delete_many(where={"lastSeenAt": {"lt": now - timedelta(days=DEVICE_DAYS)}})
