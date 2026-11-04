"""/api/insights (port of vournalOS InsightsService): rule-based insights, no AI. The data
each rule needs is loaded only for the user's enabled sections, then the pure rules in
insight_rules.py run."""

from datetime import timedelta

from fastapi import Depends

from app.db import db
from app.os import app_todos
from app.os import life_data as life
from app.os.common import dates
from app.os.common.deps import plus_user
from app.os.common.http import nest_router
from app.os.insight_habits import best_streak_in_range, current_streak, index_habit_logs, is_done_on, is_due_today
from app.os.insight_rules import run_insight_rules
from app.os.insight_text import shared
from app.os.insight_time import add_days_key, date_only_key, day_key, days_in_month, diff_days_key, key_to_date, round2

router = nest_router("/insights")


async def build_input(ctx: life.LifeContext) -> dict:
    y, m, d = (int(x) for x in ctx.today.split("-"))
    i = {"lang": ctx.lang, "today": ctx.today, "currency": ctx.fx.default_currency, "dayOfMonth": d, "daysInMonth": days_in_month(y, m),
         "sections": list(ctx.settings.enabledSections)}
    on = ctx.sections.__contains__
    if on("finance"):
        i["finance"] = await _finance(ctx, d)
    if on("tasks"):
        i["tasks"] = await _tasks(ctx)
    if on("documents"):
        docs = await db.document.find_many(where={"userId": ctx.user_id, "expiryDate": {
            "not": None, "gte": key_to_date(add_days_key(ctx.today, -90)), "lte": key_to_date(add_days_key(ctx.today, 60))}},
            order={"expiryDate": "asc"})
        i["documents"] = [{"id": x.id, "name": x.name, "daysUntil": diff_days_key(ctx.today, date_only_key(x.expiryDate))} for x in docs]
    if on("maintenance"):
        items = await db.maintenanceitem.find_many(where={"userId": ctx.user_id, "isActive": True,
                                                          "nextDueAt": {"lte": key_to_date(add_days_key(ctx.today, 7))}}, order={"nextDueAt": "asc"})
        i["maintenance"] = [{"id": x.id, "title": x.title, "daysUntil": diff_days_key(ctx.today, date_only_key(x.nextDueAt))} for x in items]
    if on("people"):
        i["dates"] = await life.upcoming_dates(ctx, 14)
    if on("habits"):
        i["habits"] = await _habits(ctx)
    if on("goals"):
        i["goals"] = await life.goals(ctx)
    if on("projects"):
        i["projects"] = await life.projects_progress(ctx, {"status": {"in": ["PLANNING", "ACTIVE", "ON_HOLD"]}, "deadline": {"not": None}})
    return i


async def _finance(ctx: life.LifeContext, day_of_month: int) -> dict:
    tz, now = ctx.tz, ctx.now
    month_from = dates.start_of_month(now, tz)
    last_month_from = dates.start_of_month(month_from - timedelta(days=1), tz)
    last_key = day_key(last_month_from, tz)
    last_days = days_in_month(int(last_key[:4]), int(last_key[5:7]))
    # last month over the same elapsed days, clamped to its length
    last_same_to = life.end_of_day_key(add_days_key(last_key, min(day_of_month, last_days) - 1), tz)
    month_to = dates.end_of_day(now, tz)
    ninety_ago = life.start_of_day_key(add_days_key(ctx.today, -90), tz)
    seven_ago = life.start_of_day_key(add_days_key(ctx.today, -7), tz)
    this_cats = await life.spending_by_category(ctx, month_from, month_to)
    last_cats = await life.spending_by_category(ctx, last_month_from, last_same_to)
    this_totals = await life.totals(ctx, month_from, month_to)
    last_totals = await life.totals(ctx, last_month_from, last_same_to)
    budgets = await life.budgets(ctx)
    bills = await life.upcoming_bills(ctx, 7)
    subs = await life.recurring_monthly(ctx)
    last_expense = await db.transaction.find_first(where={"userId": ctx.user_id, "type": "EXPENSE", "date": {"lte": now}}, order={"date": "desc"})
    recent = await db.transaction.find_many(where={"userId": ctx.user_id, "type": "EXPENSE", "date": {"gte": ninety_ago, "lte": month_to}},
                                            include={"category": True})
    accounts = await life.account_balances(ctx)
    any_tx = await db.transaction.count(where={"userId": ctx.user_id, "type": {"in": ["INCOME", "EXPENSE"]}})
    # the average single expense leaves recurring payments out (rent would skew it)
    sample = [t for t in recent if not t.recurringPaymentId]
    values = [ctx.fx.to_default(t.amount, t.currency) for t in sample]
    total = 0.0
    for v in values:
        total = total + v
    avg = total / len(values) if values else None
    return {
        "hasTransactions": any_tx > 0,
        "thisMonthByCategory": this_cats,
        "lastMonthSamePeriodByCategory": last_cats,
        "incomeThisMonth": this_totals["income"],
        "expensesThisMonth": this_totals["expenses"],
        "incomeLastMonthSamePeriod": last_totals["income"],
        "expensesLastMonthSamePeriod": last_totals["expenses"],
        "budgets": budgets,
        "upcomingBills": bills,
        "subscriptionsMonthly": subs["subscriptionsMonthly"],
        "subscriptionCount": subs["subscriptionCount"],
        "daysSinceLastExpense": diff_days_key(day_key(last_expense.date, tz), ctx.today) if last_expense else None,
        "recentExpenses": [{"id": t.id, "title": t.title or (t.category.name if t.category else None) or shared(ctx.lang, "expense"),
                            "amount": round2(ctx.fx.to_default(t.amount, t.currency)), "daysAgo": diff_days_key(day_key(t.date, tz), ctx.today)}
                           for t in sample if t.date >= seven_ago],
        "averageExpense": None if avg is None else round2(avg),
        "expenseSampleSize": len(values),
        "accounts": [{"id": a["id"], "name": a["name"], "type": a["type"], "balance": a["balance"], "currency": a["currency"]} for a in accounts],
    }


