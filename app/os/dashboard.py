"""/api/dashboard (port of vournalOS DashboardService): everything the home screen needs in
one call. A disabled section's widget is null and never queried."""

import logging
from datetime import date, timedelta

from fastapi import Depends

from app import todo_rules
from app.db import db
from app.os import app_todos
from app.os import life_data as life
from app.os.common import dates
from app.os.common.collation import locale_key
from app.os.common.currency import to_number
from app.os.common.deps import plus_user
from app.os.common.http import nest_router
from app.os.common.i18n import fmt, join_list, plural
from app.os.insight_habits import count_on, current_streak, done_in_week, index_habit_logs, is_done_on, is_due_today, times_per_week
from app.os.insight_text import enum_label, js_round, shared
from app.os.insight_time import add_days_key, date_only_key, day_key, days_in_month, diff_days_key, key_to_date, pct_change, round2
from app.os.insights import insights_for
from app.os.settings import active_focus

router = nest_router("/dashboard")

PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
MESSAGES = {
    "en": {
        "summary": "{list} today.",
        "summaryEmpty": "Nothing urgent today — a clear day ahead.",
        "tasks": {"one": "{count} task", "other": "{count} tasks"},
        "events": {"one": "{count} event", "other": "{count} events"},
        "bills": {"one": "{count} bill", "other": "{count} bills"},
        "reminders": {"one": "{count} reminder", "other": "{count} reminders"},
        "habits": {"one": "{count} habit", "other": "{count} habits"},
    },
    "ar": {
        "summary": "لديك اليوم: {list}.",
        "summaryEmpty": "لا شيء عاجل اليوم — يومك صافٍ.",
        "tasks": {"one": "مهمة واحدة", "two": "مهمتان", "few": "{count} مهام", "many": "{count} مهمة", "other": "{count} مهمة"},
        "events": {"one": "حدث واحد", "two": "حدثان", "few": "{count} أحداث", "many": "{count} حدثًا", "other": "{count} حدث"},
        "bills": {"one": "فاتورة واحدة", "two": "فاتورتان", "few": "{count} فواتير", "many": "{count} فاتورة", "other": "{count} فاتورة"},
        "reminders": {"one": "تذكير واحد", "two": "تذكيران", "few": "{count} تذكيرات", "many": "{count} تذكيرًا", "other": "{count} تذكير"},
        "habits": {"one": "عادة واحدة", "two": "عادتان", "few": "{count} عادات", "many": "{count} عادة", "other": "{count} عادة"},
    },
}


def _brief(row, *fields):
    return {f: getattr(row, f) for f in fields} if row else None


def _task(t, ctx) -> dict:
    return {
        "id": t.id, "title": t.title, "status": t.status, "priority": t.priority, "dueAt": life.iso_ms(t.dueAt) if t.dueAt else None,
        "hasDueTime": t.hasDueTime, "completedAt": life.iso_ms(t.completedAt) if t.completedAt else None,
        "focusDate": date_only_key(t.focusDate) if t.focusDate else None, "lifeArea": t.lifeArea, "estimatedMinutes": t.estimatedMinutes,
        "isRecurring": bool(t.rrule), "isSubtask": bool(t.parentId),
        "isOverdue": t.status != "COMPLETED" and bool(t.dueAt) and (t.dueAt < ctx.now if t.hasDueTime else day_key(t.dueAt, ctx.tz) < ctx.today),
        "category": _brief(t.category, "id", "name", "color", "icon"), "project": _brief(t.project, "id", "name", "color"),
        "link": f"/tasks?id={t.id}" + (f"&space={t.workspaceId}" if t.workspaceId else ""),
        "workspaceId": t.workspaceId,
    }


def _by_priority(t) -> tuple:
    return (PRIORITY_RANK[t.priority], t.dueAt.timestamp() if t.dueAt else 0)


_TASK_INCLUDE = {"category": True, "project": True}


