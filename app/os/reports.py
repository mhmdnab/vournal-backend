"""/api/reports (port of vournalOS ReportsService): weekly, monthly and yearly reports,
read-only, in the user's currency and time zone. A disabled section's block is null."""

import re
from datetime import date, datetime, timedelta

from fastapi import Depends, Request

from app.db import db
from app.os import life_data as life
from app.os.common.collation import locale_key
from app.os.common.currency import to_number
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.query import unknown_params
from app.os.insight_habits import aggregate_completion, best_streak_in_range, completion_in_range, index_habit_logs, is_done_on, is_scheduled_on
from app.os.insight_text import enum_label, fmt_day as fmt_day_intl, js_round, ltr, money, month_short, shared
from app.os.insight_time import (add_days_key, date_only_key, day_key, days_in_month, diff_days_key, each_day_key, key_to_date, pct,
                                 pct_change, round2, week_start_key, weekday_of_key)

router = nest_router("/reports")

MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
IMPORTANT_EVENT_TYPES = {"EXAM", "APPOINTMENT", "TRAVEL"}
_PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _fmt_day(lang: str, k: str) -> str:
    """"Oct 5" / "5 تشرين الأول"."""
    if lang == "en":
        return f"{MONTH_NAMES[int(k[5:7]) - 1]} {int(k[8:10])}"
    return fmt_day_intl(lang, k, day=True)


def _week_label(lang: str, start: str, end: str) -> str:
    """"Oct 5–11" / "5–11 تشرين الأول" (the day range isolated so it never reorders)."""
    a, b = int(start[8:10]), int(end[8:10])
    if lang == "en":
        return f"{_fmt_day(lang, start)}–{b}"
    return f"{ltr(lang, f'{a}–{b}')} {fmt_day_intl(lang, start)}"


