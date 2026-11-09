"""The spending plan (port of vournalOS finance/plan.service.ts): what the user can
spend today from their income, bills, installments, debts and savings target for
the current pay period. Income is ordinary recurring INCOME transactions (rrule),
set up once and recorded automatically on each payday. The maths is finance_plan.
"""

import math
import re
from datetime import date, datetime, timedelta, timezone

from prisma import Json

from app.db import db
from app.os import finance_utils as fu
from app.os.accounts import with_balances
from app.os.calendar_utils import local_date_time
from app.os.common import dates, rrule
from app.os.common.jsparse import js_sum
from app.os.common.currency import get_converter, round_money, to_number
from app.os.common.http import nest_error
from app.os.common.serialize import _iso
from app.os.finance_forecast import add_days
from app.os.finance_plan import build_plan, resolve_period, schedule_rrule
from app.os.profile import ensure_os_profile
from app.os.transactions import materialize_due

DAY = timedelta(days=1)
_MS = timedelta(milliseconds=1)
DEFAULT_SETTINGS = {"savingsMode": "none", "savingsValue": 0, "periodMode": "auto", "debtPayoffMonths": 6, "extraDebtPct": 50, "extraSavingsPct": 20}
# a monthly salary (INTERVAL absent or 1) sets the pay period
_MONTHLY_PAY = re.compile(r"^FREQ=MONTHLY(?!.*INTERVAL=(?!1(?:;|$)))")
_NOT_DEBT = {"debtPayment": {"is": None}}


def _coalesce(*values):
    return next((v for v in values if v is not None), None)