async def _tasks(ctx) -> dict:
    uid, tz, now = ctx.user_id, ctx.tz, ctx.now
    today_from, today_to = dates.start_of_day(now, tz), dates.end_of_day(now, tz)
    today_d = key_to_date(ctx.today)
    # my day: my Personal tasks and the workspace tasks assigned to me
    mine_or_assigned = {"OR": [{"userId": uid, "workspaceId": None}, {"assigneeId": uid, "workspaceId": {"not": None}}]}
    today = await db.task.find_many(where={"AND": [mine_or_assigned], "dueAt": {"gte": today_from, "lte": today_to},
                                           "OR": [{"status": {"not": "COMPLETED"}}, {"completedAt": {"gte": today_from}}]},
                                    include=_TASK_INCLUDE, order={"dueAt": "asc"}, take=50)
    overdue_where = life.overdue_task_where(uid, now, today_from)
    overdue = await db.task.find_many(where=overdue_where, include=_TASK_INCLUDE, order={"dueAt": "asc"}, take=30)
    overdue_count = await db.task.count(where=overdue_where)
    focus = await db.task.find_many(where={"userId": uid, "workspaceId": None, "focusDate": today_d}, include=_TASK_INCLUDE,
                                    order=[{"position": "asc"}, {"createdAt": "asc"}], take=3)
    candidates = await db.task.find_many(where={"userId": uid, "workspaceId": None, "status": {"not": "COMPLETED"}, "dueAt": {"lte": today_to},
                                                "OR": [{"focusDate": None}, {"focusDate": {"not": today_d}}]},
                                         include=_TASK_INCLUDE, order={"dueAt": "asc"}, take=60)
    # the app's to-dos list with the web's own tasks (a to-do counts on its own day)
    zone = app_todos.zone(tz)
    day = date.fromisoformat(ctx.today)
    todos = await app_todos.for_user(uid)
    todo_today = [t for t in todos if todo_rules.local_day(t, zone) == day]
    todo_overdue = [t for t in todos if app_todos.is_overdue(t, day, now, zone)]
    todo_open = [t for t in todos if not t.done and (d := todo_rules.local_day(t, zone)) and d <= day]

    def views(rows, todos_too) -> list[dict]:
        return [_task(t, ctx) for t in rows] + [app_todos.as_task(t, tz, day, now) for t in todos_too]

    def priority_order(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda t: (PRIORITY_RANK[t["priority"]], dates.parse_instant(t["dueAt"]).timestamp() if t["dueAt"] else 0))

    today_items = sorted(views(today, todo_today),
                         key=lambda t: (t["status"] == "COMPLETED", PRIORITY_RANK[t["priority"]], locale_key(t["dueAt"] or "")))
    return {
        "todayTasks": {"items": today_items, "total": len(today) + len(todo_today),
                       "completed": sum(1 for t in today if t.status == "COMPLETED") + sum(1 for t in todo_today if t.done)},
        "overdue": {"count": overdue_count + len(todo_overdue), "items": priority_order(views(overdue, todo_overdue))[:6]},
        "topPriorities": {"date": ctx.today, "items": [_task(t, ctx) for t in focus],
                          "suggestions": priority_order(views(candidates, todo_open))[:3] if len(focus) < 3 else []},
    }


async def _upcoming_events(ctx) -> dict:
    start = dates.start_of_day(ctx.now, ctx.tz)
    end = life.end_of_day_key(add_days_key(ctx.today, 7), ctx.tz)
    every = await life.events_between(ctx, start, end, 500, overlay=True)
    now_ms = ctx.now.timestamp() * 1000

    def ms(iso: str) -> float:
        return dates.parse_instant(iso).timestamp() * 1000

    def still_on(e) -> bool:
        finish = ms(e["endAt"]) if e["endAt"] else ms(e["startAt"]) + (86_400_000 if e["allDay"] else 3_600_000)
        return finish >= now_ms or (e["allDay"] and e["day"] == ctx.today)

    upcoming = [e for e in every if still_on(e)]
    return {"items": [{**e, "isToday": e["day"] == ctx.today, "isNow": ms(e["startAt"]) <= now_ms} for e in upcoming[:8]],
            "todayCount": sum(1 for e in every if e["day"] == ctx.today)}


