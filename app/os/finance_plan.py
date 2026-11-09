"""Spending plan — pure maths (port of vournalOS finance/plan.ts). Amounts are in
the user's default currency; inputs and outputs keep NestJS's camelCase keys.

For the current pay period (payday → day before the next payday, or the month):
  free to spend = income − commitments (bills, subscriptions, installments, debts due) − savings
  left to spend = free to spend − everyday spending so far
  today's allowance = what was left this morning ÷ days left (today included)
With accounts (a "balance" given), the plan starts from the money the user has instead
(owner, 2026-11-09): left to spend = balance + income still expected − bills still to
pay − debt payoff still due − savings. The balance already holds what was received,
paid and spent so far. Money already moved to savings accounts this period
("savedThisPeriod") counts toward the savings target.
Debts due after this period (or undated) are paid off gradually, an even share per
period up to their target date. Extra (one-off) income first covers what regular
income cannot, then is split between paying debts early, savings and spending.
"""

import calendar
import math
from datetime import date

from app.os.common.jsparse import js_sum
from app.os.finance_forecast import add_days

_EPSILON = 2.220446049250313e-16  # Number.EPSILON


def _round2(n: float) -> float:
    """Math.round((n + Number.EPSILON) * 100) / 100 (not the money round2: halves go up)."""
    return math.floor((n + _EPSILON) * 100 + 0.5) / 100


def _diff_days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def resolve_period(today: str, payday: dict | None, mode: str) -> dict:
    """Payday → day before the next payday when a monthly payday is known and today
    sits between the two; otherwise the calendar month."""
    if mode == "auto" and payday and payday.get("last") and payday.get("next") and payday["last"] <= today < payday["next"]:
        if 25 <= _diff_days(payday["last"], payday["next"]) <= 35:
            return {"start": payday["last"], "end": add_days(payday["next"], -1), "basis": "payday"}
    y, m = int(today[:4]), int(today[5:7])
    return {"start": f"{today[:7]}-01", "end": f"{today[:7]}-{calendar.monthrange(y, m)[1]:02d}", "basis": "month"}


def _sum(lines) -> float:
    return _round2(js_sum(x["amount"] for x in lines))


def _pct(n: float) -> float:
    return min(100, max(0, n)) / 100


