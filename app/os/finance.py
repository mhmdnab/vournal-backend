"""/api/finance (port of vournalOS FinanceService and FinanceController).

The money overview: totals, breakdowns and series for a period (summary), the
cash-flow forecast, and the spending plan (spending_plan.py). Amounts are in the
user's default currency. Sums follow NestJS's float arithmetic, rounded at the end.
"""

import math
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import finance_utils as fu
from app.os import spending_plan
from app.os.accounts import net_worth
from app.os.common import dates, rrule, validators
from app.os.common.currency import get_converter, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.jsparse import js_number, js_sum
from app.os.common.query import _query_int, unknown_params
from app.os.common.i18n import to_lang
from app.os.finance_forecast import add_days, average_daily_spend, build_forecast
from app.os.profile import ensure_os_profile
from app.os.recurring_payments import UNCATEGORIZED
from app.os.transactions import materialize_due

router = nest_router("/finance")

CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]

DAY = timedelta(days=1)
_MS = timedelta(milliseconds=1)
_UNCATEGORIZED_LOOK = {"color": "#94a3b8", "icon": "circle-help"}
FORECAST_HISTORY_DAYS = 60  # window used to estimate everyday (non-recurring) spending


def _js_round(x: float) -> float:
    return math.floor(x + 0.5)


def _coalesce(*values):
    """a ?? b ?? c (an empty string counts as a value)."""
    return next((v for v in values if v is not None), None)


