"""/api/budgets (port of vournalOS BudgetsService).

A budget limits expenses for a month or a year, overall or for one expense
category (sub-categories count toward their parent). Spending is converted into
the budget's own currency.
"""

from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import finance_utils as fu
from app.os.common import validators
from app.os.common.activity import log_activity
from app.os.common.currency import get_converter, round_money, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.profile import ensure_os_profile
from app.os.tasks import _query_bool
from app.os.transactions import as_decimal

router = nest_router("/budgets")

PeriodName = Literal["MONTHLY", "YEARLY"]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
_FIELDS = ("id", "name", "categoryId", "amount", "currency", "period", "alertThreshold", "isActive", "createdAt", "updatedAt")
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    # Intl ar-LB (Levantine month names), Western digits
    "ar": ["كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب", "أيلول", "تشرين الأول", "تشرين الثاني", "كانون الأول"],
}
OVERALL = {"en": "Overall spending", "ar": "إجمالي الإنفاق"}


def month_year_label(year: int, month: int, lang: str) -> str:
    return f"{MONTHS[lang][month - 1]} {year}"


def _js_round(value: float) -> float:
    import math

    return math.floor(value + 0.5)


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: object = None
    currency: CurrencyCode | None = None
    categoryId: StrictStr | None = None
    period: PeriodName | None = None
    name: StrictStr | None = None
    alertThreshold: StrictInt | None = Field(default=None, ge=1, le=100)
    isActive: StrictBool | None = None

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        return validators.money(v, "amount", 0.01)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 60)


class CreateBody(_Fields):
    amount: object

    @field_validator("amount")
    @classmethod
    def _required_amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return validators.money(v, "amount", 0.01)


class UpdateBody(_Fields):
    pass


class _Window:
    def __init__(self, key, label, start, end, start_key, end_key):
        self.key, self.label, self.start, self.end, self.start_key, self.end_key = key, label, start, end, start_key, end_key


async def _context(user, period: str | None):
    settings = await ensure_os_profile(user)
    tz, lang = fu.safe_tz(settings.timezone), to_lang(settings.language)
    fx = await get_converter(user.id)
    from datetime import datetime, timezone

    today = fu.day_key(datetime.now(timezone.utc), tz)
    cy, cm = int(today[:4]), int(today[5:7])
    y, m = cy, cm
    if period:
        parts = period.split("-")
        y = int(parts[0])
        # "YYYY": monthly budgets use this month this year, December for past years, January for future ones
        m = int(parts[1]) if len(parts) > 1 else (cm if y == cy else 12 if y < cy else 1)
    mk = f"{y}-{m:02d}"
    last = fu.days_in_month(y, m)
    month = _Window(mk, month_year_label(y, m, lang), fu.local_start(y, m, 1, tz), fu.local_end(y, m, last, tz), f"{mk}-01", f"{mk}-{last:02d}")
    year = _Window(str(y), str(y), fu.local_start(y, 1, 1, tz), fu.local_end(y, 12, 31, tz), f"{y}-01-01", f"{y}-12-31")
    return {"tz": tz, "lang": lang, "fx": fx, "today": today, "month": month, "year": year}


