"""/api/recurring-payments (port of vournalOS RecurringPaymentsService).

Bills and subscriptions repeat on a rule over date-only values (UTC midnight):
nextDate is the cycle waiting to be paid. Paying records an expense and moves
nextDate on, once (a conditional update guards against double taps). Payments
set to log themselves are paid when due, lazily here on reads and writes; the
hourly job that does it for everyone comes with the scheduler.
"""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import finance_utils as fu
from app.os.common import dates, rrule, validators
from app.os.common.activity import log_activity
from app.os.common.currency import get_converter, round_money, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.query import string_list
from app.os.profile import ensure_os_profile
from app.os.transactions import as_decimal

router = nest_router("/recurring-payments")

KINDS = ("BILL", "SUBSCRIPTION", "INSURANCE", "LOAN", "OTHER")
STATUSES = ("ACTIVE", "PAUSED", "CANCELLED")
KindName = Literal[KINDS]  # type: ignore[valid-type]
StatusName = Literal[STATUSES]  # type: ignore[valid-type]
PaymentMethodName = Literal["CASH", "DEBIT_CARD", "CREDIT_CARD", "BANK_TRANSFER", "DIGITAL_WALLET", "CHECK", "OTHER"]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
MAX_AUTO_CATCH_UP = 24
DAY = timedelta(days=1)
_FIELDS = (
    "id", "kind", "name", "amount", "currency", "rrule", "startDate", "nextDate", "status", "categoryId", "accountId",
    "paymentMethod", "reminderDaysBefore", "autoLogExpense", "url", "notes", "cancelledAt", "createdAt", "updatedAt",
)
_TX_FIELDS = ("id", "type", "title", "amount", "currency", "date", "accountId", "categoryId", "recurringPaymentId")
UNCATEGORIZED = {"en": "Uncategorized", "ar": "بدون فئة"}
LOGGED_AUTOMATICALLY = {"en": "Logged automatically", "ar": "سُجّل تلقائيًا"}
# validator.js isURL with require_protocol: false (http, https or ftp; a host with a TLD)
_URL = re.compile(
    r"^(?:(?:https?|ftp)://)?(?:[^\s:@/]+(?::[^\s:@/]*)?@)?(?:localhost|(?:[a-z0-9¡-￿](?:[a-z0-9¡-￿-]*[a-z0-9¡-￿])?\.)+[a-z¡-￿]{2,})(?::\d{1,5})?(?:[/?#]\S*)?$",
    re.I,
)


def billing_cycle(rule: str) -> str:
    """Coarse cycle for UI labels ("/mo", "/yr"…)."""
    try:
        o = rrule.parse_rrule(rule)
    except Exception:  # noqa: BLE001
        return "CUSTOM"
    interval = o.get("interval") if isinstance(o.get("interval"), int) and o.get("interval") > 0 else 1
    has_by = any(o.get(k) is not None for k in ("byweekday", "bymonthday", "bysetpos", "bymonth"))
    freq = o["freq"]
    if freq == rrule.DAILY and interval == 1 and not has_by:
        return "DAILY"
    if freq == rrule.WEEKLY and interval == 1:
        return "CUSTOM" if len(o.get("byweekday") or []) > 1 else "WEEKLY"
    if freq == rrule.WEEKLY and interval == 2:
        return "BIWEEKLY"
    if freq == rrule.MONTHLY:
        return {1: "MONTHLY", 3: "QUARTERLY", 6: "SEMIANNUAL"}.get(interval, "CUSTOM")
    if freq == rrule.YEARLY and interval == 1:
        return "YEARLY"
    return "CUSTOM"


async def _context(user):
    settings = await ensure_os_profile(user)
    tz = fu.safe_tz(settings.timezone)
    return {"tz": tz, "fx": await get_converter(user.id), "lang": to_lang(settings.language), "today": dates.today_date_only(tz)}