def _date_only(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def _iso_day(moment: datetime) -> str:
    """Date#toISOString().slice(0, 10)."""
    return dates._utc(moment).date().isoformat()


def _in(moment: datetime, window: dict) -> bool:
    return window["from"] <= dates._utc(moment) <= window["to"]


# ----- forecast ------------------------------------------------------------------------------


async def forecast(user, days: int) -> dict:
    """Today's total balance projected `days` ahead from recurring payments, recurring
    income / expenses, dated debts and recent everyday spending (see finance_forecast)."""
    await materialize_due(user.id)
    settings = await ensure_os_profile(user)
    tz = fu.safe_tz(settings.timezone)
    fx = await get_converter(user.id)
    now = datetime.now(timezone.utc)
    today = fu.day_key(now, tz)
    end_key = add_days(today, days)
    end_date_only = _date_only(end_key)
    end_instant = fu.parse_boundary(end_key, tz, "end")
    today_start = fu.parse_boundary(today, tz, "start")
    history_from = fu.parse_boundary(add_days(today, -FORECAST_HISTORY_DAYS), tz, "start")

    start_balance = await net_worth(user.id, fx)
    payments = await db.recurringpayment.find_many(where={"userId": user.id, "status": "ACTIVE", "nextDate": {"lte": end_date_only}})
    recurring_tx = await db.transaction.find_many(
        where={"userId": user.id, "rrule": {"not": None}, "type": {"in": ["INCOME", "EXPENSE"]}}, include={"category": True}
    )
    history = await db.transaction.find_many(
        where={"userId": user.id, "type": "EXPENSE", "recurringPaymentId": None, "rrule": None, "date": {"gte": history_from, "lt": today_start}}
    )
    first = await db.transaction.find_first(where={"userId": user.id}, order={"date": "asc"})
    # money I owe: installment plans stop when the loan is paid off; a dated debt without a plan is one payment
    debts = await db.debt.find_many(where={"userId": user.id, "status": "OPEN", "direction": "I_OWE"}, include={"payments": True})

    def debt_left(d) -> float:
        return max(0, to_number(d.principal) - js_sum(to_number(p.amount) for p in d.payments or []))

    plan_left = {d.recurringPaymentId: fx.to_default(debt_left(d), d.currency) for d in debts if d.recurringPaymentId}

    items: list[dict] = []
    for p in payments:
        later = rrule.between(p.rrule, p.startDate, dates._utc(p.nextDate) + DAY, end_date_only)
        subscription = p.kind == "SUBSCRIPTION"
        installment = fx.to_default(p.amount, p.currency)
        left = plan_left.get(p.id)
        count = 400 if left is None or installment <= 0 else math.ceil(left / installment - 0.005)
        for d in [p.nextDate, *later][: max(0, min(400, count))]:
            items.append({
                "date": _iso_day(d),
                "kind": "subscription" if subscription else "bill",
                "title": p.name,
                "amount": -fx.to_default(p.amount, p.currency),
                "source": "recurringPayment",
                "sourceId": p.id,
                "link": f"/finance/{'subscriptions' if subscription else 'recurring'}?id={p.id}",
            })
    for t in recurring_tx:
        value = fx.to_default(t.amount, t.currency)
        for d in rrule.between(t.rrule, t.date, dates._utc(t.date) + _MS, end_instant, [], tz=tz)[:400]:
            items.append({
                "date": fu.day_key(d, tz),
                "kind": "income" if t.type == "INCOME" else "expense",
                "title": _coalesce(t.title, t.source, t.category.name if t.category else None, ""),
                "amount": value if t.type == "INCOME" else -value,
                "source": "transaction",
                "sourceId": t.id,
                "link": f"/finance/transactions?id={t.id}",
            })
    for d in debts:
        if d.recurringPaymentId or not d.dueDate or dates._utc(d.dueDate) > end_date_only:
            continue
        left = fx.to_default(debt_left(d), d.currency)
        if left <= 0:
            continue
        due = _iso_day(d.dueDate)
        items.append({"date": due, "kind": "debt", "title": d.title, "amount": -left, "source": "debt", "sourceId": d.id, "overdue": due < today, "link": f"/finance/debts?id={d.id}"})

    # everyday spending: average of non-recurring expenses over the days we actually have data for
    history_start = dates._utc(first.date) if first and dates._utc(first.date) > history_from else history_from
    history_days = int(max(0, min(FORECAST_HISTORY_DAYS, _js_round((today_start - history_start) / DAY))))
    history_total = js_sum(fx.to_default(t.amount, t.currency) for t in history)
    daily_variable = average_daily_spend(history_total, history_days)
    return {
        "currency": fx.default_currency,
        "historyDays": history_days,
        "hasSpendingHistory": daily_variable > 0,
        **build_forecast(today=today, days=days, start_balance=start_balance, daily_variable=daily_variable, items=items),
    }


# ----- summary -------------------------------------------------------------------------------


def _previous_period(from_key: str, to_key: str, tz: str) -> dict:
    """Whole calendar month → previous month; whole year → previous year; otherwise
    the same number of days just before."""
    fy, fm, fd = (int(x) for x in from_key.split("-"))
    ty, tm, td = (int(x) for x in to_key.split("-"))
    if fd == 1 and fy == ty and fm == tm and td == fu.days_in_month(ty, tm):
        py, pm = (fy - 1, 12) if fm == 1 else (fy, fm - 1)
        pf, pt = (py, pm, 1), (py, pm, fu.days_in_month(py, pm))
    elif fm == 1 and fd == 1 and tm == 12 and td == 31 and fy == ty:
        pf, pt = (fy - 1, 1, 1), (fy - 1, 12, 31)
    else:
        n = len(fu.enumerate_days(from_key, to_key, 100_000))
        start = date(fy, fm, fd)
        p_end, p_start = start - timedelta(days=1), start - timedelta(days=n)
        pf, pt = (p_start.year, p_start.month, p_start.day), (p_end.year, p_end.month, p_end.day)
    key = lambda a: f"{a[0]:04d}-{a[1]:02d}-{a[2]:02d}"  # noqa: E731
    return {"from": fu.local_start(*pf, tz), "to": fu.local_end(*pt, tz), "fromKey": key(pf), "toKey": key(pt)}


def _breakdown(agg: dict, cat_by_id: dict, total: float, uncategorized: str) -> list[dict]:
    """Parent roll-up: sub-category amounts are added to their parent; children listed for drill-down."""
    top: dict = {}

    def ensure(id):
        if id not in top:
            c = cat_by_id.get(id) if id else None
            top[id] = {
                "categoryId": id,
                "name": c.name if c else uncategorized,
                "color": _coalesce(c.color if c else None, _UNCATEGORIZED_LOOK["color"]),
                "icon": _coalesce(c.icon if c else None, _UNCATEGORIZED_LOOK["icon"]),
                "amount": 0, "percent": 0, "count": 0, "children": [], "direct": 0,
            }
        return top[id]

    for cat_id, v in agg.items():
        cat = cat_by_id.get(cat_id) if cat_id else None
        if cat is None:
            item = ensure(None)
            item["amount"] += v["amount"]
            item["direct"] += v["amount"]
            item["count"] += v["count"]
            continue
        parent = cat.parentId if cat.parentId and cat.parentId in cat_by_id else None
        item = ensure(parent or cat.id)
        item["amount"] += v["amount"]
        item["count"] += v["count"]
        if parent:
            item["children"].append({
                "categoryId": cat.id, "name": cat.name, "color": _coalesce(cat.color, item["color"]),
                "icon": _coalesce(cat.icon, item["icon"]), "amount": v["amount"], "percent": 0, "count": v["count"],
            })
        else:
            item["direct"] += v["amount"]

    out = [
        {
            **i,
            "amount": fu.round2(i["amount"]),
            "direct": fu.round2(i["direct"]),
            "percent": fu.pct(i["amount"], total),
            "children": sorted(
                ({**c, "amount": fu.round2(c["amount"]), "percent": fu.pct(c["amount"], i["amount"])} for c in i["children"]),
                key=lambda c: -c["amount"],
            ),
        }
        for i in top.values()
    ]
    return sorted(out, key=lambda i: -i["amount"])


async def summary(user, q: dict) -> dict:
    await materialize_due(user.id)
    settings = await ensure_os_profile(user)
    tz = fu.safe_tz(settings.timezone)
    ws = settings.weekStartsOn
    uncategorized = UNCATEGORIZED[to_lang(settings.language)]
    fx = await get_converter(user.id)
    now = datetime.now(timezone.utc)

    # ----- requested range, previous comparable period, fixed periods, 12-month window -----
    base = dates.range(q.get("period") or "month", now, tz, ws)
    start = fu.parse_boundary(q.get("from"), tz, "start") or base["from"]
    end = fu.parse_boundary(q.get("to"), tz, "end") or base["to"]
    if end < start:
        raise nest_error(400, '"to" must be after "from"')
    window = {"from": start, "to": end}
    from_key, to_key = fu.day_key(start, tz), fu.day_key(end, tz)
    prev = _previous_period(from_key, to_key, tz)
    periods = {p: dates.range(name, now, tz, ws) for p, name in (("today", "day"), ("week", "week"), ("month", "month"), ("year", "year"))}
    months = fu.last_months(to_key[:7], 12)
    my, mm = (int(x) for x in months[0].split("-"))
    monthly_from = fu.local_start(my, mm, 1, tz)
    query_from = min(start, prev["from"], monthly_from, periods["year"]["from"], periods["week"]["from"])
    query_to = max(end, prev["to"], periods["year"]["to"], periods["week"]["to"])

    rows = await db.transaction.find_many(
        where={"userId": user.id, "type": {"in": ["INCOME", "EXPENSE"]}, "date": {"gte": query_from, "lte": query_to}}
    )
    categories = await db.category.find_many(where={"userId": user.id, "scope": {"in": ["INCOME", "EXPENSE"]}})
    balance = await net_worth(user.id, fx)
    cat_by_id = {c.id: c for c in categories}

    income = expenses = prev_income = prev_expenses = 0
    count = 0
    spending = {"today": 0, "week": 0, "month": 0, "year": 0}
    earning = {"today": 0, "week": 0, "month": 0, "year": 0}
    daily: dict[str, dict] = {}
    monthly: dict[str, dict] = {}
    cat_agg: dict[str, dict] = {"INCOME": {}, "EXPENSE": {}}
    candidates: list[tuple[str, float]] = []
    month_set = set(months)

    for r in rows:
        v = fx.to_default(r.amount, r.currency)
        is_income = r.type == "INCOME"
        key = fu.day_key(r.date, tz)
        if _in(r.date, window):
            count += 1
            if is_income:
                income += v
            else:
                expenses += v
                candidates.append((r.id, v))
            d = daily.setdefault(key, {"income": 0, "expenses": 0})
            d["income" if is_income else "expenses"] += v
            c = cat_agg[r.type].setdefault(r.categoryId, {"amount": 0, "count": 0})
            c["amount"] += v
            c["count"] += 1
        if _in(r.date, prev):
            if is_income:
                prev_income += v
            else:
                prev_expenses += v
        if key[:7] in month_set and dates._utc(r.date) >= monthly_from:
            m = monthly.setdefault(key[:7], {"income": 0, "expenses": 0})
            m["income" if is_income else "expenses"] += v
        for p in ("today", "week", "month", "year"):
            if _in(r.date, periods[p]):
                if is_income:
                    earning[p] += v
                else:
                    spending[p] += v

    # ----- top expenses -----
    top = sorted(candidates, key=lambda t: -t[1])[:5]
    top_rows = (
        await db.transaction.find_many(where={"userId": user.id, "id": {"in": [t[0] for t in top]}}, include={"category": True, "account": True})
        if top
        else []
    )
    top_by_id = {t.id: t for t in top_rows}
    top_expenses = [
        {
            "id": row.id, "title": row.title, "amount": row.amount, "currency": row.currency, "date": row.date,
            "paymentMethod": row.paymentMethod, "category": fu.mini(row.category, fu.CATEGORY_MINI),
            "account": {"id": row.account.id, "name": row.account.name} if row.account else None,
            "amountInDefault": fu.round2(v),
        }
        for id, v in top
        if (row := top_by_id.get(id)) is not None
    ]

    # ----- series -----
    daily_series = []
    for day in fu.enumerate_days(from_key, to_key, 1000):
        d = daily.get(day, {"income": 0, "expenses": 0})
        daily_series.append({"date": day, "income": fu.round2(d["income"]), "expenses": fu.round2(d["expenses"]), "net": fu.round2(d["income"] - d["expenses"])})
    monthly_series = []
    for month in months:
        m = monthly.get(month, {"income": 0, "expenses": 0})
        monthly_series.append({"month": month, "income": fu.round2(m["income"]), "expenses": fu.round2(m["expenses"]), "savings": fu.round2(m["income"] - m["expenses"])})

    # average daily spending over the elapsed part of the range
    today_key = fu.day_key(now, tz)
    elapsed_end = min(to_key, today_key)
    elapsed_days = len(fu.enumerate_days(from_key, elapsed_end, 1000)) if from_key <= elapsed_end else 0
    savings = income - expenses
    prev_savings = prev_income - prev_expenses
    return {
        "currency": fx.default_currency,
        "from": start,
        "to": end,
        "fromDate": from_key,
        "toDate": to_key,
        "totalIncome": fu.round2(income),
        "totalExpenses": fu.round2(expenses),
        "savings": fu.round2(savings),
        "savingsRate": _js_round((savings / income) * 1000) / 10 if income > 0 else None,
        "balance": balance,
        "netWorth": balance,
        "transactionCount": count,
        "averageDailySpending": fu.round2(expenses / elapsed_days) if elapsed_days else 0,
        "spending": {p: fu.round2(v) for p, v in spending.items()},
        "income": {p: fu.round2(v) for p, v in earning.items()},
        "byCategory": {
            "expenses": _breakdown(cat_agg["EXPENSE"], cat_by_id, expenses, uncategorized),
            "income": _breakdown(cat_agg["INCOME"], cat_by_id, income, uncategorized),
        },
        "daily": daily_series,
        "monthly": monthly_series,
        "topExpenses": top_expenses,
        "comparison": {
            "from": prev["from"],
            "to": prev["to"],
            "fromDate": prev["fromKey"],
            "toDate": prev["toKey"],
            "totalIncome": fu.round2(prev_income),
            "totalExpenses": fu.round2(prev_expenses),
            "savings": fu.round2(prev_savings),
            "incomeChange": fu.pct_change(income, prev_income),
            "expensesChange": fu.pct_change(expenses, prev_expenses),
            "savingsChange": fu.pct_change(savings, prev_savings),
        },
    }


# ----- bodies --------------------------------------------------------------------------------


def _int_between(v, field, low, high):
    if v is None:
        return v
    if v < low:
        raise ValueError(f"{field} must not be less than {low}")
    if v > high:
        raise ValueError(f"{field} must not be greater than {high}")
    return v


class PlanSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    savingsMode: Literal["none", "percent", "amount"]
    savingsValue: object
    periodMode: Literal["auto", "month"]
    debtPayoffMonths: StrictInt | None = None
    extraDebtPct: StrictInt | None = None
    extraSavingsPct: StrictInt | None = None

    @field_validator("savingsValue")
    @classmethod
    def _value(cls, v):
        if v is None:
            raise ValueError("savingsValue must be a number conforming to the specified constraints")
        return _int_between(validators.money(v, "savingsValue", 0), "savingsValue", 0, 99_999_999)

    @field_validator("debtPayoffMonths")
    @classmethod
    def _months(cls, v):
        return _int_between(v, "debtPayoffMonths", 1, 60)

    @field_validator("extraDebtPct", "extraSavingsPct")
    @classmethod
    def _pct(cls, v, info):
        return _int_between(v, info.field_name, 0, 100)


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["monthly", "weekly", "daily"]
    day: StrictInt | None = None
    weekday: StrictInt | None = None
    weekdays: list[StrictInt] | None = None

    @field_validator("day")
    @classmethod
    def _day(cls, v):
        return _int_between(v, "day", -1, 28)

    @field_validator("weekday")
    @classmethod
    def _weekday(cls, v):
        return _int_between(v, "weekday", 0, 6)

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, v):
        if v is not None:
            if len(v) > 7:
                raise ValueError("weekdays must contain no more than 7 elements")
            for d in v:
                _int_between(d, "each value in weekdays", 0, 6)
        return v


class IncomeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: StrictStr
    amount: object
    currency: CurrencyCode | None = None
    accountId: StrictStr | None = None
    categoryId: StrictStr | None = None
    schedule: ScheduleBody
    alreadyReceived: StrictBool

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return _int_between(validators.money(v, "amount", 0.01), "amount", 0.01, 99_999_999)



# ----- routes --------------------------------------------------------------------------------


@router.get("/summary")
async def get_summary(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors = unknown_params(request, ("from", "to", "period"))
    errors += [f"{f} must be a valid ISO 8601 date string" for f in ("from", "to") if q.get(f) is not None and not dates.is_iso_date_string(q.get(f))]
    if q.get("period") is not None and q.get("period") not in ("day", "week", "month", "year"):
        errors.append("period must be one of the following values: day, week, month, year")
    if errors:
        raise nest_error(400, errors)
    return await summary(user, {k: q.get(k) for k in ("from", "to", "period")})


@router.get("/forecast")
async def get_forecast(request: Request, user=Depends(plus_user)):
    """Projected balance for the next N days (7–365, default 60)."""
    errors = unknown_params(request, ("days",))
    days = _query_int(request, "days", errors, 365, minimum=7) or 60
    if errors:
        raise nest_error(400, errors)
    return await forecast(user, days)


@router.get("/plan")
async def get_plan(user=Depends(plus_user)):
    """Spending plan for the current pay period: income, commitments, savings and today's allowance."""
    return await spending_plan.plan(user)


@router.put("/plan/settings")
async def update_plan_settings(body: PlanSettingsBody, user=Depends(plus_user)):
    return await spending_plan.update_settings(user, body.model_dump())


@router.post("/income", status_code=201)
async def setup_income(body: IncomeBody, user=Depends(plus_user)):
    """Set up a recurring income once (salary / weekly pay / daily wage)."""
    return await spending_plan.setup_income(user, body)
