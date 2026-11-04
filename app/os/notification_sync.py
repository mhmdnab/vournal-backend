"""Making notifications (port of vournalOS NotificationsService's scheduler half): what is
due for one user becomes a notification once (deduplicated by `dedupeKey`), fired
reminders move on, and new notifications go out by web push.

Two passes: the timed one (reminders, task and event reminders, habits, the daily digest,
the wellness nudge) and the full one adding the daily checks (bills, debts, documents,
maintenance, people's dates, budgets). The jobs run them for every candidate account; a
list of notifications runs them lazily first (30 s / 10 min per user, per process).
The app's own to-dos never make a notification here: the phone already alerts for them.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from prisma.errors import UniqueViolationError

from app.db import db
from app.os import push
from app.os.calendar_utils import (
    ENTITY_LINKS,
    add_days_str,
    anniversary_str,
    date_only_str,
    date_only_value,
    diff_days_str,
    local_date_str,
    local_date_time,
    local_midnight,
    local_time_str,
    local_weekday,
    safe_tz,
)
from app.os.common.activity import log_activity
from app.os.common.currency import get_converter
from app.os.common.i18n import to_lang
from app.os.common.prayer import resolver
from app.os.event_occurrences import expand_event
from app.os.insight_text import js_round
from app.os.notification_texts import texts
from app.os.profile import ensure_os_profile
from app.os.reminder_schedule import advance_after, fire_at, is_all_day, next_occurrence
from app.os.settings import normalize_prefs

log = logging.getLogger("vournal.os")

LOOKBACK = timedelta(hours=48)  # missed task and event reminders are still delivered this long after
LAZY_TIMED, LAZY_FULL = timedelta(seconds=30), timedelta(minutes=10)
MAX_PUSH_PER_SYNC = 4
DAY = timedelta(days=1)

_last_timed: dict[str, datetime] = {}
_last_full: dict[str, datetime] = {}
_in_flight: dict[tuple[str, bool], asyncio.Task] = {}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _slice16(text: str, size: int) -> str:
    """String#slice(0, size) by UTF-16 units (a half emoji at the cut is dropped)."""
    return text.encode("utf-16-le", "surrogatepass")[: size * 2].decode("utf-16-le", errors="ignore")


class Ctx:
    def __init__(self, user, settings):
        self.user_id = user.id
        self.tz = safe_tz(settings.timezone)
        self.now = datetime.now(timezone.utc)
        self.today = local_date_str(self.now, self.tz)
        self.prefs = normalize_prefs(settings.notificationPrefs)
        self.time_format = settings.timeFormat
        self.week_starts_on = settings.weekStartsOn
        self.lang = to_lang(settings.language)
        self.T = texts(self.lang)
        self.sections = list(settings.enabledSections or [])

    def on(self, key: str) -> bool:
        return self.prefs.get(key) is not False

    def time(self, moment: datetime) -> str:
        return self.T.time(moment, self.tz, self.time_format)

    def day_title(self, day: str) -> str:
        """"Today" / "Tomorrow" / "Thu 8 Oct" (the start of a phrase)."""
        days = diff_days_str(self.today, day)
        if days == 0:
            return self.T.t("todayTitle")
        if days == 1:
            return self.T.t("tomorrowTitle")
        if days == -1:
            return self.T.t("yesterdayTitle")
        return self.T.date(day)

    def starts_in(self, start: datetime) -> str:
        mins = js_round((start - self.now).total_seconds() / 60)
        if mins <= 0:
            return self.T.t("startingNow" if mins > -15 else "started")
        if mins < 60:
            return self.T.n("inMinutes", mins)
        hours, minutes = mins // 60, mins % 60
        if hours < 24:
            return self.T.t("inHoursMinutes", hours=hours, minutes=minutes) if minutes else self.T.n("inHours", hours)
        return self.T.n("inDaysTitle", js_round(hours / 24))


def _candidate(key, title, body, link, entity_type, entity_id) -> dict:
    return {"dedupeKey": key, "title": title, "body": body, "link": link, "entityType": entity_type, "entityId": entity_id}


