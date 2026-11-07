"""Read-only aggregations shared by the dashboard, reports, insights and search (port of
vournalOS LifeDataService). Every query is scoped by the user; amounts are converted to
the user's default currency; days are the user's local days ("YYYY-MM-DD" keys)."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.db import db
from app.os.common import dates, rrule
from app.os.common.collation import locale_key
from app.os.common.currency import Converter, get_converter, to_number
from app.os.common.i18n import to_lang
from app.os.insight_text import js_round, shared
from app.os.insight_time import add_days_key, date_only_key, day_key, diff_days_key, key_to_date, round2
from app.os.profile import ensure_os_profile

DAY = timedelta(days=1)


@dataclass
class LifeContext:
    user_id: str
    settings: object
    fx: Converter
    tz: str
    week_starts_on: int
    now: datetime
    today: str
    sections: set[str] = field(default_factory=set)
    lang: str = "en"


def iso_ms(moment: datetime) -> str:
    """Date#toISOString()."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def context(user) -> LifeContext:
    settings = await ensure_os_profile(user)
    tz = settings.timezone or "UTC"
    now = datetime.now(timezone.utc)
    return LifeContext(user.id, settings, await get_converter(user.id), tz, settings.weekStartsOn, now, day_key(now, tz),
                       set(settings.enabledSections), to_lang(settings.language))


def start_of_day_key(key: str, tz: str) -> datetime:
    """00:00 of a day key in the user's zone."""
    return dates.start_of_day(key_to_date(key) + timedelta(hours=12), tz)


def end_of_day_key(key: str, tz: str) -> datetime:
    return dates.end_of_day(key_to_date(key) + timedelta(hours=12), tz)


def overdue_task_where(user_id: str, now: datetime, start_of_today: datetime) -> dict:
    """Not completed, and due before today or earlier today with a time (an all-day task
    due today isn't overdue yet)."""
    return {"userId": user_id, "status": {"not": "COMPLETED"},
            "OR": [{"dueAt": {"lt": start_of_today}}, {"hasDueTime": True, "dueAt": {"lt": now}}]}


def _sum(group: dict, column: str):
    return (group.get("_sum") or {}).get(column)


# ----- finance -------------------------------------------------------------------------