async def _reminders(ctx) -> dict:
    today_to = dates.end_of_day(ctx.now, ctx.tz)
    due = await db.reminder.find_many(where={"userId": ctx.user_id, "isDone": False, "remindAt": {"lte": today_to}}, order={"remindAt": "asc"}, take=20)
    upcoming = await db.reminder.find_many(where={"userId": ctx.user_id, "isDone": False, "remindAt": {"gt": today_to}}, order={"remindAt": "asc"}, take=4)

    def view(r) -> dict:
        return {"id": r.id, "title": r.title, "notes": r.notes, "remindAt": life.iso_ms(r.remindAt),
                "snoozedUntil": life.iso_ms(r.snoozedUntil) if r.snoozedUntil else None, "isRecurring": bool(r.rrule), "isDeadline": r.isDeadline,
                "isOverdue": r.remindAt < ctx.now, "daysUntil": diff_days_key(ctx.today, day_key(r.remindAt, ctx.tz)),
                "entityType": r.entityType, "entityId": r.entityId, "link": f"/reminders?id={r.id}"}

    start_today = dates.start_of_day(ctx.now, ctx.tz)
    return {"overdue": [view(r) for r in due if r.remindAt < start_today], "today": [view(r) for r in due if r.remindAt >= start_today],
            "upcoming": [view(r) for r in upcoming]}


async def _finance(ctx) -> dict:
    tz, now = ctx.tz, ctx.now
    today_from, today_to = dates.start_of_day(now, tz), dates.end_of_day(now, tz)
    month_from, month_to = dates.start_of_month(now, tz), dates.end_of_month(now, tz)
    last_from = dates.start_of_month(month_from - timedelta(days=1), tz)
    last_to = month_from - timedelta(milliseconds=1)
    today_tx = await db.transaction.find_many(where={"userId": ctx.user_id, "type": "EXPENSE", "date": {"gte": today_from, "lte": today_to}},
                                              include={"category": True, "account": True}, order={"date": "desc"})
    month = await life.totals(ctx, month_from, month_to)
    last = await life.totals(ctx, last_from, last_to)
    by_category = await life.spending_by_category(ctx, month_from, month_to)
    items = [{"id": t.id, "title": t.title or (t.category.name if t.category else None) or shared(ctx.lang, "expense"), "amount": to_number(t.amount),
              "currency": t.currency, "amountDefault": ctx.fx.to_default(t.amount, t.currency), "date": life.iso_ms(t.date),
              "category": _brief(t.category, "id", "name", "color", "icon"), "account": _brief(t.account, "id", "name"),
              "link": f"/finance/transactions?id={t.id}"} for t in today_tx]
    y, m, d = (int(x) for x in ctx.today.split("-"))
    total = 0.0
    for t in items:
        total = total + t["amountDefault"]
    return {
        "today": {"total": round2(total), "count": len(items), "items": items[:8]},
        "monthly": {
            "month": ctx.today[:7], "income": month["income"], "expenses": month["expenses"], "savings": month["savings"],
            "savingsRate": js_round((month["savings"] / month["income"]) * 100) if month["income"] > 0 else None,
            "dailyAverage": round2(month["expenses"] / d), "projectedExpenses": round2((month["expenses"] / d) * days_in_month(y, m)),
            "topCategories": by_category[:4], "lastMonth": last,
            "change": {"income": pct_change(last["income"], month["income"]), "expenses": pct_change(last["expenses"], month["expenses"]),
                       "savings": round2(month["savings"] - last["savings"])},
        },
    }


async def _habits(ctx) -> dict:
    habits, logs = await life.habits_with_logs(ctx, add_days_key(ctx.today, -400))
    idx = index_habit_logs(logs)
    weekly = lambda h: h["frequency"] == "WEEKLY"  # noqa: E731
    items = [{"id": h["id"], "name": h["name"], "icon": h["icon"], "color": h["color"], "unit": h["unit"], "frequency": h["frequency"],
              "targetPerDay": h["targetPerDay"], "countToday": count_on(h, idx, ctx.today), "doneToday": is_done_on(h, idx, ctx.today),
              "currentStreak": current_streak(h, idx, ctx.today, ctx.week_starts_on, 400),
              "weekDone": done_in_week(h, idx, ctx.today, ctx.week_starts_on, ctx.today) if weekly(h) else None,
              "timesPerWeek": times_per_week(h) if weekly(h) else None, "link": f"/habits?id={h['id']}"}
             for h in habits if is_due_today(h, idx, ctx.today, ctx.week_starts_on)]
    return {"items": items, "done": sum(1 for h in items if h["doneToday"]), "total": len(items), "totalHabits": len(habits),
            "bestCurrentStreak": max((h["currentStreak"] for h in items), default=0)}