# ----- timed checks --------------------------------------------------------------------


# NestJS's due filter, in SQL: lastFiredAt < remindAt compares two columns, which the Prisma
# client can't. Fired one-off reminders nobody ticked off stay in the table, so filtering after
# a 500-row page could leave a new reminder out of it.
_DUE_REMINDER = ('''NOT "isDone" AND ("snoozedUntil" <= $1::timestamp OR ("snoozedUntil" IS NULL AND "remindAt" <= $1::timestamp'''
                 ''' AND ("lastFiredAt" IS NULL OR "lastFiredAt" < "remindAt")))''')


def _sql_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")


def _reminder_due(r, now: datetime) -> bool:
    """A snooze that has come, or a reminder whose time has come and that hasn't fired for it."""
    if r.snoozedUntil:
        return r.snoozedUntil <= now
    return r.remindAt <= now and (r.lastFiredAt is None or r.lastFiredAt < r.remindAt)


async def _reminders(ctx: Ctx):
    """Due reminders, and the changes that mark them fired (applied after the notifications)."""
    resolve = await resolver(ctx.user_id)
    due = await db.query_raw(f'SELECT id FROM "Reminder" WHERE "userId" = $2 AND {_DUE_REMINDER} LIMIT 500', _sql_time(ctx.now), ctx.user_id)
    rows = await db.reminder.find_many(where={"id": {"in": [r["id"] for r in due]}}) if due else []
    T, out, updates = ctx.T, [], []
    for r in rows:
        if not _reminder_due(r, ctx.now):
            continue
        fire = fire_at(r, ctx.tz)
        if fire > ctx.now:
            continue  # a date-based reminder before 09:00
        all_day = is_all_day(r, ctx.tz)
        key = f"reminder:{r.id}:snooze:{_iso(r.snoozedUntil)}" if r.snoozedUntil else f"reminder:{r.id}:{_iso(r.remindAt)}"
        parts = []
        if r.isDeadline:
            parts.append(T.t("deadlineToday") if all_day else T.t("deadlineAt", time=ctx.time(r.remindAt)))
        if r.notes:
            parts.append(r.notes)
        body = " — ".join(parts) or (T.t("reminderToday") if all_day else T.t("reminderAt", time=ctx.time(fire)))
        out.append(_candidate(key, r.title, body, ENTITY_LINKS["REMINDER"](r.id), "REMINDER", r.id))
        data = {"lastFiredAt": ctx.now, "snoozedUntil": None}
        if r.rrule and r.remindAt <= ctx.now:
            nxt = next_occurrence(r, advance_after(r, ctx.now), ctx.tz, resolve)
            if nxt:
                data["remindAt"] = nxt
        # only if nobody changed it meanwhile (a job against a lazy sync)
        updates.append(({"id": r.id, "userId": ctx.user_id, "remindAt": r.remindAt, "snoozedUntil": r.snoozedUntil}, data, r.id, r.title))

    async def apply() -> None:
        for where, data, id, title in updates:
            if await db.reminder.update_many(where=where, data=data):
                await log_activity(ctx.user_id, "reminder.fired", entity_id=id, title=title)

    return out, apply


async def _tasks(ctx: Ctx) -> list[dict]:
    rows = await db.task.find_many(where={"userId": ctx.user_id, "status": {"not": "COMPLETED"},
                                          "reminderAt": {"gte": ctx.now - LOOKBACK, "lte": ctx.now}}, take=500)
    out, T = [], ctx.T
    for t in rows:
        body = T.t("taskReminder")
        if t.dueAt:
            day = local_date_str(t.dueAt, ctx.tz)
            days = diff_days_str(ctx.today, day)
            due = (T.t("dueToday") if days == 0 else T.t("dueTomorrow") if days == 1 else T.t("dueYesterday") if days == -1
                   else T.t("dueOn", date=T.date(day)))
            body = T.t("dueWithTime", due=due, time=ctx.time(t.dueAt)) if t.hasDueTime else due
        out.append(_candidate(f"task:{t.id}:reminder:{_iso(t.reminderAt)}", t.title, body, ENTITY_LINKS["TASK"](t.id), "TASK", t.id))
    return out