async def _with_computed(user_id: str, rows, ctx) -> list[dict]:
    if not rows:
        return []
    by_row = {r.id: r for r in rows}
    stats: dict[str, dict] = {}
    groups: dict[tuple, dict] = {}
    for t in await db.transaction.find_many(where={"userId": user_id, "recurringPaymentId": {"in": list(by_row)}}):
        g = groups.setdefault((t.recurringPaymentId, t.currency), {"sum": Decimal(0), "count": 0, "last": None})
        g["sum"] += t.amount
        g["count"] += 1
        if g["last"] is None or t.date > g["last"]:
            g["last"] = t.date
    for (rid, currency), g in groups.items():
        cur = stats.setdefault(rid, {"total": 0, "count": 0, "last": None})
        cur["total"] += ctx["fx"].convert(g["sum"], currency, by_row[rid].currency)
        cur["count"] += g["count"]
        if g["last"] and (cur["last"] is None or g["last"] > cur["last"]):
            cur["last"] = g["last"]
    out = []
    for r in rows:
        amount = to_number(r.amount)
        monthly = round_money(amount * rrule.monthly_factor(r.rrule))
        yearly = round_money(monthly * 12)
        days_until = dates.diff_days(ctx["today"], r.nextDate)
        active = r.status == "ACTIVE"
        st = stats.get(r.id)
        view = {f: getattr(r, f) for f in _FIELDS}
        view.update(
            category=fu.mini(r.category, fu.CATEGORY_MINI),
            account=fu.mini(r.account, fu.ACCOUNT_MINI),
            cycle=billing_cycle(r.rrule),
            cycleLabel=rrule.describe(r.rrule, ctx["lang"]),
            monthlyCost=monthly,
            yearlyCost=yearly,
            amountInDefault=ctx["fx"].to_default(amount, r.currency),
            monthlyCostInDefault=ctx["fx"].to_default(monthly, r.currency),
            yearlyCostInDefault=ctx["fx"].to_default(yearly, r.currency),
            defaultCurrency=ctx["fx"].default_currency,
            daysUntilNext=days_until,
            isOverdue=active and days_until < 0,
            isDueSoon=active and 0 <= days_until <= max(r.reminderDaysBefore or 0, 3),
            isDueToday=active and days_until == 0,
            paymentCount=st["count"] if st else 0,
            totalPaid=round_money(st["total"] if st else 0),
            lastPaidAt=st["last"] if st else None,
        )
        out.append(view)
    return out


def _occurrences(p, until: datetime) -> list[datetime]:
    """nextDate and the following occurrences up to `until` (date-only)."""
    nxt = dates._utc(p["nextDate"])
    if nxt > until:
        return []
    rest = rrule.between(p["rrule"], p["startDate"], nxt + DAY, until)
    return [nxt, *rest][:120]


def _first_on_or_after(rule: str, anchor: datetime, today: datetime) -> datetime:
    """First occurrence on or after today; the anchor itself when it's in the future."""
    anchor = dates._utc(anchor)
    if anchor >= today:
        return anchor
    return rrule.next_occurrence(rule, anchor, today, inclusive=True) or anchor


async def _owned(user_id: str, id: str):
    row = await db.recurringpayment.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Recurring payment not found")
    return row


async def _find_account(user_id: str, account_id):
    if not account_id:
        return None
    account = await db.account.find_first(where={"id": account_id, "userId": user_id})
    if account is None:
        raise nest_error(404, "Account not found")
    return account


async def _assert_category(user_id: str, category_id) -> None:
    if not category_id:
        return
    category = await db.category.find_first(where={"id": category_id, "userId": user_id})
    if category is None:
        raise nest_error(404, "Category not found")
    if category.scope != "EXPENSE":
        raise nest_error(400, "Recurring payments need an expense category")


