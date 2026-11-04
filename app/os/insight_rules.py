"""Rule-based insights, no AI (port of vournalOS insights/lib/rules.ts). Every rule is a
pure function over plain data; InsightsService loads the data and runs run_insight_rules.
The words are rules.messages.ts (app/os/data/insights_i18n.json), in the user's language."""

import math

from app.os.common.collation import locale_key
from app.os.common.i18n import fmt, plural
from app.os.insight_text import MESSAGES, js_round, money, num, percent, rel_day, shared

SEVERITY_RANK = {"danger": 0, "warning": 1, "success": 2, "info": 3}
STREAK_MILESTONES = (7, 14, 21, 30, 50, 75, 100, 150, 200, 250, 300, 365, 500, 730, 1000)


class _Ctx:
    """The language, its messages and formatters bound for one run."""

    def __init__(self, i: dict):
        self.lang = i.get("lang") or "en"
        self.M = MESSAGES[self.lang]
        self.currency = i["currency"]

    def money(self, amount: float, currency: str | None = None) -> str:
        return money(self.lang, amount, currency or self.currency)

    def pct(self, v: float) -> str:
        return percent(self.lang, v)

    def n(self, v) -> str:
        return num(self.lang, v)

    def when(self, days: int) -> str:
        return rel_day(self.lang, days)

    def p(self, count, forms: dict, **values) -> str:
        return plural(self.lang, count, forms, **values)


def _insight(id, severity, icon, title, detail, link, section, score) -> dict:
    return {"id": id, "severity": severity, "icon": icon, "title": title, "detail": detail, "link": link, "section": section, "score": score}


def _by_score(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: -x["score"])


# ----- finance -------------------------------------------------------------------------


def category_spending_change(i):
    """"You spent 32% more on Shopping this month than last month" (same days of the month)."""
    f = i.get("finance")
    if not f or i["dayOfMonth"] < 3:
        return []
    c = _Ctx(i)
    last = {x["categoryId"] or "none": x for x in f["lastMonthSamePeriodByCategory"]}
    total = max(f["expensesThisMonth"], f["expensesLastMonthSamePeriod"], 1)
    ups, downs = [], []
    for cat in f["thisMonthByCategory"]:
        key = cat["categoryId"] or "none"
        prev = last.get(key)
        if not prev or prev["amount"] <= 0:
            continue
        change = ((cat["amount"] - prev["amount"]) / prev["amount"]) * 100
        diff = cat["amount"] - prev["amount"]
        if abs(diff) < total * 0.03:
            continue  # ignore tiny categories
        detail = fmt(c.M["catDetail"], now=c.money(cat["amount"]), prev=c.money(prev["amount"]))
        if change >= 25:
            ups.append(_insight(f"category-up:{key}", "warning" if change >= 60 else "info", "trending-up",
                                fmt(c.M["catUpTitle"], pct=c.pct(change), name=cat["name"]), detail, "/finance/transactions", "finance", min(100, change)))
        elif change <= -25:
            downs.append(_insight(f"category-down:{key}", "success", "trending-down",
                                  fmt(c.M["catDownTitle"], pct=c.pct(-change), name=cat["name"]), detail, "/finance/transactions", "finance", min(100, -change)))
    return [*_by_score(ups)[:2], *_by_score(downs)[:1]]


def budget_alerts(i):
    """Over budget (danger) or past its alert threshold (warning)."""
    f = i.get("finance")
    if not f:
        return []
    c = _Ctx(i)
    out = []
    for b in f["budgets"]:
        if b["amount"] <= 0:
            continue
        yearly = b["period"] == "YEARLY"
        if b["spent"] > b["amount"]:
            out.append(_insight(
                f"budget-over:{b['id']}", "danger", "circle-alert", fmt(c.M["budgetOverTitle"], name=b["name"]),
                fmt(c.M["budgetOverDetailYear" if yearly else "budgetOverDetailMonth"], spent=c.money(b["spent"], b["currency"]),
                    amount=c.money(b["amount"], b["currency"]), over=c.money(b["spent"] - b["amount"], b["currency"])),
                "/finance/budgets", "finance", 50 + min(50, b["percentUsed"] - 100)))
        elif b["percentUsed"] >= b["alertThreshold"]:
            out.append(_insight(
                f"budget-near:{b['id']}", "warning", "gauge", fmt(c.M["budgetNearTitle"], pct=c.pct(b["percentUsed"]), name=b["name"]),
                fmt(c.M["budgetNearDetailYear" if yearly else "budgetNearDetailMonth"], left=c.money(b["amount"] - b["spent"], b["currency"])),
                "/finance/budgets", "finance", b["percentUsed"] / 2))
    return out