async def _events(ctx: Ctx) -> list[dict]:
    rows = await db.event.find_many(where={
        "userId": ctx.user_id, "reminderMinutes": {"not": None}, "startAt": {"lte": ctx.now + timedelta(days=29)},
        "OR": [{"rrule": None, "OR": [{"startAt": {"gte": ctx.now - LOOKBACK}}, {"endAt": {"gte": ctx.now}}]},
               {"rrule": {"not": None}, "OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": ctx.now}}]}],
    }, take=1000)
    resolve = await resolver(ctx.user_id)
    out, T = [], ctx.T
    for ev in rows:
        lead = timedelta(minutes=ev.reminderMinutes or 0)
        for o in expand_event(ev, ctx.now - LOOKBACK, ctx.now + lead + DAY, ctx.tz, resolve):
            base = local_date_time(o.start_date, "09:00", ctx.tz) if ev.allDay else o.start
            fire = base - lead
            if fire > ctx.now or fire < ctx.now - LOOKBACK:
                continue
            # skip what's already over (or started more than 15 minutes ago, without an end)
            if ev.allDay:
                relevant = o.end_date >= ctx.today
            elif ev.endAt:
                relevant = o.end >= ctx.now
            else:
                relevant = o.start >= ctx.now - timedelta(minutes=15)
            if not relevant:
                continue
            day = o.start_date if ev.allDay else local_date_str(o.start, ctx.tz)
            when = (T.t("eventAllDay", day=ctx.day_title(day)) if ev.allDay
                    else T.t("eventTimed", startsIn=ctx.starts_in(o.start), day=ctx.day_title(day), time=ctx.time(o.start)))
            out.append(_candidate(f"event:{ev.id}:{_iso(o.start)}:r{ev.reminderMinutes}", ev.title,
                                  T.t("withLocation", when=when, location=ev.location) if ev.location else when,
                                  f"/calendar?id={ev.id}&date={day}", "EVENT", ev.id))
    return out


async def _health(ctx: Ctx) -> list[dict]:
    """Nothing logged today by the reminder time → one nudge a day."""
    if "health" not in ctx.sections:
        return []
    p = await db.healthprofile.find_unique(where={"userId": ctx.user_id})
    if not p or not p.logReminderTime or local_time_str(ctx.now, ctx.tz) < p.logReminderTime:
        return []
    if await db.foodlog.count(where={"userId": ctx.user_id, "date": date_only_value(ctx.today)}):
        return []
    return [_candidate(f"health:log:{ctx.today}", ctx.T.t("healthLogTitle"), ctx.T.t("healthLogBody"), "/health", None, None)]


async def _digest(ctx: Ctx) -> list[dict]:
    """One summary of today, the first sync after the user's digest time; none on an empty day."""
    at = ctx.prefs.get("dailyDigestTime")
    if not at or local_time_str(ctx.now, ctx.tz) < at:
        return []
    key = f"digest:{ctx.today}"
    if await db.notification.count(where={"userId": ctx.user_id, "dedupeKey": key}):
        return []
    start, end = local_midnight(ctx.today, ctx.tz), local_midnight(add_days_str(ctx.today, 1), ctx.tz)
    open_tasks = {"userId": ctx.user_id, "status": {"not": "COMPLETED"}, "parentId": None}
    tasks_today = await db.task.count(where={**open_tasks, "dueAt": {"gte": start, "lt": end}})
    overdue = await db.task.count(where={**open_tasks, "dueAt": {"lt": start}})
    events = await db.event.find_many(where={"userId": ctx.user_id, "startAt": {"lt": end}, "OR": [
        {"rrule": None, "OR": [{"startAt": {"gte": start}}, {"endAt": {"gte": start}}]},
        {"rrule": {"not": None}, "OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": start}}]}]}, take=1000)
    bills = await db.recurringpayment.count(where={"userId": ctx.user_id, "status": "ACTIVE", "nextDate": {"lte": date_only_value(add_days_str(ctx.today, 3))}})
    reminders = await db.reminder.count(where={"userId": ctx.user_id, "isDone": False, "remindAt": {"gte": start, "lt": end}})
    resolve = await resolver(ctx.user_id)
    events_today = sum(1 for ev in events if expand_event(ev, start, end - timedelta(milliseconds=1), ctx.tz, resolve))
    T = ctx.T
    parts = [p for p in (T.n("digestTasks", tasks_today) if tasks_today else None, T.n("digestEvents", events_today) if events_today else None,
                         T.n("digestReminders", reminders) if reminders else None, T.n("digestBills", bills) if bills else None,
                         T.n("digestOverdue", overdue) if overdue else None) if p]
    if not parts:
        return []
    return [_candidate(key, T.t("digestTitle"), T.join(parts), "/dashboard", None, None)]