async def _pay_internal(user_id: str, p, *, when: datetime, amount=None, account_id, notes=None, auto=False):
    """Race-safe: a conditional update on nextDate and the expense in one
    transaction. None when the cycle was already moved on."""
    nxt = rrule.next_occurrence(p.rrule, p.startDate, p.nextDate)
    value = amount if amount is not None else to_number(p.amount)
    async with db.tx() as tx:
        moved = await tx.recurringpayment.update_many(
            where={"id": p.id, "userId": user_id, "nextDate": p.nextDate, "status": {"not": "CANCELLED"}},
            data={"nextDate": nxt} if nxt else {"status": "CANCELLED", "cancelledAt": datetime.now(timezone.utc)},
        )
        if moved != 1:
            return None
        created = await tx.transaction.create(
            data={
                "userId": user_id,
                "type": "EXPENSE",
                "title": p.name,
                "amount": as_decimal(value),
                "currency": p.currency,
                "date": when,
                "categoryId": p.categoryId,
                "accountId": account_id,
                "paymentMethod": p.paymentMethod,
                "recurringPaymentId": p.id,
                "notes": notes,
            }
        )
    await log_activity(
        user_id, "recurring.paid", entity_id=p.id, name=p.name, amount=value, currency=p.currency,
        transactionId=created.id, paidFor=dates.format_date_only(p.nextDate), auto=auto,
    )
    await log_activity(
        user_id, "transaction.created", entity_id=created.id, type="EXPENSE", amount=value, currency=p.currency,
        categoryId=p.categoryId, title=p.name, recurringPaymentId=p.id,
    )
    # NestJS's "recurring.paid" listener: a loan's installment is a payment on its debt
    from app.os.debts import on_recurring_paid

    await on_recurring_paid(user_id, p.id, value, p.currency, created.id)
    return {f: getattr(created, f) for f in _TX_FIELDS}


async def auto_log_due(user_id: str) -> int:
    """Pay ACTIVE self-logging payments that are due (idempotent)."""
    settings = await db.usersettings.find_unique(where={"userId": user_id})
    tz = fu.safe_tz(settings.timezone if settings else None)
    note = LOGGED_AUTOMATICALLY[to_lang(settings.language if settings else None)]
    today = dates.today_date_only(tz)
    due = await db.recurringpayment.find_many(
        where={"userId": user_id, "status": "ACTIVE", "autoLogExpense": True, "nextDate": {"lte": today}}
    )
    created = 0
    for original in due:
        p = original
        for _ in range(MAX_AUTO_CATCH_UP):
            if p is None or p.status != "ACTIVE" or dates._utc(p.nextDate) > today:
                break
            tx = await _pay_internal(
                user_id, p, when=fu.date_only_to_noon(p.nextDate, tz), account_id=p.accountId, notes=note, auto=True
            )
            if tx is None:
                break
            created += 1
            p = await db.recurringpayment.find_first(where={"id": p.id, "userId": user_id})
    return created


# ---------------------------------------------------------------------------
# Bodies


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    amount: object = None
    currency: CurrencyCode | None = None
    kind: KindName | None = None
    rrule: StrictStr | None = None
    startDate: StrictStr | None = None
    nextDate: StrictStr | None = None
    status: StatusName | None = None
    categoryId: StrictStr | None = None
    accountId: StrictStr | None = None
    paymentMethod: PaymentMethodName | None = None
    reminderDaysBefore: StrictInt | None = Field(default=None, ge=0, le=60)
    autoLogExpense: StrictBool | None = None
    url: StrictStr | None = Field(default=None, max_length=500)
    notes: StrictStr | None = Field(default=None, max_length=5000)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 100)

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        return validators.money(v, "amount", 0)

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v):
        problem = None if v is None else rrule.validate_rrule(v)
        if problem:
            raise ValueError(f"rrule {problem}")
        return v

    @field_validator("startDate", "nextDate")
    @classmethod
    def _dates(cls, v, info):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError(f"{info.field_name} must be a valid ISO 8601 date string")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        if v is not None and not _URL.match(v):
            raise ValueError("url must be a valid URL")
        return v


class CreateBody(_Fields):
    name: StrictStr
    amount: object

    @field_validator("amount")
    @classmethod
    def _required_amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return validators.money(v, "amount", 0)


class UpdateBody(_Fields):
    pass


class PayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: StrictStr | None = None
    amount: object = None
    accountId: StrictStr | None = None
    notes: StrictStr | None = Field(default=None, max_length=5000)

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        return validators.money(v, "amount", 0.01)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v is not None and not dates.is_iso_date_string(v):
            raise ValueError("date must be a valid ISO 8601 date string")
        return v


def _kind_filters(request: Request, errors: list) -> dict | None:
    kinds = string_list(request.query_params.getlist("kind"))
    excluded = string_list(request.query_params.getlist("excludeKind"))
    for name, values in (("kind", kinds), ("excludeKind", excluded)):
        if values and any(v not in KINDS for v in values):
            errors.append(f"each value in {name} must be one of the following values: {', '.join(KINDS)}")
    if not kinds and not excluded:
        return None
    return {**({"in": kinds} if kinds else {}), **({"not_in": excluded} if excluded else {})}