def upcoming_recurring_payments(i):
    """Overdue bills (danger) and the ones due in the next 7 days."""
    f = i.get("finance")
    if not f:
        return []
    c = _Ctx(i)
    out = []
    overdue = [b for b in f["upcomingBills"] if b["isOverdue"]]
    if overdue:
        total = sum(b["amountDefault"] for b in overdue)
        title = fmt(c.M["billsOverdueOne"], name=overdue[0]["name"]) if len(overdue) == 1 else c.p(len(overdue), c.M["billsOverdueMany"])
        out.append(_insight("bills-overdue", "danger", "receipt", title, fmt(c.M["billsOverdueDetail"], total=c.money(total)),
                            "/finance/recurring", "finance", 80))
    soon = [b for b in f["upcomingBills"] if not b["isOverdue"] and 0 <= b["daysUntil"] <= 7]
    if soon:
        total = sum(b["amountDefault"] for b in soon)
        detail = (fmt(c.M["billsUpcomingDetailNames"], total=c.money(total), names=shared(c.lang, "listSeparator").join(b["name"] for b in soon))
                  if len(soon) <= 3 else fmt(c.M["billsUpcomingDetail"], total=c.money(total)))
        out.append(_insight("bills-upcoming", "info", "calendar-clock", c.p(len(soon), c.M["billsUpcomingTitle"]), detail,
                            "/finance/recurring", "finance", 40 + len(soon)))
    return out


def savings_trend(i):
    """Negative savings this month, and savings up or down against last month."""
    f = i.get("finance")
    if not f or not f["hasTransactions"] or i["dayOfMonth"] < 3:
        return []
    c = _Ctx(i)
    out = []
    now = f["incomeThisMonth"] - f["expensesThisMonth"]
    prev = f["incomeLastMonthSamePeriod"] - f["expensesLastMonthSamePeriod"]
    if now < 0:
        out.append(_insight("savings-negative", "danger", "piggy-bank", c.M["savingsNegativeTitle"],
                            fmt(c.M["savingsNegativeDetail"], amount=c.money(-now)), "/finance", "finance", 70))
    if f["incomeLastMonthSamePeriod"] > 0 or f["expensesLastMonthSamePeriod"] > 0:
        diff = now - prev
        threshold = max(abs(prev) * 0.1, 1)
        detail = fmt(c.M["savingsDetail"], now=c.money(now), prev=c.money(prev))
        if diff > threshold and now > 0:
            out.append(_insight("savings-up", "success", "trending-up", c.M["savingsUpTitle"], detail, "/finance", "finance", 60))
        elif diff < -threshold and now >= 0:
            out.append(_insight("savings-down", "warning", "trending-down", c.M["savingsDownTitle"], detail, "/finance", "finance", 40))
    return out


def no_recent_expenses(i):
    f = i.get("finance")
    if not f or not f["hasTransactions"] or f["daysSinceLastExpense"] is None or f["daysSinceLastExpense"] < 5:
        return []
    c = _Ctx(i)
    return [_insight("no-expenses", "info", "notebook-pen", c.p(f["daysSinceLastExpense"], c.M["noExpensesTitle"]), c.M["noExpensesDetail"],
                     "/finance/transactions", "finance", 20)]


def large_expense(i):
    """An expense over 3× the 90-day average single expense."""
    f = i.get("finance")
    if not f or not f["averageExpense"] or f["expenseSampleSize"] < 5:
        return []
    c = _Ctx(i)
    big = sorted((e for e in f["recentExpenses"] if e["amount"] > 3 * f["averageExpense"] and e["amount"] > 0), key=lambda e: -e["amount"])
    if not big:
        return []
    e = big[0]
    return [_insight(f"large-expense:{e['id']}", "warning", "badge-dollar-sign", fmt(c.M["largeExpenseTitle"], title=e["title"]),
                     c.p(js_round(e["amount"] / f["averageExpense"]), c.M["largeExpenseDetail"], amount=c.money(e["amount"]), when=c.when(-e["daysAgo"])),
                     f"/finance/transactions?id={e['id']}", "finance", 30)]