async def _habits(ctx: Ctx) -> list[dict]:
    habits = await db.habit.find_many(where={"userId": ctx.user_id, "isArchived": False, "reminderTime": {"not": None}}, take=500)
    now_hhmm = local_time_str(ctx.now, ctx.tz)
    due = [h for h in habits if h.reminderTime <= now_hhmm and date_only_str(h.startDate) <= ctx.today]
    if not due:
        return []
    weekday = local_weekday(ctx.now, ctx.tz)
    week_start = add_days_str(ctx.today, -((weekday - (ctx.week_starts_on if ctx.week_starts_on is not None else 1) + 7) % 7))
    logs = await db.habitlog.find_many(where={"userId": ctx.user_id, "habitId": {"in": [h.id for h in due]},
                                              "date": {"gte": date_only_value(week_start), "lte": date_only_value(ctx.today)}})
    out = []
    for h in due:
        mine = [l for l in logs if l.habitId == h.id]
        today_count = sum(l.count for l in mine if date_only_str(l.date) == ctx.today)
        if today_count >= h.targetPerDay:
            continue
        if h.frequency == "CUSTOM" and weekday not in h.daysOfWeek:
            continue
        if h.frequency == "WEEKLY" and sum(1 for l in mine if l.count >= h.targetPerDay) >= (h.timesPerWeek if h.timesPerWeek is not None else 1):
            continue
        progress = ctx.T.ltr(f"({today_count}/{h.targetPerDay}{f' {h.unit}' if h.unit else ''})") if h.targetPerDay > 1 else None
        out.append(_candidate(f"habit:{h.id}:{ctx.today}", h.name, ctx.T.t("habitProgress", progress=progress) if progress else ctx.T.t("habit"),
                              ENTITY_LINKS["HABIT"](h.id), "HABIT", h.id))
    return out


# ----- daily checks --------------------------------------------------------------------


def _three(key: str, day: str, days: int, base: dict) -> list[dict]:
    """A heads-up before the day, one on the day, and one once overdue."""
    suffix = "" if days > 0 else ":due" if days == 0 else ":overdue"
    return [{"dedupeKey": f"{key}:{day}{suffix}", **base}]


async def _bills(ctx: Ctx) -> list[dict]:
    rows = await db.recurringpayment.find_many(where={"userId": ctx.user_id, "status": "ACTIVE", "reminderDaysBefore": {"not": None},
                                                      "nextDate": {"gte": date_only_value(add_days_str(ctx.today, -7)), "lte": date_only_value(add_days_str(ctx.today, 60))}},
                                               take=500)
    out, T = [], ctx.T
    for p in rows:
        day = date_only_str(p.nextDate)
        days = diff_days_str(ctx.today, day)
        if days > (p.reminderDaysBefore or 0):
            continue
        sub = p.kind == "SUBSCRIPTION"
        title = T.t("billOverdue", name=p.name) if days < 0 else T.t("subscriptionRenews" if sub else "billDue", name=p.name, when=T.when(days))
        base = {"title": title, "body": T.t("amountOnDate", amount=T.money(float(p.amount), p.currency), date=T.date(day)),
                "link": f"/finance/subscriptions?id={p.id}" if sub else ENTITY_LINKS["RECURRING_PAYMENT"](p.id),
                "entityType": "RECURRING_PAYMENT", "entityId": p.id}
        out += _three(f"recurring:{p.id}", day, days, base)
    return out