async def account_balances(ctx: LifeContext, include_archived: bool = False) -> list[dict]:
    uid = ctx.user_id
    accounts = await db.account.find_many(where={"userId": uid, **({} if include_archived else {"isArchived": False})},
                                          order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    # A transaction's amount is in its own currency, which can differ from its account's (an LBP
    # expense paid from a USD account), so sums are grouped by currency and converted into the
    # account's currency before they touch its balance.
    own = await db.transaction.group_by(by=["accountId", "type", "currency"], where={"userId": uid, "accountId": {"not": None}},
                                        sum={"amount": True})
    to_amount = await db.transaction.group_by(by=["toAccountId"], where={"userId": uid, "type": "TRANSFER", "toAccountId": {"not": None},
                                                                          "toAmount": {"not": None}}, sum={"toAmount": True})
    to_same = await db.transaction.group_by(by=["toAccountId", "currency"], where={"userId": uid, "type": "TRANSFER", "toAccountId": {"not": None},
                                                                                   "toAmount": None}, sum={"amount": True})
    currency_of = {a.id: a.currency for a in accounts}
    delta: dict[str, float] = {}

    def add(account_id, n: float) -> None:
        if account_id:
            delta[account_id] = delta.get(account_id, 0) + n

    def in_account_currency(account_id, amount, currency) -> float:
        target = currency_of.get(account_id)
        return ctx.fx.convert(amount, currency, target) if target and currency else to_number(amount)

    for g in own:
        amount = in_account_currency(g["accountId"], _sum(g, "amount"), g["currency"])
        add(g["accountId"], amount if g["type"] == "INCOME" else -amount)
    for g in to_amount:
        add(g["toAccountId"], to_number(_sum(g, "toAmount")))
    for g in to_same:
        add(g["toAccountId"], in_account_currency(g["toAccountId"], _sum(g, "amount"), g["currency"]))
    out = []
    for a in accounts:
        balance = round2(to_number(a.initialBalance) + delta.get(a.id, 0))
        out.append({"id": a.id, "name": a.name, "type": a.type, "currency": a.currency, "color": a.color, "icon": a.icon,
                    "includeInTotal": a.includeInTotal, "balance": balance, "balanceDefault": ctx.fx.to_default(balance, a.currency)})
    return out


def net_worth(accounts: list[dict]) -> float:
    total = 0.0
    for a in accounts:
        if a["includeInTotal"]:
            total = total + a["balanceDefault"]
    return round2(total)


async def totals(ctx: LifeContext, start: datetime, end: datetime) -> dict:
    """Income, expenses and savings in [start, end] (transfers excluded)."""
    groups = await db.transaction.group_by(by=["type", "currency"], where={"userId": ctx.user_id, "type": {"in": ["INCOME", "EXPENSE"]},
                                                                            "date": {"gte": start, "lte": end}}, sum={"amount": True})
    income = expenses = 0.0
    for g in groups:
        v = ctx.fx.to_default(_sum(g, "amount"), g["currency"])
        if g["type"] == "INCOME":
            income += v
        else:
            expenses += v
    return {"income": round2(income), "expenses": round2(expenses), "savings": round2(income - expenses)}


async def category_map(user_id: str) -> dict:
    return {c.id: c for c in await db.category.find_many(where={"userId": user_id})}


async def spending_by_category(ctx: LifeContext, start: datetime, end: datetime, kind: str = "EXPENSE") -> list[dict]:
    """Totals per top-level category, largest first."""
    groups = await db.transaction.group_by(by=["categoryId", "currency"], where={"userId": ctx.user_id, "type": kind,
                                                                                  "date": {"gte": start, "lte": end}},
                                           sum={"amount": True}, count={"_all": True})
    categories = await category_map(ctx.user_id)
    by_cat: dict[str, dict] = {}
    for g in groups:
        cat = categories.get(g["categoryId"]) if g["categoryId"] else None
        top = (categories.get(cat.parentId) or cat) if cat and cat.parentId else cat
        key = top.id if top else "none"
        cur = by_cat.setdefault(key, {"categoryId": top.id if top else None, "name": top.name if top else shared(ctx.lang, "uncategorized"),
                                      "color": top.color if top else None, "icon": top.icon if top else None, "amount": 0.0, "count": 0})
        cur["amount"] += ctx.fx.to_default(_sum(g, "amount"), g["currency"])
        cur["count"] += g["_count"]["_all"]
    rows = [{**c, "amount": round2(c["amount"])} for c in by_cat.values()]
    return sorted((c for c in rows if c["amount"] > 0), key=lambda c: -c["amount"])


async def transactions_in_range(ctx: LifeContext, start: datetime, end: datetime):
    return await db.transaction.find_many(where={"userId": ctx.user_id, "type": {"in": ["INCOME", "EXPENSE"]}, "date": {"gte": start, "lte": end}},
                                          order={"date": "asc"})


async def budgets(ctx: LifeContext) -> list[dict]:
    uid = ctx.user_id
    month = (dates.start_of_month(ctx.now, ctx.tz), dates.end_of_month(ctx.now, ctx.tz))
    year = (dates.start_of_year(ctx.now, ctx.tz), dates.end_of_year(ctx.now, ctx.tz))
    rows = await db.budget.find_many(where={"userId": uid, "isActive": True}, include={"category": True})
    if not rows:
        return []

    async def spent_groups(span):
        return await db.transaction.group_by(by=["categoryId", "currency"], where={"userId": uid, "type": "EXPENSE",
                                                                                    "date": {"gte": span[0], "lte": span[1]}}, sum={"amount": True})

    month_groups = await spent_groups(month)
    year_groups = await spent_groups(year) if any(b.period == "YEARLY" for b in rows) else []
    categories = await category_map(uid)
    out = []
    for b in rows:
        spent = 0.0
        for g in year_groups if b.period == "YEARLY" else month_groups:
            if b.categoryId:
                cat = categories.get(g["categoryId"]) if g["categoryId"] else None
                if not cat or (cat.id != b.categoryId and cat.parentId != b.categoryId):
                    continue
            spent += ctx.fx.convert(_sum(g, "amount"), g["currency"], b.currency)
        amount = to_number(b.amount)
        spent = round2(spent)
        used = js_round((spent / amount) * 1000) / 10 if amount > 0 else 0
        out.append({
            "id": b.id, "name": b.name or (b.category.name if b.category else None) or shared(ctx.lang, "overallSpending"),
            "categoryId": b.categoryId, "categoryName": b.category.name if b.category else None,
            "color": b.category.color if b.category else None, "icon": b.category.icon if b.category else None,
            "period": b.period, "amount": amount, "currency": b.currency, "spent": spent, "remaining": round2(amount - spent),
            "percentUsed": used, "alertThreshold": b.alertThreshold, "isOver": spent > amount,
            "isWarning": spent <= amount and used >= b.alertThreshold,
        })
    return sorted(out, key=lambda b: -b["percentUsed"])


async def upcoming_bills(ctx: LifeContext, days: int, include_overdue: bool = True) -> list[dict]:
    """Active recurring payments due up to today + days (overdue ones too), occurrences expanded."""
    today_d, end_d = key_to_date(ctx.today), key_to_date(add_days_key(ctx.today, days))
    payments = await db.recurringpayment.find_many(
        where={"userId": ctx.user_id, "status": "ACTIVE", "nextDate": {"lte": end_d, **({} if include_overdue else {"gte": today_d})}},
        include={"category": True}, order={"nextDate": "asc"})
    out = []
    for p in payments:
        amount = to_number(p.amount)
        base = {
            "name": p.name, "kind": p.kind, "amount": amount, "currency": p.currency, "amountDefault": ctx.fx.to_default(amount, p.currency),
            "categoryName": p.category.name if p.category else None, "color": p.category.color if p.category else None,
            "icon": p.category.icon if p.category else None,
            "link": f"/finance/subscriptions?id={p.id}" if p.kind == "SUBSCRIPTION" else f"/finance/recurring?id={p.id}",
        }
        next_key = date_only_key(p.nextDate)
        out.append({**base, "id": p.id, "date": next_key, "daysUntil": diff_days_key(ctx.today, next_key), "isOverdue": next_key < ctx.today, "isNext": True})
        later = rrule.between(p.rrule, p.nextDate, p.nextDate + DAY, end_d, [], date_only=True)
        for d in later[:8]:
            k = date_only_key(d)
            out.append({**base, "id": f"{p.id}:{k}", "date": k, "daysUntil": diff_days_key(ctx.today, k), "isOverdue": False, "isNext": False})
    return sorted(out, key=lambda b: (b["date"], -b["amountDefault"]))


async def recurring_monthly(ctx: LifeContext) -> dict:
    rows = await db.recurringpayment.find_many(where={"userId": ctx.user_id, "status": "ACTIVE"})
    subs = every = 0.0
    sub_count = 0
    for r in rows:
        monthly = ctx.fx.to_default(r.amount, r.currency) * rrule.monthly_factor(r.rrule)
        every += monthly
        if r.kind == "SUBSCRIPTION":
            subs += monthly
            sub_count += 1
    return {"subscriptionsMonthly": round2(subs), "subscriptionCount": sub_count, "recurringMonthly": round2(every), "recurringCount": len(rows)}


# ----- calendar ------------------------------------------------------------------------


async def events_between(ctx: LifeContext, start: datetime, end: datetime, limit: int = 2000) -> list[dict]:
    uid = ctx.user_id
    single = await db.event.find_many(where={"userId": uid, "rrule": None, "startAt": {"lte": end},
                                             "OR": [{"endAt": {"gte": start}}, {"endAt": None, "startAt": {"gte": start}}]},
                                      order={"startAt": "asc"}, take=limit)
    recurring = await db.event.find_many(where={"userId": uid, "rrule": {"not": None}, "startAt": {"lte": end},
                                                "OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": start}}]})

    def make(e, first: datetime, last: datetime | None, repeats: bool) -> dict:
        key = day_key(first, ctx.tz)
        return {"id": f"{e.id}:{iso_ms(first)}" if repeats else e.id, "eventId": e.id, "title": e.title, "startAt": iso_ms(first),
                "endAt": iso_ms(last) if last else None, "allDay": e.allDay, "location": e.location, "type": e.type, "priority": e.priority,
                "color": e.color, "isRecurring": repeats, "day": key, "link": f"/calendar?id={e.id}&date={key}"}

    out = [make(e, e.startAt, e.endAt, False) for e in single]
    for e in recurring:
        duration = (e.endAt - e.startAt) if e.endAt else timedelta(0)
        for first in rrule.between(e.rrule, e.startAt, start - max(timedelta(0), duration), end, e.exDates or [], tz=ctx.tz, until=e.recurrenceEndAt):
            out.append(make(e, first, first + duration if e.endAt else None, True))
    return sorted(out, key=lambda o: o["startAt"])[:limit]