async def _tasks(ctx: life.LifeContext) -> dict:
    tz, now, uid = ctx.tz, ctx.now, ctx.user_id
    week_from = dates.start_of_week(now, tz, ctx.week_starts_on)
    last_week_from = dates.add_days(week_from, -7, tz)
    last_week_same_to = dates.add_days(now, -7, tz)
    today_from, today_to = dates.start_of_day(now, tz), dates.end_of_day(now, tz)
    overdue = life.overdue_task_where(uid, now, today_from)
    oldest = await db.task.find_first(where=overdue, order={"dueAt": "asc"})
    oldest_day = day_key(oldest.dueAt, tz) if oldest and oldest.dueAt else None
    todos = await app_todos.task_counts(uid, tz, now)  # the app's to-dos count too
    if todos["oldestOverdueDay"] and (oldest_day is None or todos["oldestOverdueDay"].isoformat() < oldest_day):
        oldest_day = todos["oldestOverdueDay"].isoformat()
    return {
        "overdueCount": await db.task.count(where=overdue) + todos["overdue"],
        "oldestOverdueDays": diff_days_key(oldest_day, ctx.today) if oldest_day else None,
        "dueTodayCount": await db.task.count(where={"userId": uid, "status": {"not": "COMPLETED"}, "dueAt": {"gte": today_from, "lte": today_to}})
        + todos["dueToday"],
        "completedThisWeek": await db.task.count(where={"userId": uid, "status": "COMPLETED", "completedAt": {"gte": week_from, "lte": now}}),
        "completedLastWeekSamePeriod": await db.task.count(where={"userId": uid, "status": "COMPLETED",
                                                                  "completedAt": {"gte": last_week_from, "lte": last_week_same_to}}),
    }


async def _habits(ctx: life.LifeContext) -> list[dict]:
    start = add_days_key(ctx.today, -400)
    habits, logs = await life.habits_with_logs(ctx, start)
    idx = index_habit_logs(logs)
    return [{"id": h["id"], "name": h["name"], "frequency": h["frequency"],
             "dueToday": is_due_today(h, idx, ctx.today, ctx.week_starts_on), "doneToday": is_done_on(h, idx, ctx.today),
             "currentStreak": current_streak(h, idx, ctx.today, ctx.week_starts_on, 400),
             "bestStreak": best_streak_in_range(h, idx, start, ctx.today, ctx.week_starts_on)} for h in habits]


async def insights_for(user, ctx: life.LifeContext | None = None) -> list[dict]:
    """Sorted danger → warning → success → info, then by importance; the score stays inside."""
    ctx = ctx or await life.context(user)
    return [{k: v for k, v in ins.items() if k != "score"} for ins in run_insight_rules(await build_input(ctx))]


@router.get("")
async def list_insights(user=Depends(plus_user)):
    return await insights_for(user)