async def _debts(ctx: Ctx) -> list[dict]:
    """Open debts with a due date: ahead of it, on the day, and once overdue."""
    rows = await db.debt.find_many(where={"userId": ctx.user_id, "status": "OPEN", "reminderDaysBefore": {"not": None},
                                          "dueDate": {"gte": date_only_value(add_days_str(ctx.today, -30)), "lte": date_only_value(add_days_str(ctx.today, 60))}},
                                   include={"person": True, "payments": True}, take=500)
    out, T = [], ctx.T
    for d in rows:
        paid = 0.0
        for p in d.payments or []:
            paid = paid + float(p.amount)
        left = float(d.principal) - paid
        if left <= 0.005:
            continue
        day = date_only_str(d.dueDate)
        days = diff_days_str(ctx.today, day)
        if days > (d.reminderDaysBefore or 0):
            continue
        who = (d.person.name if d.person else None) or (d.counterparty if d.counterparty is not None else d.title)
        owe = d.direction == "I_OWE"
        title = (T.t("debtPayOverdue" if owe else "debtCollectOverdue", name=d.title, who=who) if days < 0
                 else T.t("debtPayDue" if owe else "debtCollectDue", name=d.title, who=who, when=T.when(days)))
        base = {"title": title, "body": T.t("debtLeft", amount=T.money(left, d.currency), date=T.date(day)), "link": ENTITY_LINKS["DEBT"](d.id),
                "entityType": "DEBT", "entityId": d.id}
        out += _three(f"debt:{d.id}", day, days, base)
    return out


async def _documents(ctx: Ctx) -> list[dict]:
    rows = await db.document.find_many(where={"userId": ctx.user_id, "reminderDaysBefore": {"not": None},
                                              "expiryDate": {"gte": date_only_value(add_days_str(ctx.today, -30)), "lte": date_only_value(add_days_str(ctx.today, 365))}},
                                       take=500)
    out, T = [], ctx.T
    for d in rows:
        day = date_only_str(d.expiryDate)
        days = diff_days_str(ctx.today, day)
        base = {"link": ENTITY_LINKS["DOCUMENT"](d.id), "entityType": "DOCUMENT", "entityId": d.id}
        if days < 0:
            out.append({"dedupeKey": f"doc:{d.id}:expired:{day}", "title": T.t("documentExpired", name=d.name),
                        "body": T.t("documentExpiredOn", date=T.date(day)), **base})
        elif days <= (d.reminderDaysBefore or 0):
            out.append({"dedupeKey": f"doc:{d.id}:expiry:{day}",
                        "title": T.t("documentExpiresToday", name=d.name) if days == 0 else T.n("documentExpiresIn", days, name=d.name),
                        "body": T.t("documentExpiryDate", date=T.date(day)), **base})
    return out


async def _maintenance(ctx: Ctx) -> list[dict]:
    rows = await db.maintenanceitem.find_many(where={"userId": ctx.user_id, "isActive": True, "reminderDaysBefore": {"not": None},
                                                     "nextDueAt": {"gte": date_only_value(add_days_str(ctx.today, -30)), "lte": date_only_value(add_days_str(ctx.today, 60))}},
                                              take=500)
    out, T = [], ctx.T
    for m in rows:
        day = date_only_str(m.nextDueAt)
        days = diff_days_str(ctx.today, day)
        if days > (m.reminderDaysBefore or 0):
            continue
        title = (T.t("maintenanceOverdue", title=m.title) if days < 0 else T.t("maintenanceDueToday", title=m.title) if days == 0
                 else T.t("maintenanceDue", title=m.title, when=T.when(days)))
        out.append(_candidate(f"maint:{m.id}:{day}{':overdue' if days < 0 else ''}", title, T.t("maintenanceBody", date=T.date(day)),
                              ENTITY_LINKS["MAINTENANCE"](m.id), "MAINTENANCE", m.id))
    return out