# ----- people --------------------------------------------------------------------------


async def upcoming_dates(ctx: LifeContext, days: int) -> list[dict]:
    """Birthdays, anniversaries and important dates in the next `days` days (today included)."""
    people = await db.person.find_many(where={"userId": ctx.user_id, "OR": [{"birthday": {"not": None}}, {"anniversary": {"not": None}}]})
    important = await db.importantdate.find_many(where={"userId": ctx.user_id}, include={"person": True})
    out = []

    def push(item: dict) -> None:
        until = diff_days_key(ctx.today, item["date"])
        if 0 <= until <= days:
            out.append({**item, "daysUntil": until})

    for p in people:
        if p.birthday:
            nxt = date_only_key(dates.next_anniversary(p.birthday, ctx.tz))
            push({"id": f"birthday:{p.id}", "kind": "birthday", "title": shared(ctx.lang, "birthdayOf", name=p.name), "personId": p.id,
                  "personName": p.name, "date": nxt, "turningAge": int(nxt[:4]) - dates._utc(p.birthday).year if p.birthdayHasYear else None,
                  "link": f"/people?id={p.id}"})
        if p.anniversary:
            nxt = date_only_key(dates.next_anniversary(p.anniversary, ctx.tz))
            years = int(nxt[:4]) - dates._utc(p.anniversary).year
            push({"id": f"anniversary:{p.id}", "kind": "anniversary", "title": shared(ctx.lang, "anniversaryOf", name=p.name), "personId": p.id,
                  "personName": p.name, "date": nxt, "turningAge": years if years > 0 else None, "link": f"/people?id={p.id}"})
    for d in important:
        nxt = date_only_key(dates.next_anniversary(d.date, ctx.tz)) if d.repeatsYearly else date_only_key(d.date)
        push({"id": f"date:{d.id}", "kind": "date", "title": d.title, "personId": d.personId, "personName": d.person.name if d.person else None,
              "date": nxt, "turningAge": None, "link": f"/people?id={d.personId}" if d.personId else "/people"})
    order = ("id", "kind", "title", "personId", "personName", "date", "daysUntil", "turningAge", "link")
    return [{k: x[k] for k in order} for x in sorted(out, key=lambda x: (x["daysUntil"], locale_key(x["title"])))]