def subscriptions_total(i):
    f = i.get("finance")
    if not f or f["subscriptionCount"] == 0 or f["subscriptionsMonthly"] <= 0:
        return []
    c = _Ctx(i)
    return [_insight("subscriptions-total", "info", "repeat", c.p(f["subscriptionCount"], c.M["subsTitle"], amount=c.money(f["subscriptionsMonthly"])),
                     fmt(c.M["subsDetail"], yearly=c.money(f["subscriptionsMonthly"] * 12)), "/finance/subscriptions", "finance", 10)]


def low_account_balance(i):
    """Credit cards owed and overdrawn accounts."""
    f = i.get("finance")
    if not f:
        return []
    c = _Ctx(i)
    out = []
    for a in sorted((a for a in f["accounts"] if a["balance"] < 0), key=lambda a: a["balance"])[:2]:
        card = a["type"] == "CREDIT_CARD"
        title = fmt(c.M["creditOweTitle"], amount=c.money(-a["balance"], a["currency"]), name=a["name"]) if card else fmt(c.M["overdrawnTitle"], name=a["name"])
        out.append(_insight(f"account-negative:{a['id']}", "warning" if card else "danger", "credit-card" if card else "wallet", title,
                            fmt(c.M["balanceDetail"], balance=c.money(a["balance"], a["currency"])), "/finance/accounts", "finance", 45))
    return out


# ----- productivity --------------------------------------------------------------------


def overdue_tasks(i):
    t = i.get("tasks")
    if not t or t["overdueCount"] == 0:
        return []
    c = _Ctx(i)
    oldest = t["oldestOverdueDays"]
    return [_insight("tasks-overdue", "danger" if t["overdueCount"] >= 5 else "warning", "list-todo", c.p(t["overdueCount"], c.M["tasksOverdueTitle"]),
                     c.p(oldest, c.M["tasksOverdueOldest"]) if oldest and oldest > 1 else c.M["tasksOverdueDetail"],
                     "/tasks?view=overdue", "tasks", 50 + min(40, t["overdueCount"] * 5))]


def weekly_productivity(i):
    """Tasks completed this week against last week, over the same elapsed days."""
    t = i.get("tasks")
    if not t:
        return []
    c = _Ctx(i)
    now, prev = t["completedThisWeek"], t["completedLastWeekSamePeriod"]
    if now == 0 and prev == 0:
        return []
    if now > prev:
        detail = fmt(c.M["tasksWeekMore"], diff=c.n(now - prev)) if prev > 0 else c.M["tasksWeekMomentum"]
        return [_insight("tasks-week-up", "success", "circle-check-big", c.p(now, c.M["tasksWeekTitle"]), detail, "/reports?tab=weekly", "tasks",
                         30 + min(30, now))]
    if prev >= 3 and now < prev * 0.6:
        return [_insight("tasks-week-down", "info", "list-checks", c.p(now, c.M["tasksWeekTitle"]), fmt(c.M["tasksWeekDown"], prev=c.n(prev)),
                         "/reports?tab=weekly", "tasks", 15)]
    return []


def project_deadline_risk(i):
    if i.get("projects") is None:
        return []
    c = _Ctx(i)
    risky = sorted((p for p in i["projects"] if p["daysLeft"] is not None and 0 <= p["daysLeft"] <= 14 and p["progress"] < 70),
                   key=lambda p: p["daysLeft"] or 0)[:2]
    return [_insight(f"project-risk:{p['id']}", "danger" if p["daysLeft"] <= 7 and p["progress"] < 50 else "warning", "folder-kanban",
                     fmt(c.M["projectRiskTitle"], name=p["name"], when=c.when(p["daysLeft"]), pct=c.pct(p["progress"])), c.M["projectRiskDetail"],
                     f"/projects/{p['id']}", "projects", 60 - p["daysLeft"]) for p in risky]


# ----- life admin ----------------------------------------------------------------------