async def _get(user, id: str) -> dict:
    row = await db.recurringpayment.find_first(where={"id": id, "userId": user.id}, include={"category": True, "account": True})
    if row is None:
        raise nest_error(404, "Recurring payment not found")
    [view] = await _with_computed(user.id, [row], await _context(user))
    recent = await db.transaction.find_many(where={"userId": user.id, "recurringPaymentId": id}, order={"date": "desc"}, take=12)
    return {**view, "recentPayments": [{f: getattr(t, f) for f in ("id", "amount", "currency", "date", "accountId", "notes")} for t in recent]}


# ---------------------------------------------------------------------------
# Routes


@router.get("")
async def list_payments(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors: list[str] = []
    kind = _kind_filters(request, errors)
    statuses = string_list(q.getlist("status"))
    if statuses and any(s not in STATUSES for s in statuses):
        errors.append(f"each value in status must be one of the following values: {', '.join(STATUSES)}")
    text = q.get("q")
    if text is not None and not 1 <= len(text) <= 100:
        errors.append("q must be longer than or equal to 1 and shorter than or equal to 100 characters")
    if errors:
        raise nest_error(400, errors)
    await auto_log_due(user.id)
    where: dict = {"userId": user.id}
    if kind:
        where["kind"] = kind
    if statuses:
        where["status"] = {"in": statuses}
    for field in ("categoryId", "accountId"):
        if q.get(field):
            where[field] = q.get(field)
    if text and text.strip():
        contains = {"contains": text.strip(), "mode": "insensitive"}
        where["OR"] = [{"name": contains}, {"notes": contains}]
    rows = await db.recurringpayment.find_many(
        where=where, include={"category": True, "account": True}, order=[{"status": "asc"}, {"nextDate": "asc"}, {"name": "asc"}]
    )
    items = await _with_computed(user.id, rows, await _context(user))
    return {"items": items, "total": len(items)}


@router.get("/summary")
async def summary(request: Request, user=Depends(plus_user)):
    """Totals of ACTIVE payments in the default currency, and what falls due in the next `days`."""
    errors: list[str] = []
    kind = _kind_filters(request, errors)
    raw_days = request.query_params.get("days")
    days = 30
    if raw_days is not None:
        try:
            number = float(raw_days)
        except ValueError:
            number = None
        if number is None or number != int(number) or not 1 <= number <= 365:
            errors.append("days must be an integer between 1 and 365")
        else:
            days = int(number)
    if errors:
        raise nest_error(400, errors)
    await auto_log_due(user.id)
    ctx = await _context(user)
    where: dict = {"userId": user.id, **({"kind": kind} if kind else {})}
    rows = await db.recurringpayment.find_many(where=where, include={"category": True, "account": True}, order={"nextDate": "asc"})
    items = await _with_computed(user.id, rows, ctx)
    active = [i for i in items if i["status"] == "ACTIVE"]
    today = ctx["today"]
    horizon = today + days * DAY
    month_end = dates._date_only(date(today.year, today.month, fu.days_in_month(today.year, today.month)))
    month_start = dates.start_of_month(datetime.now(timezone.utc), ctx["tz"])
    fx = ctx["fx"]

    upcoming, due_rest_of_month = [], 0
    for p in active:
        for idx, d in enumerate(_occurrences(p, max(horizon, month_end))):
            in_default = fx.to_default(p["amount"], p["currency"])
            if d <= month_end:
                due_rest_of_month += in_default
            if d > horizon:
                continue
            upcoming.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "kind": p["kind"],
                    "amount": to_number(p["amount"]),
                    "currency": p["currency"],
                    "amountInDefault": in_default,
                    "date": dates.format_date_only(d),
                    "daysUntil": dates.diff_days(today, d),
                    "isOverdue": d < today,
                    "isNext": idx == 0,
                    "autoLogExpense": p["autoLogExpense"],
                    "category": p["category"],
                    "accountId": p["accountId"],
                }
            )
    upcoming.sort(key=lambda u: (u["date"], u["name"].casefold(), u["name"]))

    by_category: dict = {}
    by_kind: dict = {}
    for p in active:
        key = p["categoryId"]
        cat = p["category"]
        entry = by_category.setdefault(
            key,
            {
                "categoryId": key,
                "name": (cat["name"] if cat else None) or UNCATEGORIZED[ctx["lang"]],
                "color": (cat["color"] if cat else None) or "#94a3b8",
                "icon": (cat["icon"] if cat else None) or "circle-help",
                "monthlyTotal": 0,
                "count": 0,
            },
        )
        entry["monthlyTotal"] += p["monthlyCostInDefault"]
        entry["count"] += 1
        k = by_kind.setdefault(p["kind"], {"kind": p["kind"], "monthlyTotal": 0, "count": 0})
        k["monthlyTotal"] += p["monthlyCostInDefault"]
        k["count"] += 1

    paid_where = {
        "userId": user.id,
        "type": "EXPENSE",
        "date": {"gte": month_start},
        "recurringPayment": {"is": {"kind": kind}} if kind else {"is_not": None},
    }
    paid_sums: dict[str, Decimal] = {}
    for t in await db.transaction.find_many(where=paid_where):
        paid_sums[t.currency] = paid_sums.get(t.currency, Decimal(0)) + t.amount
    paid_this_month = sum(fx.to_default(amount, currency) for currency, amount in paid_sums.items())
    monthly_total = sum(p["monthlyCostInDefault"] for p in active)
    rounded = lambda groups: sorted(  # noqa: E731
        ({**g, "monthlyTotal": round_money(g["monthlyTotal"])} for g in groups), key=lambda g: -g["monthlyTotal"]
    )
    return {
        "currency": fx.default_currency,
        "monthlyTotal": round_money(monthly_total),
        "yearlyTotal": round_money(monthly_total * 12),
        "activeCount": len(active),
        "pausedCount": sum(1 for i in items if i["status"] == "PAUSED"),
        "cancelledCount": sum(1 for i in items if i["status"] == "CANCELLED"),
        "overdueCount": sum(1 for i in active if i["isOverdue"]),
        "dueSoonCount": sum(1 for i in active if i["isDueSoon"]),
        "upcomingDays": days,
        "upcomingTotal": round_money(sum(u["amountInDefault"] for u in upcoming)),
        "upcoming": upcoming[:100],
        "paidThisMonth": round_money(paid_this_month),
        "dueRestOfMonth": round_money(due_rest_of_month),
        "byCategory": rounded(by_category.values()),
        "byKind": rounded(by_kind.values()),
    }


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    return await _get(user, (await create_payment(user, body)).id)