async def _people(ctx: Ctx) -> list[dict]:
    people = await db.person.find_many(where={"userId": ctx.user_id, "reminderDaysBefore": {"not": None},
                                              "OR": [{"birthday": {"not": None}}, {"anniversary": {"not": None}}]}, take=2000)
    important = await db.importantdate.find_many(where={"userId": ctx.user_id, "reminderDaysBefore": {"not": None}}, include={"person": True}, take=2000)
    year = int(ctx.today[:4])

    def next_yearly(value) -> str:
        this_year = anniversary_str(value, year)
        return this_year if this_year >= ctx.today else anniversary_str(value, year + 1)

    out, T = [], ctx.T

    def push_date(key, day, lead, title, body, link, entity_type, entity_id) -> None:
        days = diff_days_str(ctx.today, day)
        if days < 0 or days > lead:
            return
        out.append(_candidate(f"{key}:{day}" if days > 0 else f"{key}:{day}:today", title(days), body, link, entity_type, entity_id))

    for p in people:
        lead = p.reminderDaysBefore or 0
        if p.birthday:
            day = next_yearly(p.birthday)
            age = int(day[:4]) - p.birthday.year if p.birthdayHasYear else None
            push_date(f"bday:{p.id}", day, lead,
                      lambda days, p=p: T.t("birthdayToday", name=p.name) if days == 0 else T.t("birthdayUpcoming", name=p.name, when=T.when(days)),
                      T.t("turnsOnDate", age=age, date=T.date(day)) if age and age > 0 else T.date(day), ENTITY_LINKS["PERSON"](p.id), "PERSON", p.id)
        if p.anniversary:
            day = next_yearly(p.anniversary)
            years = int(day[:4]) - p.anniversary.year
            push_date(f"anniv:{p.id}", day, lead,
                      lambda days, p=p: T.t("anniversaryToday", name=p.name) if days == 0 else T.t("anniversaryUpcoming", name=p.name, when=T.when(days)),
                      T.t("yearsOnDate", years=T.n("years", years), date=T.date(day)) if years > 0 else T.date(day),
                      ENTITY_LINKS["PERSON"](p.id), "PERSON", p.id)
    for d in important:
        day = next_yearly(d.date) if d.repeatsYearly else date_only_str(d.date)
        label = T.t("importantDateWithPerson", title=d.title, person=d.person.name) if d.person else d.title
        push_date(f"idate:{d.id}", day, d.reminderDaysBefore or 0,
                  lambda days, label=label: T.t("importantDateToday", label=label) if days == 0 else T.t("importantDateUpcoming", label=label, when=T.when(days)),
                  T.date(day), ENTITY_LINKS["PERSON"](d.personId) if d.personId else "/people", "IMPORTANT_DATE", d.id)
    return out


async def _budgets(ctx: Ctx) -> list[dict]:
    budgets = await db.budget.find_many(where={"userId": ctx.user_id, "isActive": True}, include={"category": True}, take=200)
    if not budgets:
        return []
    month, year = ctx.today[:7], ctx.today[:4]
    month_start, year_start = local_midnight(f"{month}-01", ctx.tz), local_midnight(f"{year}-01-01", ctx.tz)
    yearly = any(b.period == "YEARLY" for b in budgets)
    category_ids = [b.categoryId for b in budgets if b.categoryId]
    children = await db.category.find_many(where={"userId": ctx.user_id, "parentId": {"in": category_ids}}) if category_ids else []
    txs = await db.transaction.find_many(where={"userId": ctx.user_id, "type": "EXPENSE",
                                                "date": {"gte": year_start if yearly else month_start, "lte": ctx.now}})
    fx = await get_converter(ctx.user_id)
    out, T = [], ctx.T
    for b in budgets:
        limit = float(b.amount)
        if not limit > 0:
            continue
        cats = {b.categoryId, *(c.id for c in children if c.parentId == b.categoryId)} if b.categoryId else None
        start = year_start if b.period == "YEARLY" else month_start
        spent = 0.0
        for t in txs:
            if t.date < start or (cats is not None and (not t.categoryId or t.categoryId not in cats)):
                continue
            spent += fx.convert(float(t.amount), t.currency, b.currency)
        pct = js_round((spent / limit) * 100)
        period = year if b.period == "YEARLY" else month
        name = b.name or (b.category.name if b.category else None) or T.t("budgetOverall")
        percent = T.ltr(f"{pct}%")
        base = {"body": T.t("budgetBodyYear" if b.period == "YEARLY" else "budgetBodyMonth", spent=T.money(spent, b.currency),
                            limit=T.money(limit, b.currency), percent=percent),
                "link": "/finance/budgets", "entityType": "BUDGET", "entityId": b.id}
        if pct >= 100:
            out.append({"dedupeKey": f"budget:{b.id}:{period}:over", "title": T.t("budgetOver", name=name), **base})
        elif pct >= b.alertThreshold:
            out.append({"dedupeKey": f"budget:{b.id}:{period}:warn", "title": T.t("budgetWarn", name=name, percent=percent), **base})
    return out