def document_expiry(i):
    if i.get("documents") is None:
        return []
    c = _Ctx(i)
    out = []
    for d in i["documents"]:
        if d["daysUntil"] < 0:
            out.append(_insight(f"document-expired:{d['id']}", "danger", "file-x", fmt(c.M["docExpiredTitle"], name=d["name"]),
                                fmt(c.M["docExpiredDetail"], when=c.when(d["daysUntil"])), f"/documents?id={d['id']}", "documents", 70))
        elif d["daysUntil"] <= 60:
            out.append(_insight(f"document-expiring:{d['id']}", "danger" if d["daysUntil"] <= 14 else "warning", "file-clock",
                                fmt(c.M["docExpiringTitle"], name=d["name"]), fmt(c.M["docExpiringDetail"], when=c.when(d["daysUntil"])),
                                f"/documents?id={d['id']}", "documents", 60 - d["daysUntil"] / 2))
    return out[:3]


def maintenance_due(i):
    if i.get("maintenance") is None:
        return []
    c = _Ctx(i)
    overdue = [m for m in i["maintenance"] if m["daysUntil"] < 0]
    if not overdue:
        return []
    one = len(overdue) == 1
    title = fmt(c.M["maintOverdueOne"], title=overdue[0]["title"]) if one else c.p(len(overdue), c.M["maintOverdueMany"])
    detail = (fmt(c.M["maintOverdueDetail"], when=c.when(overdue[0]["daysUntil"])) if one
              else shared(c.lang, "listSeparator").join(m["title"] for m in overdue[:3]) + (c.M["ellipsis"] if len(overdue) > 3 else ""))
    return [_insight("maintenance-overdue", "warning", "wrench", title, detail, "/maintenance", "maintenance", 40 + len(overdue))]


def upcoming_birthdays(i):
    if i.get("dates") is None:
        return []
    c = _Ctx(i)
    out = []
    for d in [d for d in i["dates"] if 0 <= d["daysUntil"] <= 7][:2]:
        birthday = d["kind"] == "birthday"
        icon = "cake" if birthday else "heart" if d["kind"] == "anniversary" else "calendar-heart"
        title = (fmt(c.M["birthdayTitle"], name=d["personName"], when=c.when(d["daysUntil"])) if birthday and d["personName"]
                 else fmt(c.M["dateTitle"], title=d["title"], when=c.when(d["daysUntil"])))
        detail = fmt(c.M["birthdayTurning"], age=c.n(d["turningAge"])) if birthday and d["turningAge"] else c.M["dateDetail"]
        out.append(_insight(f"date:{d['id']}", "info", icon, title, detail, d["link"], "people", 40 - d["daysUntil"] * 3))
    return out


# ----- habits and goals ----------------------------------------------------------------


def habit_streak_at_risk(i):
    if i.get("habits") is None:
        return []
    c = _Ctx(i)
    risky = sorted((h for h in i["habits"] if h["dueToday"] and not h["doneToday"] and h["currentStreak"] >= 3 and h["frequency"] != "WEEKLY"),
                   key=lambda h: -h["currentStreak"])[:2]
    return [_insight(f"habit-risk:{h['id']}", "warning", "flame", c.p(h["currentStreak"], c.M["habitRiskTitle"], name=h["name"]),
                     fmt(c.M["habitRiskDetail"], name=h["name"]), "/habits", "habits", 30 + min(40, h["currentStreak"])) for h in risky]


def habit_streak_milestone(i):
    if i.get("habits") is None:
        return []
    c = _Ctx(i)
    hits = sorted((h for h in i["habits"] if h["frequency"] != "WEEKLY" and h["currentStreak"] >= 7
                   and (h["currentStreak"] in STREAK_MILESTONES or (h["currentStreak"] == h["bestStreak"] and h["currentStreak"] >= 7))),
                  key=lambda h: -h["currentStreak"])[:1]
    return [_insight(f"habit-milestone:{h['id']}", "success", "trophy",
                     c.p(h["currentStreak"], c.M["habitMilestoneTitle"] if h["currentStreak"] in STREAK_MILESTONES else c.M["habitBestTitle"], name=h["name"]),
                     c.M["habitMilestoneDetail"], "/habits", "habits", 40 + min(40, h["currentStreak"] / 5)) for h in hits]