# ----- habits and goals ----------------------------------------------------------------


async def habits_with_logs(ctx: LifeContext, start_key: str, end_key: str | None = None) -> tuple[list[dict], list[dict]]:
    """Active habits (as dicts) with their logs; a habit's start is moved back to its first
    log, as the habits section counts back-filled history."""
    end_key = end_key or ctx.today
    habits = await db.habit.find_many(where={"userId": ctx.user_id, "isArchived": False}, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    logs = [{"habitId": l.habitId, "date": l.date, "count": l.count}
            for l in await db.habitlog.find_many(where={"userId": ctx.user_id, "date": {"gte": key_to_date(start_key), "lte": key_to_date(end_key)}})]
    first: dict[str, datetime] = {}
    for l in logs:
        if l["habitId"] not in first or l["date"] < first[l["habitId"]]:
            first[l["habitId"]] = l["date"]
    out = []
    for h in habits:
        start = h.startDate
        if h.id in first and first[h.id] < start:
            start = first[h.id]
        out.append({f: getattr(h, f) for f in ("id", "name", "icon", "color", "frequency", "daysOfWeek", "timesPerWeek", "targetPerDay", "unit")}
                   | {"startDate": start})
    return out, logs


async def goals(ctx: LifeContext, where: dict | None = None, accounts: list[dict] | None = None) -> list[dict]:
    """Goal progress as the goals section works it out: a linked account's balance; a money
    goal's manual value plus its contributions; otherwise the manual value."""
    where = {"status": "ACTIVE"} if where is None else where
    rows = await db.goal.find_many(where={"userId": ctx.user_id, **where}, order=[{"deadline": "asc"}, {"createdAt": "asc"}],
                                   include={"milestones": True, "linkedAccount": True})
    if not rows:
        return []
    acc_map = {}
    if any(g.linkedAccountId for g in rows):
        acc_map = {a["id"]: a for a in (accounts if accounts is not None else await account_balances(ctx, True))}
    contrib = []
    if any(g.type == "FINANCIAL" for g in rows):
        contrib = await db.transaction.group_by(by=["goalId", "currency"], where={"userId": ctx.user_id, "goalId": {"in": [g.id for g in rows]}},
                                                sum={"amount": True})
    out = []
    for g in rows:
        display = (g.currency or (g.linkedAccount.currency if g.linkedAccount else None) or ctx.fx.default_currency) \
            if g.type == "FINANCIAL" or g.linkedAccountId else None
        current = to_number(g.currentValue)
        if g.linkedAccountId and g.linkedAccountId in acc_map:
            acc = acc_map[g.linkedAccountId]
            current = ctx.fx.convert(acc["balance"], acc["currency"], display or acc["currency"])
        elif g.type == "FINANCIAL":
            for c in contrib:
                if c["goalId"] == g.id:
                    current += ctx.fx.convert(_sum(c, "amount"), c["currency"], display or c["currency"])
        target = None if g.targetValue is None else to_number(g.targetValue)
        ms_total = len(g.milestones or [])
        ms_done = sum(1 for m in g.milestones or [] if m.completedAt)
        progress = 0
        if target and target > 0:
            progress = max(0, min(100, js_round((current / target) * 100)))
        elif ms_total:
            progress = js_round((ms_done / ms_total) * 100)
        if g.status == "COMPLETED":
            progress = 100
        deadline = date_only_key(g.deadline) if g.deadline else None
        start = date_only_key(g.startDate) if g.startDate else day_key(g.createdAt, ctx.tz)
        days_left = diff_days_key(ctx.today, deadline) if deadline else None
        time_progress = None
        if deadline:
            span, elapsed = diff_days_key(start, deadline), diff_days_key(start, ctx.today)
            time_progress = max(0, min(100, js_round((elapsed / span) * 100))) if span > 0 else (100 if elapsed >= 0 else 0)
        remaining = round2(max(0, target - current)) if target is not None else None
        monthly = (round2(remaining / max(1, math.ceil(days_left / 30.44)))
                   if days_left is not None and remaining is not None and g.status != "COMPLETED" else None)
        out.append({
            "id": g.id, "name": g.name, "type": g.type, "status": g.status, "progress": progress, "currentValue": round2(current),
            "targetValue": target, "remaining": remaining, "monthlyRequired": monthly, "unit": g.unit, "currency": display or g.currency,
            "startDate": date_only_key(g.startDate) if g.startDate else None, "deadline": deadline, "daysLeft": days_left,
            "timeProgress": time_progress, "color": g.color, "icon": g.icon, "completedAt": iso_ms(g.completedAt) if g.completedAt else None,
            "milestones": {"total": ms_total, "completed": ms_done}, "link": f"/goals/{g.id}",
        })
    return out


async def projects_progress(ctx: LifeContext, where: dict) -> list[dict]:
    """A project's progress: its override, or done / all of its top-level tasks."""
    rows = await db.project.find_many(where={"userId": ctx.user_id, **where}, order={"deadline": "asc"})
    if not rows:
        return []
    counts = await db.task.group_by(by=["projectId", "status"], where={"userId": ctx.user_id, "projectId": {"in": [p.id for p in rows]},
                                                                        "parentId": None}, count={"_all": True})
    out = []
    for p in rows:
        mine = [c for c in counts if c["projectId"] == p.id]
        total = sum(c["_count"]["_all"] for c in mine)
        done = sum(c["_count"]["_all"] for c in mine if c["status"] == "COMPLETED")
        if p.status == "COMPLETED":
            progress = 100
        elif p.progressOverride is not None:
            progress = p.progressOverride
        else:
            progress = js_round((done / total) * 100) if total else 0
        deadline = date_only_key(p.deadline) if p.deadline else None
        out.append({"id": p.id, "name": p.name, "status": p.status, "color": p.color, "icon": p.icon, "progress": progress,
                    "tasksTotal": total, "tasksDone": done, "deadline": deadline,
                    "daysLeft": diff_days_key(ctx.today, deadline) if deadline else None,
                    "completedAt": iso_ms(p.completedAt) if p.completedAt else None, "link": f"/projects/{p.id}"})
    return out