# ----- the sync ------------------------------------------------------------------------


async def _persist(ctx: Ctx, candidates: list[dict]) -> list:
    """Stores the candidates that don't exist yet; returns the new rows."""
    unique: dict[str, dict] = {}
    for c in candidates:
        unique.setdefault(c["dedupeKey"], c)
    if not unique:
        return []
    for row in await db.notification.find_many(where={"userId": ctx.user_id, "dedupeKey": {"in": list(unique)}}):
        unique.pop(row.dedupeKey, None)
    read_at = ctx.now if ctx.prefs.get("inApp") is False else None  # in-app off: no badge, still pushed
    created = []
    for c in unique.values():
        try:
            created.append(await db.notification.create(data={
                "userId": ctx.user_id, "dedupeKey": c["dedupeKey"], "title": _slice16(c["title"], 200),
                "body": _slice16(c["body"], 1000) if c["body"] else None, "link": c["link"], "entityType": c["entityType"],
                "entityId": c["entityId"], "readAt": read_at,
            }))
        except UniqueViolationError:
            pass  # made by a concurrent sync a moment ago
    return created


async def _push_created(ctx: Ctx, created: list) -> None:
    T = ctx.T

    def payload(n) -> dict:
        return {"title": n.title, "body": n.body, "link": n.link, "tag": n.id, "id": n.id}

    if len(created) <= MAX_PUSH_PER_SYNC:
        payloads = [payload(n) for n in created]
    else:
        payloads = [payload(n) for n in created[: MAX_PUSH_PER_SYNC - 1]] + [{
            "title": T.n("moreNotifications", len(created) - (MAX_PUSH_PER_SYNC - 1)),
            "body": T.join([n.title for n in created[MAX_PUSH_PER_SYNC - 1:MAX_PUSH_PER_SYNC + 3]]), "link": "/dashboard", "tag": "life-os-summary",
        }]
    try:
        if await push.send_to_user(ctx.user_id, payloads) > 0:
            await db.notification.update_many(where={"userId": ctx.user_id, "id": {"in": [n.id for n in created]}},
                                              data={"pushedAt": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        log.warning("push failed for user %s: %s", ctx.user_id, exc)


async def _do_sync(user, full: bool) -> int:
    ctx = Ctx(user, await ensure_os_profile(user))
    reminders, apply_reminders = await _reminders(ctx)
    candidates = list(reminders)
    for check, pref in ((_tasks, "taskReminders"), (_events, "eventReminders"), (_habits, "habits"), (_digest, None), (_health, "health")):
        if pref is None or ctx.on(pref):
            candidates += await check(ctx)
    if full:
        for check, pref in ((_bills, "bills"), (_debts, "bills"), (_documents, "documents"), (_maintenance, "maintenance"),
                            (_people, "birthdays"), (_budgets, "budgets")):
            if ctx.on(pref):
                candidates += await check(ctx)
    created = await _persist(ctx, candidates)
    await apply_reminders()
    _last_timed[user.id] = datetime.now(timezone.utc)
    if full:
        _last_full[user.id] = _last_timed[user.id]
    if created and ctx.prefs.get("push") is not False and push.is_enabled():
        await _push_created(ctx, created)
    return len(created)


async def sync_for_user(user, full: bool = True) -> int:
    """Makes what's due for one user (idempotent); a sync already running is joined."""
    key = (user.id, full)
    running = _in_flight.get(key)
    if running and not running.done():
        return await running
    task = asyncio.ensure_future(_do_sync(user, full))
    _in_flight[key] = task
    try:
        return await task
    finally:
        if _in_flight.get(key) is task:
            del _in_flight[key]


async def lazy_sync(user) -> None:
    """Before a list: timed checks at most every 30 s, full ones every 10 min (the jobs may have slept)."""
    now = datetime.now(timezone.utc)
    need_full = now - _last_full.get(user.id, datetime.min.replace(tzinfo=timezone.utc)) >= LAZY_FULL
    need_timed = now - _last_timed.get(user.id, datetime.min.replace(tzinfo=timezone.utc)) >= LAZY_TIMED
    if not need_full and not need_timed:
        return
    try:
        await sync_for_user(user, need_full)
    except Exception:  # noqa: BLE001 — a failed sync never fails the list
        log.exception("lazy sync failed for user %s", user.id)


# ----- the accounts the jobs look at -----------------------------------------------------


async def timed_candidate_users(now: datetime) -> set[str]:
    ids: set[str] = set()
    ids |= {r["userId"] for r in await db.query_raw(f'SELECT DISTINCT "userId" FROM "Reminder" WHERE {_DUE_REMINDER}', _sql_time(now))}
    for rows in (
        await db.task.find_many(where={"reminderAt": {"gte": now - timedelta(minutes=10), "lte": now}, "status": {"not": "COMPLETED"}}, distinct=["userId"]),
        await db.event.find_many(where={"reminderMinutes": {"not": None}, "OR": [
            {"rrule": None, "startAt": {"gte": now - DAY, "lte": now + timedelta(days=29)}},
            {"rrule": {"not": None}, "OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": now}}]}]}, distinct=["userId"]),
        await db.habit.find_many(where={"isArchived": False, "reminderTime": {"not": None}}, distinct=["userId"]),
        await db.healthprofile.find_many(where={"logReminderTime": {"not": None}}),
    ):
        ids |= {r.userId for r in rows}
    # accounts with a daily digest set (the digest itself is made once a day)
    ids |= {s.userId for s in await db.usersettings.find_many() if isinstance(s.notificationPrefs, dict) and s.notificationPrefs.get("dailyDigestTime")}
    return ids


