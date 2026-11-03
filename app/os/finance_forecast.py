"""Cash-flow forecast — pure maths (port of vournalOS finance/forecast.ts).

From today's total balance it walks forward day by day, applying scheduled money
movements (recurring bills / subscriptions, recurring income and expenses, dated
debts) and an "everyday spending" estimate (the recent average of non-recurring
expenses). Two lines come out: `committed` (scheduled items only) and `balance`
(scheduled + everyday estimate). Amounts are in the user's default currency.
"""

import calendar
from datetime import date, timedelta

from app.os.finance_utils import round2


def add_days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _last_day_of_month(day: str) -> str:
    d = date.fromisoformat(day)
    return d.replace(day=calendar.monthrange(d.year, d.month)[1]).isoformat()


def build_forecast(*, today: str, days, start_balance: float, daily_variable: float, items: list[dict]) -> dict:
    days = max(1, min(366, int(days // 1)))
    daily_variable = max(0, daily_variable)
    end = add_days(today, days)

    # normalise: overdue items are due today; drop anything after the horizon
    normalised = [{**i, "date": today, "overdue": True} if i["date"] < today else i for i in items]
    kept = sorted(
        (i for i in normalised if i["date"] <= end and i["amount"] != 0),
        key=lambda i: (i["date"], -i["amount"]),
    )

    by_day: dict[str, dict] = {}
    for i in kept:
        slot = by_day.setdefault(i["date"], {"income": 0, "out": 0})
        if i["amount"] > 0:
            slot["income"] += i["amount"]
        else:
            slot["out"] += -i["amount"]

    series = []
    balance = committed = start_balance
    totals = {"income": 0, "scheduledOut": 0, "variable": 0}
    for n in range(days + 1):
        day = add_days(today, n)
        slot = by_day.get(day, {"income": 0, "out": 0})
        variable = 0 if n == 0 else daily_variable  # today's own spending is already in the balance
        committed += slot["income"] - slot["out"]
        balance += slot["income"] - slot["out"] - variable
        totals["income"] += slot["income"]
        totals["scheduledOut"] += slot["out"]
        totals["variable"] += variable
        series.append({
            "date": day,
            "balance": round2(balance),
            "committed": round2(committed),
            "income": round2(slot["income"]),
            "scheduledOut": round2(slot["out"]),
            "variable": round2(variable),
        })

    lowest = series[0]
    for p in series:
        if p["balance"] < lowest["balance"]:
            lowest = p
    negative = next((p for p in series if p["balance"] < 0), None)
    eom_key = _last_day_of_month(today)
    eom = None if eom_key == today else next((p for p in series if p["date"] == eom_key), None)
    income = next((i for i in kept if i["amount"] > 0), None)

    return {
        "today": today,
        "days": days,
        "startBalance": round2(start_balance),
        "dailyVariable": round2(daily_variable),
        "series": series,
        "items": kept,
        "endBalance": series[-1]["balance"],
        "endOfMonth": {"date": eom["date"], "balance": eom["balance"], "committed": eom["committed"]} if eom else None,
        "lowest": {"date": lowest["date"], "balance": lowest["balance"]},
        "negativeOn": negative["date"] if negative else None,
        "safeToSpend": round2(max(0, lowest["balance"])),
        "nextIncome": {"date": income["date"], "amount": round2(income["amount"]), "title": income["title"]} if income else None,
        "totals": {k: round2(v) for k, v in totals.items()},
    }


def average_daily_spend(total: float, history_days: int, min_days: int = 14) -> float:
    """Average non-recurring spending per day; 0 with too little history."""
    if history_days < min_days or total <= 0:
        return 0
    return round2(total / history_days)