def build_plan(p: dict) -> dict:
    today, period = p["today"], p["period"]
    days_total = _diff_days(period["start"], period["end"]) + 1
    days_left = max(1, _diff_days(today, period["end"]) + 1)
    income_total = _sum(p["income"])
    extra_lines = [i for i in p["income"] if i.get("extra")]
    extra_income = _sum(extra_lines)
    regular_income = _round2(income_total - extra_income)
    commitments_total = _sum(p["commitments"])
    commitments_paid = _sum(c for c in p["commitments"] if c["done"])
    savings = p["savings"]
    if savings["mode"] == "percent":
        base_savings = _round2(regular_income * _pct(savings["value"]))
    elif savings["mode"] == "amount":
        base_savings = _round2(max(0, savings["value"]))
    else:
        base_savings = 0

    # ----- debts paid off over several periods: an even share from the period start to the target -----
    debts = []
    for d in p.get("debts") or []:
        if d["left"] + d["paidInPeriod"] <= 0.005:
            continue
        at_start = d["left"] + d["paidInPeriod"]
        span = max(1, _diff_days(period["start"], d["target"]) + 1)
        share = at_start if d["target"] <= period["end"] else min(at_start, (at_start * days_total) / span)
        debts.append({"d": d, "atStart": at_start, "perDay": _round2(at_start / span), "share": _round2(share), "extra": 0})
    debts.sort(key=lambda x: x["d"]["target"])
    debt_needs = _sum({"amount": max(x["share"], x["d"]["paidInPeriod"])} for x in debts)

    # ----- extra income: cover what regular income cannot, then split -----
    extra = None
    extra_savings = 0
    if extra_income > 0:
        split = p.get("extraSplit") or {"debts": 0, "savings": 0}
        regular_free = regular_income - commitments_total - base_savings - debt_needs
        cover = _round2(min(extra_income, max(0, -regular_free)))
        rest = extra_income - cover
        to_debts = _round2(rest * _pct(split["debts"]))
        given = 0
        for x in debts:
            room = _round2(max(0, x["atStart"] - max(x["share"], x["d"]["paidInPeriod"])))
            x["extra"] = _round2(min(room, to_debts - given))
            given = _round2(given + x["extra"])
        # nothing left to pay early: that part goes to savings
        extra_savings = _round2(min(rest - given, rest * _pct(split["savings"]) + (to_debts - given)))
        to_debts = given
        spend = _round2(rest - to_debts - extra_savings)
        latest = sorted(extra_lines, key=lambda i: i["date"], reverse=True)[0]
        extra = {
            "total": extra_income,
            "cover": cover,
            "debts": to_debts,
            "savings": extra_savings,
            "spend": spend,
            "perDay": _round2(spend / days_left),
            "latest": {
                "title": latest["title"], "amount": _round2(latest["amount"]), "date": latest["date"],
                **({"link": latest["link"]} if latest.get("link") else {}),
            },
        }

    debt_plan = []
    for x in debts:
        d = x["d"]
        target = max(x["share"] + x["extra"], d["paidInPeriod"])
        debt_plan.append({
            "id": d["id"],
            "title": d["title"],
            "left": _round2(d["left"]),
            "target": d["target"],
            "perDay": x["perDay"],
            "share": x["share"],
            "extra": x["extra"],
            "paid": _round2(d["paidInPeriod"]),
            "remaining": _round2(max(0, target - d["paidInPeriod"])),
            **({"link": d["link"]} if d.get("link") else {}),
        })
    debt_plan_paid = _sum({"amount": x["paid"]} for x in debt_plan)
    debt_plan_total = _round2(debt_plan_paid + _sum({"amount": x["remaining"]} for x in debt_plan))

    savings_target = _round2(base_savings + extra_savings)
    income_received = _sum(i for i in p["income"] if i["done"])
    expected_income = _round2(income_total - income_received)
    saved = _round2(max(0, p.get("savedThisPeriod") or 0))
    savings_due = _round2(max(0, savings_target - saved))
    still_needed = _round2(commitments_total - commitments_paid + savings_due + (debt_plan_total - debt_plan_paid))
    balance = p.get("balance")
    if balance is None:
        free_to_spend = _round2(income_total - commitments_total - savings_target - debt_plan_total)
        left_to_spend = _round2(free_to_spend - p["everydaySpent"])
    else:
        balance = _round2(balance)
        left_to_spend = _round2(balance + expected_income - still_needed)
        free_to_spend = _round2(left_to_spend + p["everydaySpent"])
    # fixed for the whole day: what was left this morning, spread over the days that remain
    left_this_morning = left_to_spend + p["spentToday"]
    daily_allowance = _round2(max(0, left_this_morning / days_left))
    today_left = _round2(daily_allowance - p["spentToday"])
    from_tomorrow = _round2(max(0, left_to_spend / (days_left - 1))) if days_left > 1 else 0
    if balance is None and income_total <= 0:
        status = "noIncome"
    elif left_to_spend < 0:
        status = "over"
    elif today_left < 0:
        status = "overToday"
    else:
        status = "ok"

    # ----- what to keep aside -----
    if balance is None:
        cash_on_hand = _round2(income_received - commitments_paid - debt_plan_paid - p["everydaySpent"])
    else:
        cash_on_hand = balance
    keep_aside = _round2(max(0, min(cash_on_hand, still_needed)))
    gap = _round2(max(0, still_needed - max(0, cash_on_hand)))
    advice = {
        "stillNeeded": still_needed,
        "cashOnHand": cash_on_hand,
        "keepAside": keep_aside,
        "gap": gap,
        # rounded up so the daily amounts add up to at least the gap
        "setAsidePerDay": math.ceil((gap / days_left) * 100 - 1e-6) / 100,
        "expectedIncome": expected_income,
        "incomeShare": math.floor(min(1, gap / expected_income) * 1000 + 0.5) / 1000 if expected_income > 0 else None,
        "shortfall": _round2(max(0, gap - expected_income)),
    }
    # what the savings accounts could cover of what the spending accounts cannot
    advice["fromSavings"] = _round2(min(max(0, p.get("savingsBalance") or 0), advice["shortfall"]))

    return {
        "basis": "income" if balance is None else "balance",
        "balance": balance,
        "daysTotal": days_total,
        "daysLeft": days_left,
        "incomeTotal": income_total,
        "incomeReceived": income_received,
        "regularIncome": regular_income,
        "extraIncome": extra_income,
        "commitmentsTotal": commitments_total,
        "commitmentsPaid": commitments_paid,
        "savingsTarget": savings_target,
        "savingsPaid": saved,
        "savingsBalance": p.get("savingsBalance"),
        "debtPlanTotal": debt_plan_total,
        "debtPlanPaid": debt_plan_paid,
        "debtPlan": debt_plan,
        "freeToSpend": free_to_spend,
        "everydaySpent": _round2(p["everydaySpent"]),
        "leftToSpend": left_to_spend,
        "dailyAllowance": daily_allowance,
        "spentToday": _round2(p["spentToday"]),
        "todayLeft": today_left,
        "fromTomorrow": from_tomorrow,
        "status": status,
        "advice": advice,
        "extra": extra,
    }


_BYDAY = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]


def schedule_rrule(s: dict) -> str:
    """Income schedule → RRULE body."""
    if s["type"] == "monthly":
        return f"FREQ=MONTHLY;BYMONTHDAY={s['day']}"
    if s["type"] == "weekly":
        return f"FREQ=WEEKLY;BYDAY={_BYDAY[s['weekday']]}"
    days = sorted({d for d in s["weekdays"] if 0 <= d <= 6})
    return "FREQ=DAILY" if len(days) in (0, 7) else "FREQ=WEEKLY;BYDAY=" + ",".join(_BYDAY[d] for d in days)