def _roll_key(key: str) -> str:
    """A "YYYY-MM-DD" that passed the pattern, rolled over as Date.UTC does (02-30 → 03-02)."""
    y, m, d = int(key[:4]), int(key[5:7]), int(key[8:10])
    months = y * 12 + (m - 1)
    return (date(months // 12, months % 12 + 1, 1) + timedelta(days=d - 1)).isoformat()


def _shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    total = y * 12 + (m - 1) + delta
    return f"{total // 12}-{total % 12 + 1:02d}"


def _count_by(values: list[str]) -> list[dict]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return sorted(({"key": k, "count": c} for k, c in counts.items()), key=lambda x: -x["count"])


def _range(ctx, start_key: str, end_key: str) -> dict:
    return {"from": life.start_of_day_key(start_key, ctx.tz), "to": life.end_of_day_key(end_key, ctx.tz), "fromKey": start_key, "toKey": end_key}


def _month_range(ctx, month: str) -> dict:
    y, m = int(month[:4]), int(month[5:7])
    return _range(ctx, f"{month}-01", f"{month}-{days_in_month(y, m):02d}")


def _day_habit_percent(habits, idx, k: str) -> int | None:
    """% of the DAILY/CUSTOM habits scheduled on day k that were done."""
    expected = done = 0
    for h in habits:
        if h["frequency"] == "WEEKLY" or not is_scheduled_on(h, k):
            continue
        expected += 1
        if is_done_on(h, idx, k):
            done += 1
    return js_round((done / expected) * 100) if expected else None


# ----- building blocks -----------------------------------------------------------------


async def _task_stats(ctx, r: dict) -> dict:
    uid, tz, now = ctx.user_id, ctx.tz, ctx.now
    completed = await db.task.find_many(where={"OR": [{"userId": uid}, {"assigneeId": uid}], "status": "COMPLETED", "completedAt": {"gte": r["from"], "lte": r["to"]}},
                                        include={"category": True}, order={"completedAt": "desc"}, take=2000)
    due = await db.task.find_many(where={"OR": [{"userId": uid}, {"assigneeId": uid}], "dueAt": {"gte": r["from"], "lte": r["to"]}}, include={"category": True},
                                  order={"dueAt": "asc"}, take=2000)
    start_today = life.start_of_day_key(ctx.today, tz)
    overdue_now = await db.task.count(where=life.overdue_task_where(uid, now, start_today))
    completed_by_day: dict[str, int] = {}
    for t in completed:
        k = day_key(t.completedAt, tz)
        completed_by_day[k] = completed_by_day.get(k, 0) + 1
    due_by_day: dict[str, int] = {}
    for t in due:
        k = day_key(t.dueAt, tz)
        due_by_day[k] = due_by_day.get(k, 0) + 1

    def late(t) -> bool:
        return bool(t.dueAt) and (t.dueAt < start_today or (t.hasDueTime and t.dueAt < now))

    overdue = [t for t in due if t.status != "COMPLETED" and late(t)]
    pending = [t for t in due if t.status != "COMPLETED" and not late(t)]
    cats: dict[str, dict] = {}
    for t in completed:
        key = t.category.id if t.category else "none"
        cur = cats.setdefault(key, {"categoryId": t.category.id if t.category else None,
                                    "name": t.category.name if t.category else shared(ctx.lang, "uncategorized"),
                                    "color": t.category.color if t.category else None, "count": 0})
        cur["count"] += 1

    def lite(t) -> dict:
        return {"id": t.id, "title": t.title, "status": t.status, "priority": t.priority, "dueAt": life.iso_ms(t.dueAt) if t.dueAt else None,
                "completedAt": life.iso_ms(t.completedAt) if t.completedAt else None, "isOverdue": t.status != "COMPLETED" and late(t),
                "link": _task_link(t)}

    return {
        "completed": len(completed), "dueTotal": len(due), "overdue": len(overdue), "overdueNow": overdue_now, "pending": len(pending),
        "completionRate": pct(sum(1 for t in due if t.status == "COMPLETED"), len(due)),
        "completedByDay": completed_by_day, "dueByDay": due_by_day,
        "completedItems": [lite(t) for t in completed], "pendingItems": [lite(t) for t in overdue + pending],
        "byCategory": sorted(cats.values(), key=lambda c: -c["count"]),
        "byLifeArea": _count_by([t.lifeArea or "NONE" for t in completed]),
    }


async def _finance_stats(ctx, r: dict) -> dict:
    txs = await life.transactions_in_range(ctx, r["from"], r["to"])
    by_category = await life.spending_by_category(ctx, r["from"], r["to"])
    income_by_category = await life.spending_by_category(ctx, r["from"], r["to"], "INCOME")
    expense_by_day: dict[str, float] = {}
    income_by_day: dict[str, float] = {}
    income = expenses = 0.0
    expense_list = []
    for t in txs:
        k = day_key(t.date, ctx.tz)
        v = ctx.fx.to_default(t.amount, t.currency)
        if t.type == "INCOME":
            income += v
            income_by_day[k] = income_by_day.get(k, 0) + v
        else:
            expenses += v
            expense_by_day[k] = expense_by_day.get(k, 0) + v
            expense_list.append((t, v))
    cats = await life.category_map(ctx.user_id)
    top = []
    for t, v in sorted(expense_list, key=lambda e: -e[1])[:5]:
        cat = cats.get(t.categoryId) if t.categoryId else None
        top.append({"id": t.id, "title": t.title or (cat.name if cat else None) or shared(ctx.lang, "expense"), "amount": to_number(t.amount),
                    "currency": t.currency, "amountDefault": round2(v), "date": life.iso_ms(t.date), "categoryName": cat.name if cat else None,
                    "link": f"/finance/transactions?id={t.id}"})
    return {"totals": {"income": round2(income), "expenses": round2(expenses), "savings": round2(income - expenses)},
            "byCategory": by_category, "incomeByCategory": income_by_category, "expenseByDay": expense_by_day, "incomeByDay": income_by_day,
            "topExpenses": top}


async def _habit_stats(ctx, r: dict) -> dict:
    habits, logs = await life.habits_with_logs(ctx, add_days_key(r["fromKey"], -1), r["toKey"])
    idx = index_habit_logs(logs)
    end = min(r["toKey"], ctx.today)
    week = each_day_key(r["fromKey"], r["toKey"])
    items = []
    for h in habits:
        c = completion_in_range(h, idx, r["fromKey"], r["toKey"], ctx.today)
        item = {"id": h["id"], "name": h["name"], "color": h["color"], "icon": h["icon"], "frequency": h["frequency"], "percent": c["percent"],
                "done": c["done"], "expected": c["expected"],
                "bestStreak": best_streak_in_range(h, idx, r["fromKey"], end, ctx.week_starts_on) if end >= r["fromKey"] else 0,
                "streakUnit": "weeks" if h["frequency"] == "WEEKLY" else "days"}
        if len(week) <= 7:
            item["days"] = [{"date": k, "scheduled": is_scheduled_on(h, k), "done": is_done_on(h, idx, k)} for k in week]
        item["link"] = f"/habits?id={h['id']}"
        items.append(item)
    return {"overall": aggregate_completion(habits, idx, r["fromKey"], r["toKey"], ctx.today), "items": items,
            "dayPercent": lambda k: _day_habit_percent(habits, idx, k)}


async def _goal_stats(ctx, r: dict) -> dict:
    goals = await life.goals(ctx, {"OR": [{"status": "ACTIVE"}, {"completedAt": {"gte": r["from"], "lte": r["to"]}}]})
    activity = await db.activitylog.find_many(where={"userId": ctx.user_id, "action": {"in": ["goal.progressed", "goal.completed", "milestone.completed"]},
                                                     "createdAt": {"gte": r["from"], "lte": r["to"]}}, order={"createdAt": "asc"}, take=2000)
    milestones = await db.goalmilestone.find_many(where={"goal": {"is": {"userId": ctx.user_id}}, "completedAt": {"gte": r["from"], "lte": r["to"]}},
                                                  include={"goal": True}, order={"completedAt": "asc"})
    completed = await db.goal.count(where={"userId": ctx.user_id, "completedAt": {"gte": r["from"], "lte": r["to"]}})
    updates: dict[str, int] = {}
    for a in activity:
        if a.entityId:
            updates[a.entityId] = updates.get(a.entityId, 0) + 1
    start_iso, end_iso = life.iso_ms(r["from"]), life.iso_ms(r["to"])
    return {
        "active": sum(1 for g in goals if g["status"] == "ACTIVE"),
        "completed": completed,
        "progressUpdates": sum(1 for a in activity if a.action == "goal.progressed"),
        "items": [{"id": g["id"], "name": g["name"], "type": g["type"], "status": g["status"], "progress": g["progress"], "timeProgress": g["timeProgress"],
                   "deadline": g["deadline"], "daysLeft": g["daysLeft"], "color": g["color"], "icon": g["icon"], "updatesInPeriod": updates.get(g["id"], 0),
                   "completedInPeriod": bool(g["completedAt"]) and start_iso <= g["completedAt"] <= end_iso, "link": g["link"]} for g in goals],
        "milestonesCompleted": [{"id": m.id, "title": m.title, "goalId": m.goal.id, "goalName": m.goal.name, "completedAt": life.iso_ms(m.completedAt),
                                 "link": f"/goals/{m.goal.id}"} for m in milestones],
    }


def _task_link(t) -> str:
    """A workspace task opens in its workspace (the dashboard reads ?space=)."""
    space = getattr(t, "workspaceId", None)
    return f"/tasks?id={t.id}&space={space}" if space else f"/tasks?id={t.id}"


async def _upcoming_deadlines(ctx, r: dict) -> list[dict]:
    """Deadlines in a (future) range: tasks, projects, assessments, bills, documents, maintenance."""
    on, uid = ctx.sections.__contains__, ctx.user_id
    d_from, d_to = key_to_date(r["fromKey"]), key_to_date(r["toKey"])
    out = []
    if on("tasks"):
        for t in await db.task.find_many(where={"OR": [{"userId": uid}, {"assigneeId": uid}], "status": {"not": "COMPLETED"}, "dueAt": {"gte": r["from"], "lte": r["to"]}},
                                         order={"dueAt": "asc"}, take=30):
            out.append({"kind": "task", "id": t.id, "title": t.title, "subtitle": None, "date": day_key(t.dueAt, ctx.tz), "priority": t.priority,
                        "link": _task_link(t)})
    if on("projects"):
        for p in await db.project.find_many(where={"userId": uid, "workspaceId": None, "status": {"in": ["PLANNING", "ACTIVE", "ON_HOLD"]}, "deadline": {"gte": d_from, "lte": d_to}}):
            out.append({"kind": "project", "id": p.id, "title": p.name, "subtitle": shared(ctx.lang, "projectDeadline"), "date": date_only_key(p.deadline),
                        "priority": "HIGH", "link": f"/projects/{p.id}"})
    if on("study"):
        for a in await db.assessment.find_many(where={"userId": uid, "status": {"in": ["NOT_STARTED", "IN_PROGRESS"]}, "dueAt": {"gte": r["from"], "lte": r["to"]}},
                                               include={"course": True}):
            out.append({"kind": "assessment", "id": a.id, "title": a.title, "subtitle": a.course.name, "date": day_key(a.dueAt, ctx.tz),
                        "priority": "HIGH" if a.type == "EXAM" else None, "link": f"/study?id={a.course.id}"})
    if on("finance"):
        for b in await db.recurringpayment.find_many(where={"userId": uid, "status": "ACTIVE", "nextDate": {"gte": d_from, "lte": d_to}}):
            sub = b.kind == "SUBSCRIPTION"
            out.append({"kind": "subscription" if sub else "bill", "id": b.id, "title": b.name, "subtitle": money(ctx.lang, to_number(b.amount), b.currency),
                        "date": date_only_key(b.nextDate), "priority": None,
                        "link": f"/finance/subscriptions?id={b.id}" if sub else f"/finance/recurring?id={b.id}"})
    if on("documents"):
        for d in await db.document.find_many(where={"userId": uid, "expiryDate": {"gte": d_from, "lte": d_to}}):
            out.append({"kind": "document", "id": d.id, "title": d.name, "subtitle": shared(ctx.lang, "expires"), "date": date_only_key(d.expiryDate),
                        "priority": "HIGH", "link": f"/documents?id={d.id}"})
    if on("maintenance"):
        for m in await db.maintenanceitem.find_many(where={"userId": uid, "isActive": True, "nextDueAt": {"gte": d_from, "lte": d_to}}):
            out.append({"kind": "maintenance", "id": m.id, "title": m.title, "subtitle": shared(ctx.lang, "maintenance"), "date": date_only_key(m.nextDueAt),
                        "priority": None, "link": f"/maintenance?id={m.id}"})
    out.sort(key=lambda x: (locale_key(x["date"]), _PRIORITY_RANK.get(x["priority"] or "LOW", 3)))
    return out[:40]


async def _dates_in_range(ctx, start_key: str, end_key: str) -> list[dict]:
    """Birthdays, anniversaries and important dates falling in [start, end]."""
    people = await db.person.find_many(where={"userId": ctx.user_id, "OR": [{"birthday": {"not": None}}, {"anniversary": {"not": None}}]})
    important = await db.importantdate.find_many(where={"userId": ctx.user_id})
    years = list(dict.fromkeys((int(start_key[:4]), int(end_key[:4]))))

    def occ(value: datetime, yearly: bool) -> list[str]:
        if not yearly:
            return [k for k in [date_only_key(value)] if start_key <= k <= end_key]
        d = life.dates._utc(value)
        keys = [f"{yy}-{d.month:02d}-{min(d.day, days_in_month(yy, d.month)):02d}" for yy in years]
        return [k for k in keys if start_key <= k <= end_key]

    out = []
    for p in people:
        if p.birthday:
            for k in occ(p.birthday, True):
                out.append({"kind": "birthday", "id": f"birthday:{p.id}:{k}", "title": shared(ctx.lang, "birthdayOf", name=p.name), "date": k,
                            "turningAge": int(k[:4]) - life.dates._utc(p.birthday).year if p.birthdayHasYear else None, "link": f"/people?id={p.id}"})
        if p.anniversary:
            for k in occ(p.anniversary, True):
                out.append({"kind": "anniversary", "id": f"anniversary:{p.id}:{k}", "title": shared(ctx.lang, "anniversaryOf", name=p.name), "date": k,
                            "turningAge": None, "link": f"/people?id={p.id}"})
    for d in important:
        for k in occ(d.date, d.repeatsYearly):
            out.append({"kind": "date", "id": f"date:{d.id}:{k}", "title": d.title, "date": k, "turningAge": None,
                        "link": f"/people?id={d.personId}" if d.personId else "/people"})
    return sorted(out, key=lambda x: locale_key(x["date"]))


def _finance_block(finance: dict, prev_totals: dict | None) -> dict:
    t = finance["totals"]
    return {
        "income": t["income"], "expenses": t["expenses"], "savings": t["savings"],
        "savingsRate": js_round((t["savings"] / t["income"]) * 100) if t["income"] > 0 else None,
    }


def _change(prev: dict | None, t: dict) -> dict | None:
    if not prev:
        return None
    return {"income": pct_change(prev["income"], t["income"]), "expenses": pct_change(prev["expenses"], t["expenses"]),
            "savings": round2(t["savings"] - prev["savings"])}


# ----- weekly --------------------------------------------------------------------------


async def weekly(user, day: str | None = None) -> dict:
    ctx = await life.context(user)
    start_key = week_start_key(_roll_key(day) if day else ctx.today, ctx.week_starts_on)
    end_key = add_days_key(start_key, 6)
    r = _range(ctx, start_key, end_key)
    prev = _range(ctx, add_days_key(start_key, -7), add_days_key(start_key, -1))
    # upcoming deadlines: this week → from today to the end of next week; otherwise the following week
    current = start_key <= ctx.today <= end_key
    nxt = _range(ctx, ctx.today, add_days_key(start_key, 13)) if current else _range(ctx, add_days_key(start_key, 7), add_days_key(start_key, 13))
    on = ctx.sections.__contains__
    tasks = await _task_stats(ctx, r) if on("tasks") else None
    prev_done = await db.task.count(where={"OR": [{"userId": ctx.user_id}, {"assigneeId": ctx.user_id}], "status": "COMPLETED", "completedAt": {"gte": prev["from"], "lte": prev["to"]}}) \
        if on("tasks") else 0
    events = await life.events_between(ctx, r["from"], r["to"], 1000) if on("calendar") else None
    finance = await _finance_stats(ctx, r) if on("finance") else None
    prev_totals = await life.totals(ctx, prev["from"], prev["to"]) if on("finance") else None
    habits = await _habit_stats(ctx, r) if on("habits") else None
    goals = await _goal_stats(ctx, r) if on("goals") else None
    upcoming = await _upcoming_deadlines(ctx, nxt)
    per_day = []
    for k in each_day_key(start_key, end_key):
        future = k > ctx.today
        per_day.append({
            "date": k, "weekday": weekday_of_key(k), "isToday": k == ctx.today, "isFuture": future,
            "tasksCompleted": tasks["completedByDay"].get(k, 0) if tasks else None,
            "tasksDue": tasks["dueByDay"].get(k, 0) if tasks else None,
            "spend": round2(finance["expenseByDay"].get(k, 0)) if finance else None,
            "income": round2(finance["incomeByDay"].get(k, 0)) if finance else None,
            "habitsPercent": (None if future else habits["dayPercent"](k)) if habits else None,
            "events": sum(1 for e in events if e["day"] == k) if events is not None else None,
        })
    return {
        "period": {"type": "week", "from": start_key, "to": end_key, "label": f"{_fmt_day(ctx.lang, start_key)} – {_fmt_day(ctx.lang, end_key)}",
                   "prev": add_days_key(start_key, -7), "next": add_days_key(start_key, 7), "isCurrent": current},
        "upcomingRange": {"from": nxt["fromKey"], "to": nxt["toKey"]},
        "currency": ctx.fx.default_currency,
        "enabledSections": ctx.settings.enabledSections,
        "tasks": {
            "completed": tasks["completed"], "pending": tasks["pending"], "overdue": tasks["overdue"], "overdueNow": tasks["overdueNow"],
            "dueTotal": tasks["dueTotal"], "completionRate": tasks["completionRate"], "previousCompleted": prev_done,
            "change": pct_change(prev_done, tasks["completed"]), "completedItems": tasks["completedItems"][:20],
            "pendingItems": tasks["pendingItems"][:20], "byLifeArea": tasks["byLifeArea"],
        } if tasks else None,
        "calendar": {"count": len(events), "items": events[:40]} if events is not None else None,
        "finance": {**_finance_block(finance, prev_totals), "byCategory": finance["byCategory"], "topExpenses": finance["topExpenses"],
                    "previous": prev_totals, "change": _change(prev_totals, finance["totals"])} if finance else None,
        "habits": {"completionRate": habits["overall"]["percent"], "expected": habits["overall"]["expected"], "done": habits["overall"]["done"],
                   "items": habits["items"]} if habits else None,
        "goals": goals,
        "upcomingDeadlines": upcoming,
        "perDay": per_day,
    }


# ----- monthly -------------------------------------------------------------------------


async def monthly(user, month: str | None = None) -> dict:
    ctx = await life.context(user)
    m = month or ctx.today[:7]
    y, mo = int(m[:4]), int(m[5:7])
    start_key, end_key = f"{m}-01", f"{m}-{days_in_month(y, mo):02d}"
    r = _range(ctx, start_key, end_key)
    prev_month, next_month = _shift_month(m, -1), _shift_month(m, 1)
    prev, next_r = _month_range(ctx, prev_month), _month_range(ctx, next_month)
    on = ctx.sections.__contains__
    days = each_day_key(start_key, end_key)
    tasks = await _task_stats(ctx, r) if on("tasks") else None
    finance = await _finance_stats(ctx, r) if on("finance") else None
    prev_totals = await life.totals(ctx, prev["from"], prev["to"]) if on("finance") else None
    prev_by_category = await life.spending_by_category(ctx, prev["from"], prev["to"]) if on("finance") else None
    habits = await _habit_stats(ctx, r) if on("habits") else None
    goals = await _goal_stats(ctx, r) if on("goals") else None
    events = await life.events_between(ctx, r["from"], r["to"], 2000) if on("calendar") else None
    next_events = await life.events_between(ctx, next_r["from"], next_r["to"], 2000) if on("calendar") else None
    next_dates = await _dates_in_range(ctx, next_r["fromKey"], next_r["toKey"]) if on("people") else None

    weeks = []
    w = week_start_key(start_key, ctx.week_starts_on)
    while w <= end_key:
        a = max(w, start_key)
        b = min(add_days_key(w, 6), end_key)
        weeks.append({"from": a, "to": b, "label": _week_label(ctx.lang, a, b)})
        w = add_days_key(w, 7)

    calendar = None
    if events is not None:
        per_day: dict[str, int] = {}
        for e in events:
            per_day[e["day"]] = per_day.get(e["day"], 0) + 1
        busiest = sorted(per_day.items(), key=lambda kv: (-kv[1], locale_key(kv[0])))[:5]
        by_type: dict[str, int] = {}
        for e in events:
            by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        important = [{"kind": "event", "id": e["id"], "title": e["title"], "date": e["startAt"], "type": e["type"], "priority": e["priority"], "link": e["link"]}
                     for e in next_events if e["priority"] in ("HIGH", "URGENT") or e["type"] in IMPORTANT_EVENT_TYPES][:8]
        important += [{"kind": d["kind"], "id": d["id"], "title": d["title"], "date": d["date"], "type": None, "priority": None, "link": d["link"]}
                      for d in next_dates or []]
        calendar = {
            "count": len(events), "busyDays": len(per_day),
            "busiestDays": [{"date": d, "count": c, "titles": [e["title"] for e in events if e["day"] == d][:4]} for d, c in busiest],
            "perDay": [{"date": k, "count": per_day.get(k, 0)} for k in days],
            "byType": sorted(({"type": t, "count": c} for t, c in by_type.items()), key=lambda x: -x["count"]),
            "upcomingImportant": sorted(important, key=lambda x: locale_key(x["date"]))[:10],
        }
    finance_out = None
    if finance:
        t = finance["totals"]

        def with_prev(c: dict) -> dict:
            p = next((x for x in prev_by_category or [] if x["categoryId"] == c["categoryId"]), None)
            return {**c, "previous": p["amount"] if p else 0, "change": pct_change(p["amount"], c["amount"]) if p else None}

        elapsed = diff_days_key(start_key, ctx.today if ctx.today < end_key else end_key) + 1
        finance_out = {
            **_finance_block(finance, prev_totals), "byCategory": [with_prev(c) for c in finance["byCategory"]],
            "incomeByCategory": finance["incomeByCategory"], "topExpenses": finance["topExpenses"], "previous": prev_totals,
            "change": _change(prev_totals, t),
            "daily": [{"date": k, "expenses": round2(finance["expenseByDay"].get(k, 0)), "income": round2(finance["incomeByDay"].get(k, 0))} for k in days],
            "dailyAverage": round2(t["expenses"] / max(1, min(len(days), max(1, elapsed)))),
        }
    habits_out = None
    if habits:
        best = sorted(habits["items"], key=lambda h: -h["bestStreak"])
        habits_out = {"completionRate": habits["overall"]["percent"], "expected": habits["overall"]["expected"], "done": habits["overall"]["done"],
                      "items": habits["items"], "bestStreaks": [h for h in best if h["bestStreak"] > 0][:5],
                      "perDay": [{"date": k, "percent": None if k > ctx.today else habits["dayPercent"](k)} for k in days]}
    return {
        "period": {"type": "month", "month": m, "from": start_key, "to": end_key,
                   "label": f"{MONTH_NAMES[mo - 1]} {y}" if ctx.lang == "en" else fmt_day_intl(ctx.lang, start_key, year=True),
                   "prev": prev_month, "next": next_month, "isCurrent": ctx.today[:7] == m},
        "currency": ctx.fx.default_currency,
        "enabledSections": ctx.settings.enabledSections,
        "productivity": {
            "completed": tasks["completed"], "overdue": tasks["overdue"], "pending": tasks["pending"], "dueTotal": tasks["dueTotal"],
            "completionRate": tasks["completionRate"], "byCategory": tasks["byCategory"], "byLifeArea": tasks["byLifeArea"],
            "perWeek": [{**w, "completed": sum(tasks["completedByDay"].get(k, 0) for k in each_day_key(w["from"], w["to"]))} for w in weeks],
            "topCompleted": tasks["completedItems"][:10],
        } if tasks else None,
        "finance": finance_out,
        "habits": habits_out,
        "goals": goals,
        "calendar": calendar,
    }


# ----- yearly --------------------------------------------------------------------------


async def yearly(user, year: str | None = None) -> dict:
    ctx = await life.context(user)
    y = int(year or ctx.today[:4])
    start_key, end_key = f"{y}-01-01", f"{y}-12-31"
    r = _range(ctx, start_key, end_key)
    on, uid = ctx.sections.__contains__, ctx.user_id
    txs = await life.transactions_in_range(ctx, r["from"], r["to"]) if on("finance") else None
    by_category = await life.spending_by_category(ctx, r["from"], r["to"]) if on("finance") else None
    income_by_category = await life.spending_by_category(ctx, r["from"], r["to"], "INCOME") if on("finance") else None
    tasks_done = await db.task.find_many(where={"OR": [{"userId": uid}, {"assigneeId": uid}], "status": "COMPLETED", "completedAt": {"gte": r["from"], "lte": r["to"]}}) \
        if on("tasks") else None
    habit_data = await life.habits_with_logs(ctx, start_key, end_key) if on("habits") else None
    events = await life.events_between(ctx, r["from"], r["to"], 5000) if on("calendar") else None
    goals_done = await db.goal.find_many(where={"userId": uid, "status": "COMPLETED", "completedAt": {"gte": r["from"], "lte": r["to"]}},
                                         order={"completedAt": "asc"}) if on("goals") else None
    projects_done = await db.project.find_many(where={"userId": uid, "workspaceId": None, "status": "COMPLETED", "completedAt": {"gte": r["from"], "lte": r["to"]}},
                                               order={"completedAt": "asc"}) if on("projects") else None
    trips = await db.trip.find_many(where={"userId": uid, "status": {"not": "CANCELLED"}, "startDate": {"gte": key_to_date(start_key), "lte": key_to_date(end_key)}},
                                    order={"startDate": "asc"}) if on("travel") else None
    dates = await _dates_in_range(ctx, start_key, end_key) if on("people") else None

    idx = index_habit_logs(habit_data[1]) if habit_data else None
    months = []
    for i in range(12):
        mk = f"{y}-{i + 1:02d}"
        months.append({"month": mk, "label": MONTH_NAMES[i] if ctx.lang == "en" else month_short(ctx.lang, y, i + 1), "from": f"{mk}-01",
                       "to": f"{mk}-{days_in_month(y, i + 1):02d}", "isFuture": f"{mk}-01" > ctx.today})
    inc, exp = [0.0] * 12, [0.0] * 12
    for t in txs or []:
        i = int(day_key(t.date, ctx.tz)[5:7]) - 1
        v = ctx.fx.to_default(t.amount, t.currency)
        if t.type == "INCOME":
            inc[i] += v
        else:
            exp[i] += v
    tasks_per_month = [0] * 12
    for t in tasks_done or []:
        tasks_per_month[int(day_key(t.completedAt, ctx.tz)[5:7]) - 1] += 1
    events_per_month = [0] * 12
    for e in events or []:
        events_per_month[int(e["day"][5:7]) - 1] += 1
    series = []
    for i, mm in enumerate(months):
        habit_pct = aggregate_completion(habit_data[0], idx, mm["from"], mm["to"], ctx.today)["percent"] if habit_data and not mm["isFuture"] else None
        series.append({"month": mm["month"], "label": mm["label"], "isFuture": mm["isFuture"],
                       "income": round2(inc[i]) if txs is not None else None, "expenses": round2(exp[i]) if txs is not None else None,
                       "savings": round2(inc[i] - exp[i]) if txs is not None else None,
                       "tasksCompleted": tasks_per_month[i] if tasks_done is not None else None, "habitPercent": habit_pct,
                       "events": events_per_month[i] if events is not None else None})
    total_income = total_expenses = 0.0
    for v in inc:
        total_income += v
    for v in exp:
        total_expenses += v
    total_income, total_expenses = round2(total_income), round2(total_expenses)
    past = [s for s in series if not s["isFuture"] and (s["income"] or s["expenses"])]
    best = worst = None
    if txs is not None and past:
        best = past[0]
        for s in past[1:]:
            if (s["savings"] or 0) > (best["savings"] or 0):
                best = s
    if txs is not None and len(past) > 1:
        worst = past[0]
        for s in past[1:]:
            if (s["savings"] or 0) < (worst["savings"] or 0):
                worst = s
    habits = None
    if habit_data:
        hs = habit_data[0]
        overall = aggregate_completion(hs, idx, start_key, end_key, ctx.today)
        items = []
        for h in hs:
            c = completion_in_range(h, idx, start_key, end_key, ctx.today)
            items.append({"id": h["id"], "name": h["name"], "color": h["color"], "icon": h["icon"], "percent": c["percent"],
                          "checkIns": sum(1 for k in idx.get(h["id"], {}) if start_key <= k <= end_key),
                          "bestStreak": best_streak_in_range(h, idx, start_key, min(end_key, ctx.today), ctx.week_starts_on),
                          "unit": "weeks" if h["frequency"] == "WEEKLY" else "days"})
        with_pct = sorted((s for s in series if s["habitPercent"] is not None), key=lambda s: -(s["habitPercent"] or 0))
        habits = {"completionRate": overall["percent"], "totalCheckIns": sum(h["checkIns"] for h in items),
                  "items": sorted(items, key=lambda h: -(h["percent"] if h["percent"] is not None else -1)),
                  "bestMonth": with_pct[0]["month"] if with_pct else None}
    important = []
    for e in events or []:
        if e["priority"] in ("HIGH", "URGENT") or e["type"] in IMPORTANT_EVENT_TYPES:
            important.append({"kind": "event", "id": e["id"], "title": e["title"], "date": e["startAt"],
                              "subtitle": enum_label(ctx.lang, "eventType", e["type"]), "link": e["link"]})
    for t in trips or []:
        important.append({"kind": "trip", "id": t.id, "title": t.name, "date": date_only_key(t.startDate),
                          "subtitle": shared(ctx.lang, "tripTo", destination=t.destination), "link": f"/travel/{t.id}"})
    for d in dates or []:
        important.append({"kind": d["kind"], "id": d["id"], "title": d["title"], "date": d["date"],
                          "subtitle": shared(ctx.lang, "turns", age=d["turningAge"]) if d["turningAge"] else None, "link": d["link"]})
    important.sort(key=lambda x: locale_key(x["date"]))
    return {
        "period": {"type": "year", "year": y, "from": start_key, "to": end_key, "label": str(y), "prev": str(y - 1), "next": str(y + 1),
                   "isCurrent": ctx.today[:4] == str(y)},
        "currency": ctx.fx.default_currency,
        "enabledSections": ctx.settings.enabledSections,
        "totals": {
            "income": total_income if txs is not None else None, "expenses": total_expenses if txs is not None else None,
            "savings": round2(total_income - total_expenses) if txs is not None else None,
            "savingsRate": js_round(((total_income - total_expenses) / total_income) * 100) if txs is not None and total_income > 0 else None,
            "tasksCompleted": len(tasks_done) if tasks_done is not None else None,
            "goalsCompleted": len(goals_done) if goals_done is not None else None,
            "projectsCompleted": len(projects_done) if projects_done is not None else None,
            "events": len(events) if events is not None else None,
            "habitCompletion": habits["completionRate"] if habits else None,
        },
        "months": series,
        "spendingByCategory": by_category,
        "incomeByCategory": income_by_category,
        "bestMonth": {"month": best["month"], "label": best["label"], "savings": best["savings"]} if best else None,
        "worstMonth": {"month": worst["month"], "label": worst["label"], "savings": worst["savings"]} if worst and best and worst["month"] != best["month"] else None,
        "goalsCompleted": [{"id": g.id, "name": g.name, "type": g.type, "completedAt": life.iso_ms(g.completedAt) if g.completedAt else None,
                            "link": f"/goals/{g.id}"} for g in goals_done] if goals_done is not None else None,
        "projectsCompleted": [{"id": p.id, "name": p.name, "completedAt": life.iso_ms(p.completedAt) if p.completedAt else None,
                               "link": f"/projects/{p.id}"} for p in projects_done] if projects_done is not None else None,
        "tasksByLifeArea": _count_by([t.lifeArea or "NONE" for t in tasks_done]) if tasks_done is not None else None,
        "habits": habits,
        "importantEvents": important[:40],
    }


# ----- routes --------------------------------------------------------------------------


def _query(request: Request, name: str, pattern: str, message: str) -> str | None:
    errors = unknown_params(request, (name,))
    value = request.query_params.get(name)
    if value is not None and not re.fullmatch(pattern, value):
        errors.append(message)
    if errors:
        raise nest_error(400, errors)
    return value


@router.get("/weekly")
async def get_weekly(request: Request, user=Depends(plus_user)):
    return await weekly(user, _query(request, "date", r"\d{4}-\d{2}-\d{2}", "date must be YYYY-MM-DD"))


@router.get("/monthly")
async def get_monthly(request: Request, user=Depends(plus_user)):
    return await monthly(user, _query(request, "month", r"\d{4}-(0[1-9]|1[0-2])", "month must be YYYY-MM"))


@router.get("/yearly")
async def get_yearly(request: Request, user=Depends(plus_user)):
    return await yearly(user, _query(request, "year", r"(19|20|21)\d{2}", "year must be YYYY"))