def all_habits_done(i):
    if i.get("habits") is None:
        return []
    c = _Ctx(i)
    due = [h for h in i["habits"] if h["dueToday"]]
    if len(due) < 2 or any(not h["doneToday"] for h in due):
        return []
    return [_insight("habits-all-done", "success", "sparkles", c.M["habitsAllDoneTitle"], c.p(len(due), c.M["habitsAllDoneDetail"]),
                     "/habits", "habits", 35)]


def goal_behind_schedule(i):
    if i.get("goals") is None:
        return []
    c = _Ctx(i)
    behind = [g for g in i["goals"] if g["timeProgress"] is not None and g["daysLeft"] is not None and g["daysLeft"] >= 0
              and g["timeProgress"] >= 20 and g["progress"] + 15 < g["timeProgress"]]
    behind.sort(key=lambda g: -(g["timeProgress"] - g["progress"]))
    return [_insight(f"goal-behind:{g['id']}", "warning", "target", fmt(c.M["goalBehindTitle"], name=g["name"]),
                     c.p(g["daysLeft"], c.M["goalBehindDetail"], progress=c.pct(g["progress"]), time=c.pct(g["timeProgress"])),
                     f"/goals/{g['id']}", "goals", 30 + (g["timeProgress"] - g["progress"]) / 2) for g in behind[:2]]


def financial_goal_monthly(i):
    """How much to put aside each month to reach a money goal by its deadline."""
    if i.get("goals") is None:
        return []
    c = _Ctx(i)
    out = []
    goals = sorted((g for g in i["goals"] if g["type"] == "FINANCIAL" and g["targetValue"] and g["targetValue"] > g["currentValue"]
                    and g["daysLeft"] is not None and g["daysLeft"] > 0), key=lambda g: g["daysLeft"] or 0)
    for g in goals[:2]:
        months = max(1, math.ceil(g["daysLeft"] / 30.44))
        remaining = g["targetValue"] - g["currentValue"]
        monthly = g.get("monthlyRequired")
        per_month = monthly if monthly is not None else remaining / months
        cur = g["currency"] or i["currency"]
        detail = (c.p(g["daysLeft"], c.M["goalMonthlyDetailDays"], remaining=c.money(remaining, cur)) if months == 1
                  else c.p(months, c.M["goalMonthlyDetailMonths"], remaining=c.money(remaining, cur)))
        out.append(_insight(f"goal-monthly:{g['id']}", "info", "piggy-bank",
                            fmt(c.M["goalMonthlyTitle"], amount=c.money(js_round(per_month * 100) / 100, cur), name=g["name"]), detail,
                            f"/goals/{g['id']}", "goals", 25))
    return out


def goal_almost_done(i):
    if i.get("goals") is None:
        return []
    c = _Ctx(i)
    return [_insight(f"goal-close:{g['id']}", "success", "flag", fmt(c.M["goalCloseTitle"], pct=c.pct(g["progress"]), name=g["name"]),
                     c.M["goalCloseDetail"], f"/goals/{g['id']}", "goals", 30) for g in [g for g in i["goals"] if 85 <= g["progress"] < 100][:1]]


ALL_RULES = (
    budget_alerts, upcoming_recurring_payments, category_spending_change, savings_trend, low_account_balance, large_expense,
    no_recent_expenses, subscriptions_total, overdue_tasks, weekly_productivity, project_deadline_risk, document_expiry,
    maintenance_due, upcoming_birthdays, habit_streak_at_risk, habit_streak_milestone, all_habits_done, goal_behind_schedule,
    financial_goal_monthly, goal_almost_done,
)


def sort_insights(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (SEVERITY_RANK[x["severity"]], -x["score"], locale_key(x["id"])))


def run_insight_rules(i: dict, rules=ALL_RULES) -> list[dict]:
    """Every rule; insights of disabled sections dropped, ids de-duplicated, by severity."""
    enabled = set(i["sections"])
    seen: set[str] = set()
    out = []
    for rule in rules:
        try:
            produced = rule(i)
        except Exception:  # noqa: BLE001 — a rule that fails produces nothing, as in NestJS
            produced = []
        for ins in produced:
            if ins["section"] not in enabled or ins["id"] in seen:
                continue
            seen.add(ins["id"])
            out.append({**ins, "score": js_round(ins["score"] * 10) / 10})
    return sort_insights(out)