async def _deadlines(ctx) -> list[dict]:
    on, uid, tz, today = ctx.sections.__contains__, ctx.user_id, ctx.tz, ctx.today

    def d(n: int):
        return key_to_date(add_days_key(today, n))

    today_from = dates.start_of_day(ctx.now, tz)
    out = []
    if on("tasks"):
        for t in await db.task.find_many(where={"userId": uid, "workspaceId": None, "status": {"not": "COMPLETED"}, "priority": {"in": ["HIGH", "URGENT"]},
                                                "dueAt": {"gte": today_from, "lte": life.end_of_day_key(add_days_key(today, 7), tz)}},
                                         include={"project": True}, order={"dueAt": "asc"}, take=10):
            k = day_key(t.dueAt, tz)
            out.append({"id": f"task:{t.id}", "kind": "task", "title": t.title, "subtitle": t.project.name if t.project else None,
                        "date": life.iso_ms(t.dueAt) if t.hasDueTime else k, "daysUntil": diff_days_key(today, k), "priority": t.priority,
                        "link": f"/tasks?id={t.id}"})
    if on("projects"):
        for p in await db.project.find_many(where={"userId": uid, "workspaceId": None, "status": {"in": ["PLANNING", "ACTIVE", "ON_HOLD"]}, "deadline": {"gte": d(0), "lte": d(30)}}, take=10):
            k = date_only_key(p.deadline)
            out.append({"id": f"project:{p.id}", "kind": "project", "title": p.name, "subtitle": shared(ctx.lang, "projectDeadline"), "date": k,
                        "daysUntil": diff_days_key(today, k), "priority": None, "link": f"/projects/{p.id}"})
    if on("study"):
        for a in await db.assessment.find_many(where={"userId": uid, "status": {"in": ["NOT_STARTED", "IN_PROGRESS"]},
                                                      "dueAt": {"gte": today_from, "lte": life.end_of_day_key(add_days_key(today, 14), tz)}},
                                               include={"course": True}, take=10):
            k = day_key(a.dueAt, tz)
            out.append({"id": f"assessment:{a.id}", "kind": "assessment", "title": a.title,
                        "subtitle": f"{a.course.name} · {enum_label(ctx.lang, 'assessmentType', a.type)}", "date": life.iso_ms(a.dueAt),
                        "daysUntil": diff_days_key(today, k), "priority": "HIGH" if a.type == "EXAM" else None, "link": f"/study?id={a.course.id}"})
    if on("documents"):
        for doc in await db.document.find_many(where={"userId": uid, "expiryDate": {"gte": d(-30), "lte": d(60)}}, take=10):
            k = date_only_key(doc.expiryDate)
            days = diff_days_key(today, k)
            out.append({"id": f"document:{doc.id}", "kind": "document", "title": doc.name, "subtitle": shared(ctx.lang, "expired" if days < 0 else "expires"),
                        "date": k, "daysUntil": days, "priority": "HIGH" if days <= 14 else None, "link": f"/documents?id={doc.id}"})
    if on("maintenance"):
        for m in await db.maintenanceitem.find_many(where={"userId": uid, "isActive": True, "nextDueAt": {"lte": d(7)}}, take=10):
            k = date_only_key(m.nextDueAt)
            out.append({"id": f"maintenance:{m.id}", "kind": "maintenance", "title": m.title,
                        "subtitle": shared(ctx.lang, "areaMaintenance", area=enum_label(ctx.lang, "maintenanceArea", m.area)), "date": k,
                        "daysUntil": diff_days_key(today, k), "priority": None, "link": f"/maintenance?id={m.id}"})
    if on("travel"):
        for t in await db.trip.find_many(where={"userId": uid, "status": {"in": ["PLANNING", "BOOKED"]}, "startDate": {"gte": d(0), "lte": d(30)}}, take=5):
            k = date_only_key(t.startDate)
            out.append({"id": f"trip:{t.id}", "kind": "trip", "title": t.name, "subtitle": shared(ctx.lang, "tripTo", destination=t.destination),
                        "date": k, "daysUntil": diff_days_key(today, k), "priority": None, "link": f"/travel/{t.id}"})
    return sorted(out, key=lambda x: (x["daysUntil"], locale_key(x["date"])))