async def _compute(user_id: str, rows, ctx) -> list[dict]:
    if not rows:
        return []
    need_year = any(r.period == "YEARLY" for r in rows)
    month, year, fx = ctx["month"], ctx["year"], ctx["fx"]
    start = min(year.start, month.start) if need_year else month.start
    end = max(year.end, month.end) if need_year else month.end
    txs = await db.transaction.find_many(where={"userId": user_id, "type": "EXPENSE", "date": {"gte": start, "lte": end}})
    children = await db.category.find_many(where={"userId": user_id, "scope": "EXPENSE", "parentId": {"not": None}})
    parent_of = {c.id: c.parentId for c in children}
    out = []
    for b in rows:
        win = month if b.period == "MONTHLY" else year
        amount = to_number(b.amount)
        spent, count = 0, 0
        for t in txs:
            when = fu.dates._utc(t.date)
            if when < win.start or when > win.end:
                continue
            if b.categoryId:
                if not t.categoryId or (t.categoryId != b.categoryId and parent_of.get(t.categoryId) != b.categoryId):
                    continue
            spent += fx.convert(t.amount, t.currency, b.currency)
            count += 1
        spent = round_money(spent)
        total_days = len(fu.enumerate_days(win.start_key, win.end_key, 400))
        status = "past" if ctx["today"] > win.end_key else "future" if ctx["today"] < win.start_key else "current"
        elapsed = total_days if status == "past" else 0 if status == "future" else len(fu.enumerate_days(win.start_key, ctx["today"], 400))
        days_left = total_days - elapsed + 1 if status == "current" else total_days if status == "future" else 0
        remaining = round_money(amount - spent)
        percent = _js_round((spent / amount) * 1000) / 10 if amount > 0 else 0
        projected = round_money((spent / elapsed) * total_days) if status == "current" and elapsed > 0 else spent
        over = spent > amount
        view = {f: getattr(b, f) for f in _FIELDS}
        view.update(
            category=fu.mini(b.category, fu.CATEGORY_MINI),
            name=b.name or (b.category.name if b.category else None) or OVERALL[ctx["lang"]],
            customName=b.name,  # the user's own name (None = named after the category / overall)
            isOverall=not b.categoryId,
            periodKey=win.key,
            periodLabel=win.label,
            periodStart=win.start,
            periodEnd=win.end,
            periodStatus=status,
            spent=spent,
            remaining=remaining,
            percentUsed=percent,
            isWarning=percent >= b.alertThreshold,
            isOver=over,
            status="over" if over else "warning" if percent >= b.alertThreshold else "ok",
            transactionCount=count,
            totalDays=total_days,
            elapsedDays=elapsed,
            daysLeft=days_left,
            dailyAllowance=round_money(remaining / days_left) if days_left > 0 and remaining > 0 else 0,
            projectedSpend=projected,
            projectedOver=projected > amount,
            amountInDefault=fx.to_default(amount, b.currency),
            spentInDefault=fx.to_default(spent, b.currency),
            defaultCurrency=fx.default_currency,
        )
        out.append(view)
    return out


def _period_query(request: Request, errors: list) -> tuple[str | None, str | None]:
    import re

    period = request.query_params.get("period")
    if period is not None and not re.fullmatch(r"\d{4}(-(0[1-9]|1[0-2]))?", period, re.ASCII):
        errors.append("period must be YYYY-MM or YYYY")
    kind = request.query_params.get("periodType")
    if kind is not None and kind not in ("MONTHLY", "YEARLY"):
        errors.append("periodType must be one of the following values: MONTHLY, YEARLY")
    return period, kind


async def _assert_category(user_id: str, category_id: str | None) -> None:
    if not category_id:
        return
    category = await db.category.find_first(where={"id": category_id, "userId": user_id})
    if category is None:
        raise nest_error(404, "Category not found")
    if category.scope != "EXPENSE":
        raise nest_error(400, "Budgets can only use expense categories")


async def _assert_unique(user_id: str, category_id, period: str, exclude_id: str | None = None) -> None:
    where = {"userId": user_id, "categoryId": category_id, "period": period}
    if exclude_id:
        where["id"] = {"not": exclude_id}
    clash = await db.budget.find_first(where=where, include={"category": True})
    if clash:
        what = f"for {clash.category.name}" if clash.category and clash.category.name else "for overall spending"
        raise nest_error(409, f"You already have a {'monthly' if period == 'MONTHLY' else 'yearly'} budget {what}")


async def _get(user, id: str, period: str | None) -> dict:
    row = await db.budget.find_first(where={"id": id, "userId": user.id}, include={"category": True})
    if row is None:
        raise nest_error(404, "Budget not found")
    [view] = await _compute(user.id, [row], await _context(user, period))
    return view