async def create_payment(user, body: CreateBody):
    """Creates a recurring payment (also used for a debt's installment plan)."""
    ctx = await _context(user)
    await _assert_category(user.id, body.categoryId)
    account = await _find_account(user.id, body.accountId)
    rule = rrule.normalize_rrule(body.rrule or "FREQ=MONTHLY")
    start = dates.parse_date_only(body.startDate or body.nextDate) or ctx["today"]
    nxt = dates.parse_date_only(body.nextDate) or _first_on_or_after(rule, start, ctx["today"])
    status = body.status or "ACTIVE"
    row = await db.recurringpayment.create(
        data={
            "userId": user.id,
            "kind": body.kind or "BILL",
            "name": body.name,
            "amount": as_decimal(body.amount),
            "currency": body.currency or (account.currency if account else None) or ctx["fx"].default_currency,
            "rrule": rule,
            "startDate": start,
            "nextDate": nxt,
            "status": status,
            "cancelledAt": datetime.now(timezone.utc) if status == "CANCELLED" else None,
            "categoryId": body.categoryId or None,
            "accountId": body.accountId or None,
            "paymentMethod": body.paymentMethod,
            "reminderDaysBefore": 2 if "reminderDaysBefore" not in body.model_fields_set else body.reminderDaysBefore,
            "autoLogExpense": bool(body.autoLogExpense),
            "url": body.url,
            "notes": body.notes,
        }
    )
    await log_activity(user.id, "recurring.created", entity_id=row.id, name=row.name, kind=row.kind, amount=body.amount)
    if row.autoLogExpense:
        await auto_log_due(user.id)
    return row


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await _get(user, id)