def _date_only(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def _iso_day(moment: datetime) -> str:
    return dates._utc(moment).date().isoformat()


def _is_int(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and float(v).is_integer()


def read_settings(raw) -> dict:
    o = raw if isinstance(raw, dict) else {}

    def whole(v, low, high, default):
        return v if _is_int(v) and low <= v <= high else default

    extra_debt = whole(o.get("extraDebtPct"), 0, 100, DEFAULT_SETTINGS["extraDebtPct"])
    value = o.get("savingsValue")
    return {
        "savingsMode": o.get("savingsMode") if o.get("savingsMode") in ("percent", "amount") else DEFAULT_SETTINGS["savingsMode"],
        "savingsValue": value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else DEFAULT_SETTINGS["savingsValue"],
        "periodMode": "month" if o.get("periodMode") == "month" else "auto",
        "debtPayoffMonths": whole(o.get("debtPayoffMonths"), 1, 60, DEFAULT_SETTINGS["debtPayoffMonths"]),
        "extraDebtPct": extra_debt,
        "extraSavingsPct": min(100 - extra_debt, whole(o.get("extraSavingsPct"), 0, 100, DEFAULT_SETTINGS["extraSavingsPct"])),
    }


def add_months(day: str, n: int) -> str:
    """"YYYY-MM-DD" + n months, clamped to the end of the month."""
    y, m, d = (int(x) for x in day.split("-"))
    total = y * 12 + (m - 1) + n
    ty, tm = total // 12, total % 12 + 1
    last = fu.days_in_month(ty, tm)
    return date(ty, tm, min(d, last)).isoformat()


async def _sum_expenses(user_id: str, start: datetime, end: datetime, fx) -> float:
    """Everyday spending: expenses that are not bills, installments, recurring expenses or debt repayments."""
    rows = await db.transaction.find_many(
        where={"userId": user_id, "type": "EXPENSE", "recurringPaymentId": None, "rrule": None, **_NOT_DEBT, "date": {"gte": start, "lte": end}}
    )
    return round_money(js_sum(fx.to_default(t.amount, t.currency) for t in rows))


async def _balance_now(user_id: str, start: datetime, now: datetime, fx) -> float | None:
    """The money the user has now: the accounts they count in their total, plus what was
    received or spent without an account this period (it came in or out of that money
    too). None while no such account is in use (every account starts with an empty
    Cash account): the plan then works from income alone."""
    rows = await db.account.find_many(where={"userId": user_id, "isArchived": False, "includeInTotal": True})
    used = {a["id"] for a in await with_balances(user_id, rows, fx) if a["transactionCount"] or to_number(a["initialBalance"])}
    if not used:
        return None
    in_accounts = js_sum(a["balanceInDefault"] for a in await with_balances(user_id, [r for r in rows if r.id in used], fx, until=now))
    loose = await db.transaction.find_many(
        where={"userId": user_id, "accountId": None, "type": {"in": ["INCOME", "EXPENSE"]}, "date": {"gte": start, "lte": now}}
    )
    flows = js_sum(fx.to_default(t.amount, t.currency) * (1 if t.type == "INCOME" else -1) for t in loose)
    return round_money(in_accounts + flows)


def _kind_of(kind: str, is_plan: bool) -> str:
    if kind == "SUBSCRIPTION":
        return "subscription"
    return "installment" if kind == "LOAN" or is_plan else "bill"


def _left(debt) -> float:
    return max(0, to_number(debt.principal) - js_sum(to_number(p.amount) for p in debt.payments or []))


async def plan(user) -> dict:
    await materialize_due(user.id)
    settings = await ensure_os_profile(user)
    fx = await get_converter(user.id)
    prefs = read_settings(settings.spendingPlan)
    tz = fu.safe_tz(settings.timezone)
    now = datetime.now(timezone.utc)
    today = fu.day_key(now, tz)

    # ----- income sources (recurring INCOME series) and the payday they define -----
    heads = await db.transaction.find_many(
        where={"userId": user.id, "type": "INCOME", "rrule": {"not": None}}, include={"category": True}, order={"date": "asc"}
    )
    sources = []
    for h in heads:
        h_date = dates._utc(h.date)
        sources.append({
            "id": h.id,
            "title": _coalesce(h.title, h.source, h.category.name if h.category else None, ""),
            "amount": to_number(h.amount),
            "currency": h.currency,
            "rrule": h.rrule,
            "accountId": h.accountId,
            "lastDate": h.date if h_date <= now else None,
            "nextDate": h.date if h_date > now else rrule.next_occurrence(h.rrule, h.date, h.date, tz=tz),
            "monthlyAmount": fx.to_default(to_number(h.amount) * rrule.monthly_factor(h.rrule), h.currency),
        })
    primary = max(sources, key=lambda s: s["monthlyAmount"]) if sources else None
    payday = None
    if primary and _MONTHLY_PAY.search(primary["rrule"]):
        payday = {
            "last": fu.day_key(primary["lastDate"], tz) if primary["lastDate"] else None,
            "next": fu.day_key(primary["nextDate"], tz) if primary["nextDate"] else None,
        }
    period = resolve_period(today, payday, prefs["periodMode"])
    start_at = fu.parse_boundary(period["start"], tz, "start")
    end_at = fu.parse_boundary(period["end"], tz, "end")
    today_start = fu.parse_boundary(today, tz, "start")
    end_date_only, start_date_only, today_date_only = _date_only(period["end"]), _date_only(period["start"]), _date_only(today)

    received = await db.transaction.find_many(
        where={"userId": user.id, "type": "INCOME", "date": {"gte": start_at, "lte": now}, **_NOT_DEBT}, include={"category": True}
    )
    paid_bills = await db.transaction.find_many(
        where={"userId": user.id, "type": "EXPENSE", "recurringPaymentId": {"not": None}, "date": {"gte": start_at, "lte": end_at}},
        include={"recurringPayment": {"include": {"debt": True}}},
    )
    rps = await db.recurringpayment.find_many(
        where={"userId": user.id, "status": "ACTIVE", "nextDate": {"lte": end_date_only}}, include={"debt": {"include": {"payments": True}}}
    )
    recurring_expenses = await db.transaction.find_many(
        where={"userId": user.id, "type": "EXPENSE", "rrule": {"not": None}}, include={"category": True}
    )
    owe = {"userId": user.id, "status": "OPEN", "direction": "I_OWE", "recurringPaymentId": None}
    debts = await db.debt.find_many(where={**owe, "dueDate": {"not": None, "lte": end_date_only}}, include={"payments": True})
    # paid off over several periods: due after this one, or no due date
    long_debts = await db.debt.find_many(
        where={**owe, "OR": [{"dueDate": None}, {"dueDate": {"gt": end_date_only}}]}, include={"payments": True}
    )
    debt_payments = await db.debtpayment.find_many(
        where={"userId": user.id, "date": {"gte": start_date_only, "lte": today_date_only}, "debt": {"is": {"direction": "I_OWE", "recurringPaymentId": None}}},
        include={"debt": True},
    )
    everyday = await _sum_expenses(user.id, start_at, now, fx)
    spent_today = await _sum_expenses(user.id, today_start, now, fx)
    later_from = max(now + _MS, start_at)
    balance = await _balance_now(user.id, start_at, now, fx)

    # ----- income lines -----
    # a past occurrence of a recurring income is a copy of its series (same title, payer, category, currency)
    def same_as(t, h) -> bool:
        return t.title == h.title and t.source == h.source and t.categoryId == h.categoryId and t.currency == h.currency

    income = []
    for t in received:
        line = {
            "title": _coalesce(t.title, t.source, t.category.name if t.category else None, ""),
            "amount": fx.to_default(t.amount, t.currency),
            "date": fu.day_key(t.date, tz),
            "done": True,
            "link": f"/finance/transactions?id={t.id}",
        }
        # with no recurring income at all, every income is "regular"
        if heads and not t.rrule and not any(same_as(t, h) for h in heads):
            line["extra"] = True
        income.append(line)
    for src, head in zip(sources, heads):
        for d in rrule.between(src["rrule"], head.date, later_from, end_at, [], tz=tz)[:62]:
            income.append({"title": src["title"], "amount": fx.to_default(src["amount"], src["currency"]), "date": fu.day_key(d, tz), "done": False, "link": f"/finance/transactions?id={src['id']}"})

    # ----- commitments -----
    commitments = [
        {
            "kind": _kind_of(t.recurringPayment.kind if t.recurringPayment else "BILL", bool(t.recurringPayment and t.recurringPayment.debt)),
            "title": _coalesce(t.title, ""),
            "amount": fx.to_default(t.amount, t.currency),
            "date": fu.day_key(t.date, tz),
            "done": True,
            "link": f"/finance/transactions?id={t.id}",
        }
        for t in paid_bills
    ]
    for p in rps:
        kind = _kind_of(p.kind, p.debt is not None)
        each = fx.to_default(p.amount, p.currency)
        # an installment plan stops when its loan is paid off
        left = fx.to_default(_left(p.debt), p.debt.currency) if p.debt else math.inf
        count = math.ceil(left / each - 0.005) if each > 0 and math.isfinite(left) else 62
        later = rrule.between(p.rrule, p.startDate, dates._utc(p.nextDate) + DAY, end_date_only)
        for d in [p.nextDate, *later][: max(0, min(62, count))]:
            day = _iso_day(d)
            link = f"/finance/{'subscriptions' if p.kind == 'SUBSCRIPTION' else 'recurring'}?id={p.id}"
            commitments.append({"kind": kind, "title": p.name, "amount": each, "date": max(day, today), "done": False, "link": link})
    for t in recurring_expenses:
        amount = fx.to_default(t.amount, t.currency)
        title = _coalesce(t.title, t.category.name if t.category else None, "")
        link = f"/finance/transactions?id={t.id}"
        if start_at <= dates._utc(t.date) <= now:
            commitments.append({"kind": "expense", "title": title, "amount": amount, "date": fu.day_key(t.date, tz), "done": True, "link": link})
        for d in rrule.between(t.rrule, t.date, later_from, end_at, [], tz=tz)[:62]:
            commitments.append({"kind": "expense", "title": title, "amount": amount, "date": fu.day_key(d, tz), "done": False, "link": link})
    for d in debts:
        left = _left(d)
        if left <= 0.005:
            continue
        commitments.append({"kind": "debt", "title": d.title, "amount": fx.to_default(left, d.currency), "date": max(_iso_day(d.dueDate), today), "done": False, "link": f"/finance/debts?id={d.id}"})
    long_ids = {d.id for d in long_debts}
    for p in debt_payments:
        if p.debt.id in long_ids:
            continue  # counted in the debt plan below
        commitments.append({"kind": "debt", "title": p.debt.title, "amount": fx.to_default(p.amount, p.debt.currency), "date": _iso_day(p.date), "done": True, "link": f"/finance/debts?id={p.debt.id}"})

    plan_debts = []
    for d in long_debts:
        paid = js_sum(to_number(x.amount) for x in d.payments or [])
        in_period = js_sum(to_number(x.amount) for x in d.payments or [] if start_date_only <= dates._utc(x.date) <= today_date_only)
        plan_debts.append({
            "id": d.id,
            "title": d.title,
            "left": fx.to_default(max(0, to_number(d.principal) - paid), d.currency),
            "paidInPeriod": fx.to_default(in_period, d.currency),
            "target": _iso_day(d.dueDate) if d.dueDate else add_months(fu.day_key(d.createdAt, tz), prefs["debtPayoffMonths"]),
            "link": f"/finance/debts?id={d.id}",
        })

    result = build_plan({
        "today": today,
        "period": period,
        "income": income,
        "commitments": commitments,
        "savings": {"mode": prefs["savingsMode"], "value": prefs["savingsValue"]},
        "everydaySpent": everyday,
        "spentToday": spent_today,
        "debts": plan_debts,
        "extraSplit": {"debts": prefs["extraDebtPct"], "savings": prefs["extraSavingsPct"]},
        "balance": balance,
    })
    by_date = lambda line: (line["done"], line["date"])  # noqa: E731 — unpaid first, then by date
    return {
        "currency": fx.default_currency,
        "today": today,
        "period": period,
        **result,
        "income": sorted(income, key=by_date),
        "commitments": sorted(commitments, key=by_date),
        # NestJS rebuilt each source as {...rest, monthlyAmount, lastDate}
        "sources": [
            {**{k: v for k, v in s.items() if k not in ("monthlyAmount", "lastDate")}, "monthlyAmount": round_money(s["monthlyAmount"]), "lastDate": s["lastDate"]}
            for s in sources
        ],
        "settings": prefs,
    }


async def update_settings(user, dto: dict) -> dict:
    if dto["savingsMode"] == "percent" and dto["savingsValue"] > 100:
        raise nest_error(400, "A percentage cannot be more than 100")
    settings = await ensure_os_profile(user)
    prev = read_settings(settings.spendingPlan)
    value = {
        "savingsMode": dto["savingsMode"],
        "savingsValue": dto["savingsValue"],
        "periodMode": dto["periodMode"],
        **{k: prev[k] if dto.get(k) is None else dto[k] for k in ("debtPayoffMonths", "extraDebtPct", "extraSavingsPct")},
    }
    if value["extraDebtPct"] + value["extraSavingsPct"] > 100:
        raise nest_error(400, "The extra-income split cannot be more than 100%")
    await db.usersettings.update(where={"userId": user.id}, data={"spendingPlan": Json(value)})
    return await plan(user)


def _to_schedule(s) -> dict:
    if s.type == "monthly":
        if s.day is None or s.day == 0:
            raise nest_error(400, "Choose the day of the month")
        return {"type": "monthly", "day": s.day}
    if s.type == "weekly":
        if s.weekday is None:
            raise nest_error(400, "Choose the day of the week")
        return {"type": "weekly", "weekday": s.weekday}
    if not s.weekdays:
        raise nest_error(400, "Choose the days you work")
    return {"type": "daily", "weekdays": s.weekdays}


async def _salary_category(user_id: str) -> str | None:
    """The user's "Salary" income category, when they have one (any language)."""
    row = await db.category.find_first(
        where={"userId": user_id, "scope": "INCOME", "OR": [{"name": {"contains": "salary", "mode": "insensitive"}}, {"name": {"contains": "راتب"}}]}
    )
    return row.id if row else None


async def setup_income(user, body) -> dict:
    """Sets up a recurring income (salary, weekly pay, daily wage), recorded automatically on each payday."""
    from app.os import transactions

    rule = schedule_rrule(_to_schedule(body.schedule))
    settings = await ensure_os_profile(user)
    tz = fu.safe_tz(settings.timezone)
    now = datetime.now(timezone.utc)
    today = fu.day_key(now, tz)
    # occurrences at 12:00 local around today
    anchor = local_date_time(add_days(today, -70), "12:00", tz)
    window = rrule.between(rule, anchor, anchor, local_date_time(add_days(today, 70), "12:00", tz), [], tz=tz)
    if body.alreadyReceived:
        first = next((d for d in reversed(window) if fu.day_key(d, tz) <= today), None)
    else:
        first = next((d for d in window if fu.day_key(d, tz) > today), None)
    if first is None:
        raise nest_error(400, "Could not work out the next payday")
    # a payday "today" counts as received now, not at noon
    moment = now if body.alreadyReceived and first > now else first

    category_id = body.categoryId if body.categoryId is not None else await _salary_category(user.id)
    fields = {
        "type": "INCOME",
        "amount": body.amount,
        "date": _iso(moment),
        "title": body.title,
        "rrule": rule,
        **({"currency": body.currency} if body.currency else {}),
        **({"categoryId": category_id} if category_id else {}),
        **({"accountId": body.accountId} if body.accountId else {}),
    }
    tx = await transactions.create(transactions.CreateBody(**fields), user=user)
    return {"transaction": tx, "plan": await plan(user)}