def _summary(c: dict, ctx) -> str:
    """"2 tasks, 1 event and 1 bill today." in the user's language."""
    lang, M, on = ctx.lang, MESSAGES[ctx.lang], ctx.sections.__contains__
    parts = []
    for section, count, forms in (("tasks", c["tasksToday"], "tasks"), ("calendar", c["eventsToday"], "events"), ("finance", c["billsToday"], "bills"),
                                  ("reminders", c["remindersToday"], "reminders"), ("habits", c["habitsLeft"], "habits")):
        if on(section) and count:
            parts.append(plural(lang, count, M[forms]))
    return fmt(M["summary"], list=join_list(lang, parts)) if parts else M["summaryEmpty"]


def _scoped_tasks(tasks: dict, project_ids: set[str], task_ids: set[str]) -> dict:
    """A focus mode limited to some projects / tasks: the home screen's task lists keep only those."""
    def keep(t: dict) -> bool:
        project = t.get("project") or {}
        return t["id"] in task_ids or project.get("id") in project_ids

    today = [t for t in tasks["todayTasks"]["items"] if keep(t)]
    overdue = [t for t in tasks["overdue"]["items"] if keep(t)]
    top = tasks["topPriorities"]
    return {
        "todayTasks": {"items": today, "total": len(today), "completed": sum(1 for t in today if t["status"] == "COMPLETED")},
        "overdue": {"count": len(overdue), "items": overdue},
        "topPriorities": {**top, "items": [t for t in top["items"] if keep(t)], "suggestions": [t for t in top["suggestions"] if keep(t)]},
    }


async def _challenges(user) -> dict | None:
    """Active (and upcoming) challenges; None when there are none, so the widget stays away."""
    from app.os import challenges as challenge_routes

    rows = await db.challenge.find_many(where={"userId": user.id, "isArchived": False}, include=challenge_routes._INCLUDE,
                                        order=[{"sortOrder": "asc"}, {"createdAt": "desc"}])
    items = [c for c in await challenge_routes._views(user, rows) if c["status"] != "completed"]
    if not items:
        return None
    keep = ("id", "name", "icon", "color", "status", "startDate", "endDate", "durationDays", "dayNumber", "daysLeft", "perfectDays",
            "currentStreak", "completionRate", "todayDone", "todayTotal")
    return {"items": [{k: c[k] for k in keep} for c in items[:4]], "total": len(items)}


async def _workouts(user) -> dict | None:
    """Today's planned workout and this week's sessions; None when the user has no plan or session."""
    from app.os import workouts as workout_routes

    if not await db.workoutplan.count(where={"userId": user.id}) and not await db.workoutsession.count(where={"userId": user.id}):
        return None
    s = await workout_routes.stats(user=user)
    last = s["weekly"][-1] if s["weekly"] else {"sessions": 0, "volumeKg": 0}
    return {"todayPlan": s["todayPlan"], "todaySession": s["todaySession"] and {k: s["todaySession"][k] for k in ("id", "name", "doneSets", "setCount", "completedAt")},
            "thisWeekSessions": s["thisWeekSessions"], "thisWeekVolumeKg": last["volumeKg"], "weekStreak": s["weekStreak"], "totalSessions": s["totalSessions"]}