async def daily_candidate_users(now: datetime) -> set[str]:
    today = now.astimezone(timezone.utc).date().isoformat()

    def d(n: int):
        return date_only_value(add_days_str(today, n))

    ids: set[str] = set()
    for rows in (
        await db.recurringpayment.find_many(where={"status": "ACTIVE", "reminderDaysBefore": {"not": None}, "nextDate": {"gte": d(-8), "lte": d(61)}}, distinct=["userId"]),
        await db.document.find_many(where={"reminderDaysBefore": {"not": None}, "expiryDate": {"gte": d(-31), "lte": d(366)}}, distinct=["userId"]),
        await db.maintenanceitem.find_many(where={"isActive": True, "reminderDaysBefore": {"not": None}, "nextDueAt": {"gte": d(-31), "lte": d(61)}}, distinct=["userId"]),
        await db.person.find_many(where={"reminderDaysBefore": {"not": None}, "OR": [{"birthday": {"not": None}}, {"anniversary": {"not": None}}]}, distinct=["userId"]),
        await db.importantdate.find_many(where={"reminderDaysBefore": {"not": None}}, distinct=["userId"]),
        await db.budget.find_many(where={"isActive": True}, distinct=["userId"]),
    ):
        ids |= {r.userId for r in rows}
    return ids


async def cleanup() -> None:
    """Read notifications older than 90 days go."""
    await db.notification.delete_many(where={"readAt": {"not": None}, "createdAt": {"lt": datetime.now(timezone.utc) - timedelta(days=90)}})