async def _update(user, id: str, body: UpdateBody) -> dict:
    existing = await _owned(user.id, id)
    ctx = await _context(user)
    sent = body.model_fields_set
    if "categoryId" in sent:
        await _assert_category(user.id, body.categoryId)
    if "accountId" in sent:
        await _find_account(user.id, body.accountId)
    rule = rrule.normalize_rrule(body.rrule) if body.rrule else existing.rrule
    start = (dates.parse_date_only(body.startDate) or existing.startDate) if body.startDate else existing.startDate
    nxt = (dates.parse_date_only(body.nextDate) or existing.nextDate) if body.nextDate else existing.nextDate
    status = body.status or existing.status
    cancelled_at = existing.cancelledAt
    if status == "CANCELLED" and existing.status != "CANCELLED":
        cancelled_at = datetime.now(timezone.utc)
    if status != "CANCELLED":
        cancelled_at = None
    # Reactivating a paused or cancelled payment skips the missed cycles.
    if status == "ACTIVE" and existing.status != "ACTIVE" and not body.nextDate and dates._utc(nxt) < ctx["today"]:
        nxt = _first_on_or_after(rule, start, ctx["today"])
    data = {"rrule": rule, "startDate": start, "nextDate": nxt, "status": status, "cancelledAt": cancelled_at}
    for field in ("kind", "name", "currency", "autoLogExpense"):
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    if body.amount is not None:
        data["amount"] = as_decimal(body.amount)
    for field in ("categoryId", "accountId"):
        if field in sent:
            data[field] = getattr(body, field) or None
    for field in ("paymentMethod", "reminderDaysBefore", "url", "notes"):
        if field in sent:
            data[field] = getattr(body, field)
    await db.recurringpayment.update(where={"id": existing.id}, data=data)
    if status != existing.status:
        verb = {"CANCELLED": "cancelled", "PAUSED": "paused"}.get(status, "reactivated")
    else:
        verb = "updated"
    await log_activity(user.id, f"recurring.{verb}", entity_id=id, name=body.name or existing.name)
    if status == "ACTIVE":
        await auto_log_due(user.id)
    return await _get(user, id)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    return await _update(user, id, body)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    await db.recurringpayment.delete(where={"id": existing.id})  # its transactions stay, unlinked
    await log_activity(user.id, "recurring.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


@router.post("/{id}/pay")
async def pay(id: str, body: PayBody | None = None, user=Depends(plus_user)):
    """Records this cycle's expense (category, account and method copied) and moves nextDate on."""
    body = body or PayBody()
    p = await _owned(user.id, id)
    if p.status == "CANCELLED":
        raise nest_error(400, "This payment is cancelled — reactivate it first")
    account_id = p.accountId if "accountId" not in body.model_fields_set else (body.accountId or None)
    if body.accountId:
        await _find_account(user.id, body.accountId)
    ctx = await _context(user)
    when = fu.parse_instant(body.date, ctx["tz"]) or datetime.now(timezone.utc)
    tx = await _pay_internal(user.id, p, when=when, amount=body.amount, account_id=account_id, notes=body.notes)
    if tx is None:
        raise nest_error(409, "This payment was already recorded — refresh and try again")
    return {"payment": await _get(user, id), "transaction": tx}


@router.post("/{id}/skip")
async def skip(id: str, user=Depends(plus_user)):
    """Moves nextDate on without paying."""
    p = await _owned(user.id, id)
    if p.status == "CANCELLED":
        raise nest_error(400, "This payment is cancelled — reactivate it first")
    nxt = rrule.next_occurrence(p.rrule, p.startDate, p.nextDate)
    moved = await db.recurringpayment.update_many(
        where={"id": p.id, "userId": user.id, "nextDate": p.nextDate},
        data={"nextDate": nxt} if nxt else {"status": "CANCELLED", "cancelledAt": datetime.now(timezone.utc)},
    )
    if moved != 1:
        raise nest_error(409, "This payment changed meanwhile — refresh and try again")
    await log_activity(
        user.id, "recurring.skipped", entity_id=id, name=p.name, skippedDate=dates.format_date_only(p.nextDate),
        nextDate=dates.format_date_only(nxt) if nxt else None,
    )
    return await _get(user, id)


@router.post("/{id}/cancel")
async def cancel(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(status="CANCELLED"))


@router.post("/{id}/pause")
async def pause(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(status="PAUSED"))


@router.post("/{id}/reactivate")
async def reactivate(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(status="ACTIVE"))