@router.get("")
async def list_budgets(request: Request, user=Depends(plus_user)):
    errors: list[str] = []
    period, kind = _period_query(request, errors)
    include_inactive = _query_bool(request, "includeInactive", errors)
    if errors:
        raise nest_error(400, errors)
    where = {"userId": user.id, **({} if include_inactive else {"isActive": True}), **({"period": kind} if kind else {})}
    rows = await db.budget.find_many(where=where, include={"category": True}, order=[{"period": "asc"}, {"createdAt": "asc"}])
    ctx = await _context(user, period)
    items = await _compute(user.id, rows, ctx)
    totals = []
    for kind_name in ("MONTHLY", "YEARLY"):
        group = [i for i in items if i["period"] == kind_name and i["isActive"]]
        budgeted = sum(i["amountInDefault"] for i in group)
        spent = sum(i["spentInDefault"] for i in group)
        totals.append(
            {
                "period": kind_name,
                "count": len(group),
                "budgeted": round_money(budgeted),
                "spent": round_money(spent),
                "remaining": round_money(budgeted - spent),
                "percentUsed": _js_round((spent / budgeted) * 1000) / 10 if budgeted else 0,
            }
        )
    month, year = ctx["month"], ctx["year"]
    window = lambda w: {"key": w.key, "label": w.label, "from": w.start, "to": w.end, "fromKey": w.start_key, "toKey": w.end_key}  # noqa: E731
    return {
        "items": items,
        "total": len(items),
        "currency": ctx["fx"].default_currency,
        "month": window(month),
        "year": window(year),
        "totals": totals,
        "overCount": sum(1 for i in items if i["isActive"] and i["isOver"]),
        "warningCount": sum(1 for i in items if i["isActive"] and i["isWarning"] and not i["isOver"]),
    }


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    settings = await ensure_os_profile(user)
    category_id = body.categoryId or None
    period = body.period or "MONTHLY"
    await _assert_category(user.id, category_id)
    await _assert_unique(user.id, category_id, period)
    row = await db.budget.create(
        data={
            "userId": user.id,
            "amount": as_decimal(body.amount),
            "currency": body.currency or settings.defaultCurrency,
            "categoryId": category_id,
            "period": period,
            "name": body.name,
            "alertThreshold": body.alertThreshold if body.alertThreshold is not None else 80,
            "isActive": True if body.isActive is None else body.isActive,
        }
    )
    await log_activity(user.id, "budget.created", entity_id=row.id, amount=body.amount, period=period, categoryId=category_id)
    return await _get(user, row.id, None)


@router.get("/{id}")
async def get(id: str, request: Request, user=Depends(plus_user)):
    errors: list[str] = []
    period, _ = _period_query(request, errors)
    if errors:
        raise nest_error(400, errors)
    return await _get(user, id, period)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await db.budget.find_first(where={"id": id, "userId": user.id})
    if existing is None:
        raise nest_error(404, "Budget not found")
    sent = body.model_fields_set
    category_id = existing.categoryId if "categoryId" not in sent else (body.categoryId or None)
    period = body.period or existing.period
    if "categoryId" in sent:
        await _assert_category(user.id, category_id)
    if category_id != existing.categoryId or period != existing.period:
        await _assert_unique(user.id, category_id, period, id)
    data = {"categoryId": category_id, "period": period}
    if body.amount is not None:
        data["amount"] = as_decimal(body.amount)
    for field in ("currency", "alertThreshold", "isActive"):
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    if "name" in sent:
        data["name"] = body.name
    await db.budget.update(where={"id": existing.id}, data=data)
    await log_activity(user.id, "budget.updated", entity_id=id)
    return await _get(user, id, None)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await db.budget.find_first(where={"id": id, "userId": user.id})
    if existing is None:
        raise nest_error(404, "Budget not found")
    await db.budget.delete(where={"id": existing.id})
    await log_activity(user.id, "budget.deleted", entity_id=id)
    return {"ok": True}