@router.get("")
async def get_dashboard(user=Depends(plus_user)):
    ctx = await life.context(user)
    on = ctx.sections.__contains__
    fin = on("finance")
    tasks = await _tasks(ctx) if on("tasks") else None
    events = await _upcoming_events(ctx) if on("calendar") else None
    reminders = await _reminders(ctx) if on("reminders") else None
    bills = await life.upcoming_bills(ctx, 14) if fin else None
    finance = await _finance(ctx) if fin else None
    accounts = await life.account_balances(ctx) if fin else None
    budgets = await life.budgets(ctx) if fin else None
    birthdays = await life.upcoming_dates(ctx, 30) if on("people") else None
    goals = await life.goals(ctx) if on("goals") else None
    habits = await _habits(ctx) if on("habits") else None
    deadlines = await _deadlines(ctx)
    focus = active_focus(ctx.settings)
    if tasks and focus and (focus["projectIds"] or focus["taskIds"]):
        tasks = _scoped_tasks(tasks, set(focus["projectIds"]), set(focus["taskIds"]))
    try:
        challenges = await _challenges(user) if on("habits") else None
        workouts = await _workouts(user) if on("health") else None
    except Exception:  # noqa: BLE001 — the home screen shows without them rather than failing
        logging.getLogger("vournal.os").exception("dashboard: challenges/workouts widgets failed")
        challenges = workouts = None
    try:
        insights = await insights_for(user, ctx)
    except Exception:  # noqa: BLE001 — the home screen shows without insights rather than failing
        insights = []

    bills_week = [b for b in bills or [] if b["daysUntil"] <= 7]
    bills_today = [b for b in bills or [] if b["daysUntil"] <= 0]
    counts = {
        "tasksToday": sum(1 for t in tasks["todayTasks"]["items"] if t["status"] != "COMPLETED") if tasks else 0,
        "tasksDoneToday": tasks["todayTasks"]["completed"] if tasks else 0,
        "overdueTasks": tasks["overdue"]["count"] if tasks else 0,
        "eventsToday": events["todayCount"] if events else 0,
        "remindersToday": len(reminders["overdue"]) + len(reminders["today"]) if reminders else 0,
        "billsToday": len(bills_today),
        "billsThisWeek": len(bills_week),
        "birthdaysThisWeek": sum(1 for b in birthdays if b["daysUntil"] <= 7) if birthdays is not None else 0,
        "habitsLeft": habits["total"] - habits["done"] if habits else 0,
        "deadlinesThisWeek": sum(1 for d in deadlines if 0 <= d["daysUntil"] <= 7),
    }
    daily = None
    if tasks or habits:
        t = {"done": tasks["todayTasks"]["completed"], "total": tasks["todayTasks"]["total"]} if tasks else {"done": 0, "total": 0}
        h = {"done": habits["done"], "total": habits["total"]} if habits else {"done": 0, "total": 0}
        total, done = t["total"] + h["total"], t["done"] + h["done"]
        daily = {"done": done, "total": total, "percent": js_round((done / total) * 100) if total else None, "tasks": t, "habits": h}
    net_worth = life.net_worth(accounts) if accounts is not None else 0

    def bills_total(rows) -> float:
        s = 0.0
        for b in rows:
            s = s + b["amountDefault"]
        return round2(s)

    return {
        "generatedAt": life.iso_ms(ctx.now),
        "now": life.iso_ms(ctx.now),
        "today": ctx.today,
        "timezone": ctx.tz,
        "currency": ctx.fx.default_currency,
        "weekStartsOn": ctx.week_starts_on,
        "enabledSections": ctx.settings.enabledSections,
        "widgets": ctx.settings.dashboardWidgets,
        "counts": counts,
        "summary": _summary(counts, ctx),
        "dailyCompletion": daily,
        "topPriorities": tasks["topPriorities"] if tasks else None,
        "todayTasks": tasks["todayTasks"] if tasks else None,
        "overdueTasks": tasks["overdue"] if tasks else None,
        "upcomingEvents": {"items": events["items"], "todayCount": events["todayCount"]} if events else None,
        "reminders": reminders,
        "upcomingBills": {"items": bills[:10], "total": bills_total(b for b in bills if not b["isOverdue"] or b["isNext"]), "weekTotal": bills_total(bills_week),
                          "overdueCount": sum(1 for b in bills if b["isOverdue"])} if bills is not None else None,
        "todayExpenses": finance["today"] if finance else None,
        "monthlyFinance": finance["monthly"] if finance else None,
        "savings": {"netWorth": net_worth, "savingsThisMonth": finance["monthly"]["savings"], "savingsLastMonth": finance["monthly"]["lastMonth"]["savings"],
                    "savingsRate": finance["monthly"]["savingsRate"], "accountsCount": len(accounts or [])} if finance else None,
        "accounts": {"items": sorted(accounts, key=lambda a: -abs(a["balanceDefault"]))[:5], "total": net_worth, "count": len(accounts)}
        if accounts is not None else None,
        "budgets": {"items": ([b for b in budgets if b["period"] == "MONTHLY"] + [b for b in budgets if b["period"] == "YEARLY"])[:4],
                    "total": len(budgets), "overCount": sum(1 for b in budgets if b["isOver"])} if budgets is not None else None,
        "birthdays": {"items": birthdays[:6], "total": len(birthdays)} if birthdays is not None else None,
        "goals": {"items": goals[:5], "activeCount": len(goals),
                  "averageProgress": js_round(sum(g["progress"] for g in goals) / len(goals)) if goals else None} if goals is not None else None,
        "habits": habits,
        "challenges": challenges,
        "workouts": workouts,
        "deadlines": {"items": deadlines[:10], "total": len(deadlines)},
        "insights": {"items": insights[:3], "total": len(insights)},
    }
