"""/api/debts (port of vournalOS DebtsService and debts.util).

Money owed both ways, from a friend's €20 to a car loan. What's left is always
worked out from the payments. A payment can also record the matching transaction,
and a loan can be tied to a LOAN recurring payment whose installments become
payments here (NestJS did that, and reopening on a deleted transaction, through
event listeners; here the paying and deleting code calls the hooks below).
"""

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os.calendar_utils import local_date_time
from app.os.common import dates, rrule, validators
from app.os.common.activity import log_activity
from app.os.common.currency import get_converter, round_money, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.ownership import assert_owned
from app.os.profile import ensure_os_profile
from app.os.transactions import as_decimal

log = logging.getLogger("vournal.os")
router = nest_router("/debts")

DirectionName = Literal["I_OWE", "OWED_TO_ME"]
KindName = Literal["PERSONAL", "LOAN", "CREDIT_CARD", "OTHER"]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
PAID_EPSILON = 0.005  # below this a debt counts as fully paid (rounding of split payments)
_EPSILON = 2.220446049250313e-16  # Number.EPSILON
_INCLUDE = {"person": True, "account": True, "recurringPayment": True, "payments": True}


# ----- debt maths (debts.util) -------------------------------------------------------------


def _round2(n: float) -> float:
    return math.floor((n + _EPSILON) * 100 + 0.5) / 100


def debt_progress(principal: float, payments: list[float]) -> dict:
    paid = _round2(sum(payments))
    remaining = max(0, _round2(principal - paid))
    percent = min(100, math.floor((paid / principal) * 100 + 0.5)) if principal > 0 else 100
    return {"paid": paid, "remaining": remaining, "percentPaid": percent}


def days_until(today: str, due: str | None) -> int | None:
    if not due:
        return None
    return round((dates.parse_date_only(due) - dates.parse_date_only(today)).days)


def exceeds_remaining(amount: float, remaining: float) -> bool:
    """A payment may not take the debt below zero (a cent of slack for rounding)."""
    return amount - remaining > 0.01


# ----- views ---------------------------------------------------------------------------------


async def _context(user):
    settings = await ensure_os_profile(user)
    return {"tz": settings.timezone, "fx": await get_converter(user.id), "today": dates.format_date_only(dates.today_date_only(settings.timezone))}


def _view(r, ctx) -> dict:
    principal = to_number(r.principal)
    payments = r.payments or []
    progress = debt_progress(principal, [to_number(p.amount) for p in payments])
    is_open = r.status == "OPEN"
    due = dates.format_date_only(r.dueDate) if r.dueDate else None
    days = days_until(ctx["today"], due) if is_open else None
    last = max((p.date for p in payments), default=None)
    plan = r.recurringPayment
    return {
        "id": r.id,
        "direction": r.direction,
        "kind": r.kind,
        "title": r.title,
        "personId": r.personId,
        "person": {"id": r.person.id, "name": r.person.name, "avatarColor": r.person.avatarColor} if r.person else None,
        "counterparty": r.counterparty,
        "who": r.person.name if r.person else r.counterparty,
        "principal": principal,
        "currency": r.currency,
        "startDate": r.startDate,
        "dueDate": r.dueDate,
        "interestRate": None if r.interestRate is None else to_number(r.interestRate),
        "status": r.status,
        "settledAt": r.settledAt,
        "accountId": r.accountId,
        "account": {"id": r.account.id, "name": r.account.name, "currency": r.account.currency} if r.account else None,
        "recurringPaymentId": r.recurringPaymentId,
        "recurring": (
            {
                "id": plan.id, "name": plan.name, "amount": to_number(plan.amount), "currency": plan.currency,
                "rrule": plan.rrule, "nextDate": plan.nextDate, "status": plan.status, "autoLogExpense": plan.autoLogExpense,
            }
            if plan
            else None
        ),
        "reminderDaysBefore": r.reminderDaysBefore,
        "notes": r.notes,
        "createdAt": r.createdAt,
        "updatedAt": r.updatedAt,
        **progress,
        "paymentCount": len(payments),
        "lastPaymentDate": last,
        "daysUntilDue": days,
        "isOverdue": days is not None and days < 0 and progress["remaining"] > 0,
        "remainingInDefault": ctx["fx"].to_default(progress["remaining"], r.currency) if is_open else 0,
    }


async def _summary(user_id: str, ctx) -> dict:
    rows = await db.debt.find_many(where={"userId": user_id, "status": "OPEN"}, include=_INCLUDE, take=1000)
    views = [_view(r, ctx) for r in rows]
    total = lambda d: round_money(sum(v["remainingInDefault"] for v in views if v["direction"] == d))  # noqa: E731
    i_owe, owed = total("I_OWE"), total("OWED_TO_ME")
    soon = sorted(
        (v for v in views if v["daysUntilDue"] is not None and v["daysUntilDue"] <= 7 and v["remaining"] > 0),
        key=lambda v: v["daysUntilDue"],
    )[:5]
    return {
        "currency": ctx["fx"].default_currency,
        "iOwe": i_owe,
        "owedToMe": owed,
        "net": round_money(owed - i_owe),
        "openCount": len(views),
        "iOweCount": sum(1 for v in views if v["direction"] == "I_OWE"),
        "owedToMeCount": sum(1 for v in views if v["direction"] == "OWED_TO_ME"),
        "overdueCount": sum(1 for v in views if v["isOverdue"]),
        "dueSoon": [
            {k: v[k] for k in ("id", "title", "who", "direction", "remaining", "currency", "dueDate", "daysUntilDue")} for v in soon
        ],
    }


async def _get(user, id: str) -> dict:
    row = await db.debt.find_first(where={"id": id, "userId": user.id}, include=_INCLUDE)
    if row is None:
        raise nest_error(404, "Debt not found")
    ctx = await _context(user)
    payments = await db.debtpayment.find_many(
        where={"debtId": id, "userId": user.id}, order=[{"date": "desc"}, {"createdAt": "desc"}]
    )
    return {
        **_view(row, ctx),
        "payments": [
            {"id": p.id, "amount": to_number(p.amount), "date": p.date, "note": p.note, "transactionId": p.transactionId, "createdAt": p.createdAt}
            for p in payments
        ],
    }


# ----- helpers -------------------------------------------------------------------------------


async def _owned(user_id: str, id: str):
    row = await db.debt.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Debt not found")
    return row


async def _assert_links(user_id: str, person_id, account_id, recurring_id) -> None:
    await assert_owned("person", person_id or None, user_id)
    await assert_owned("account", account_id or None, user_id)
    await assert_owned("recurringpayment", recurring_id or None, user_id)


async def _assert_recurring_free(recurring_id: str, debt_id: str | None) -> None:
    where = {"recurringPaymentId": recurring_id, **({"id": {"not": debt_id}} if debt_id else {})}
    if await db.debt.find_first(where=where):
        raise nest_error(409, "That recurring payment already belongs to another debt")


async def _remaining_of(client, debt_id: str, principal: float) -> float:
    # an exact Decimal sum, as Prisma's aggregate _sum gave it
    paid = sum((p.amount for p in await client.debtpayment.find_many(where={"debtId": debt_id})), Decimal(0))
    return debt_progress(principal, [to_number(paid)])["remaining"]


async def _stop_plan(client, recurring_id) -> None:
    if recurring_id:
        await client.recurringpayment.update_many(
            where={"id": recurring_id, "status": {"not": "CANCELLED"}},
            data={"status": "CANCELLED", "cancelledAt": datetime.now(timezone.utc)},
        )


async def _settle_state(client, debt_id: str) -> None:
    """OPEN ↔ SETTLED from the payments; settling also stops the installment plan.
    Cancelled debts are left alone."""
    debt = await client.debt.find_unique(where={"id": debt_id})
    if debt is None or debt.status == "CANCELLED":
        return
    remaining = await _remaining_of(client, debt_id, to_number(debt.principal))
    if remaining <= PAID_EPSILON and debt.status == "OPEN":
        await client.debt.update(where={"id": debt_id}, data={"status": "SETTLED", "settledAt": datetime.now(timezone.utc)})
        await _stop_plan(client, debt.recurringPaymentId)
    elif remaining > PAID_EPSILON and debt.status == "SETTLED":
        await client.debt.update(where={"id": debt_id}, data={"status": "OPEN", "settledAt": None})


def _noon_of(day: datetime, tz: str) -> datetime:
    """A past day is stored at 12:00 local; today is "now"."""
    if dates._utc(day) == dates.today_date_only(tz):
        return datetime.now(timezone.utc)
    return local_date_time(dates.format_date_only(day), "12:00", tz)


async def _record_transaction(client, user_id: str, *, kind: str, title: str, amount: float, currency: str, when: datetime, account_id) -> str:
    tx = await client.transaction.create(
        data={"userId": user_id, "type": kind, "title": title, "amount": as_decimal(amount), "currency": currency, "date": when, "accountId": account_id}
    )
    await log_activity(user_id, "transaction.created", entity_id=tx.id, type=kind, amount=amount, currency=currency, title=title, categoryId=None)
    return tx.id


# ----- hooks (NestJS event listeners) --------------------------------------------------------


async def on_recurring_paid(user_id: str, recurring_id: str, amount: float, currency: str, transaction_id: str) -> None:
    """Paying (or auto-logging) a loan's linked recurring payment records a debt payment."""
    try:
        debt = await db.debt.find_first(where={"userId": user_id, "recurringPaymentId": recurring_id, "status": "OPEN"})
        if debt is None:
            return
        fx = await get_converter(user_id)
        value = fx.convert(amount, currency, debt.currency)
        tx = await db.transaction.find_first(where={"id": transaction_id, "userId": user_id})
        settings = await db.usersettings.find_unique(where={"userId": user_id})
        tz = settings.timezone if settings else "UTC"
        async with db.tx() as client:
            remaining = await _remaining_of(client, debt.id, to_number(debt.principal))
            if remaining <= PAID_EPSILON:
                return
            await client.debtpayment.create(
                data={
                    "userId": user_id,
                    "debtId": debt.id,
                    "amount": as_decimal(min(value, remaining)),
                    "date": dates.to_date_only(tx.date if tx else datetime.now(timezone.utc), tz),
                    "transactionId": transaction_id,
                }
            )
            await _settle_state(client, debt.id)
        await log_activity(user_id, "debt.paymentAdded", entity_id=debt.id, title=debt.title, amount=value, currency=debt.currency)
    except Exception as exc:  # noqa: BLE001 — NestJS logged and carried on
        log.warning("could not record installment on debt: %s", exc)


async def on_transaction_deleted(user_id: str) -> None:
    """A deleted transaction takes its debt payment with it: a settled debt may reopen."""
    try:
        for debt in await db.debt.find_many(where={"userId": user_id, "status": "SETTLED"}):
            async with db.tx() as client:
                await _settle_state(client, debt.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not re-check debts: %s", exc)


# ----- bodies --------------------------------------------------------------------------------


def _iso(v, field):
    if v is not None and not dates.is_iso_date_string(v):
        raise ValueError(f"{field} must be a valid ISO 8601 date string")
    return v


def _bounded_money(v, field, minimum, maximum):
    """IsNumber({maxDecimalPlaces: 2}) + Min + Max."""
    if v is not None and validators.money(v, field, minimum) > maximum:
        raise ValueError(f"{field} must not be greater than {maximum}")
    return v


def _rate(v):
    """IsNumber({maxDecimalPlaces: 3}) + Min(0) + Max(1000)."""
    if v is None:
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or validators._decimal_places(v) > 3:
        raise ValueError("interestRate must be a number conforming to the specified constraints")
    if v < 0:
        raise ValueError("interestRate must not be less than 0")
    if v > 1000:
        raise ValueError("interestRate must not be greater than 1000")
    return v


class Installments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: object
    rrule: StrictStr | None = None
    firstDate: StrictStr
    categoryId: StrictStr | None = None
    autoLog: StrictBool | None = None

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return _bounded_money(v, "amount", 0.01, 99_999_999)

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v):
        problem = None if v is None else rrule.validate_rrule(v)
        if problem:
            raise ValueError(f"rrule {problem}")
        return v

    @field_validator("firstDate")
    @classmethod
    def _first(cls, v):
        return _iso(v, "firstDate")


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: DirectionName | None = None
    kind: KindName | None = None
    title: StrictStr | None = None
    personId: StrictStr | None = None
    counterparty: StrictStr | None = None
    principal: object = None
    currency: CurrencyCode | None = None
    startDate: StrictStr | None = None
    dueDate: StrictStr | None = None
    interestRate: object = None
    accountId: StrictStr | None = None
    recurringPaymentId: StrictStr | None = None
    reminderDaysBefore: StrictInt | None = Field(default=None, ge=0, le=365)
    notes: StrictStr | None = Field(default=None, max_length=5000)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("counterparty")
    @classmethod
    def _counterparty(cls, v):
        return validators.trimmed(v, "counterparty", 0, 120)

    @field_validator("principal")
    @classmethod
    def _principal(cls, v):
        return _bounded_money(v, "principal", 0.01, 99_999_999_999)

    @field_validator("interestRate")
    @classmethod
    def _rate(cls, v):
        return _rate(v)

    @field_validator("startDate", "dueDate")
    @classmethod
    def _dates(cls, v, info):
        return _iso(v, info.field_name)


class CreateBody(_Fields):
    direction: DirectionName
    title: StrictStr
    principal: object
    installments: Installments | None = None
    recordTransaction: StrictBool | None = None

    @field_validator("title")
    @classmethod
    def _required_title(cls, v):
        return validators.trimmed(v, "title", 1, 200)

    @field_validator("principal")
    @classmethod
    def _required_principal(cls, v):
        if v is None:
            raise ValueError("principal must be a number conforming to the specified constraints")
        return _bounded_money(v, "principal", 0.01, 99_999_999_999)


class UpdateBody(_Fields):
    pass


class SettleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: StrictStr | None = None
    note: StrictStr | None = None
    recordTransaction: StrictBool | None = None
    accountId: StrictStr | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _iso(v, "date")

    @field_validator("note")
    @classmethod
    def _note(cls, v):
        return validators.trimmed(v, "note", 0, 500)


class PaymentBody(SettleBody):
    amount: object

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return _bounded_money(v, "amount", 0.01, 99_999_999_999)


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["OPEN", "CANCELLED"]


# ----- routes --------------------------------------------------------------------------------


@router.get("")
async def list_debts(request: Request, user=Depends(plus_user)):
    q = request.query_params
    errors: list[str] = []
    status = q.get("status")
    if status is not None and status not in ("open", "closed", "all"):
        errors.append("status must be one of the following values: open, closed, all")
    direction = q.get("direction")
    if direction is not None and direction not in ("I_OWE", "OWED_TO_ME"):
        errors.append("direction must be one of the following values: I_OWE, OWED_TO_ME")
    text = q.get("q")
    if text is not None:
        text = text.strip()
        if len(text) > 100:
            errors.append("q must be shorter than or equal to 100 characters")
    if errors:
        raise nest_error(400, errors)
    status = status or "open"
    where: dict = {"userId": user.id}
    if status == "open":
        where["status"] = "OPEN"
    elif status == "closed":
        where["status"] = {"not": "OPEN"}
    if direction:
        where["direction"] = direction
    if q.get("personId"):
        where["personId"] = q.get("personId")
    if text:
        contains = {"contains": text, "mode": "insensitive"}
        where["OR"] = [{"title": contains}, {"counterparty": contains}, {"person": {"is": {"name": contains}}}]
    rows = await db.debt.find_many(
        where=where, include=_INCLUDE, order=[{"dueDate": {"sort": "asc", "nulls": "last"}}, {"createdAt": "desc"}], take=500
    )
    ctx = await _context(user)
    items = [_view(r, ctx) for r in rows]
    if status != "open":
        items.sort(key=lambda v: (v["status"] != "OPEN", -dates._utc(v["settledAt"] or v["updatedAt"]).timestamp()))
    return {"items": items, "total": len(items), "summary": await _summary(user.id, ctx)}


@router.get("/summary")
async def summary(user=Depends(plus_user)):
    return await _summary(user.id, await _context(user))


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await _get(user, id)


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    from app.os import recurring_payments

    ctx = await _context(user)
    await _assert_links(user.id, body.personId, body.accountId, body.recurringPaymentId)
    if body.recurringPaymentId:
        await _assert_recurring_free(body.recurringPaymentId, None)
    if body.recurringPaymentId and body.installments:
        raise nest_error(400, "Link an existing recurring payment or create installments, not both")
    if not body.personId and not (body.counterparty or "").strip() and body.kind not in ("CREDIT_CARD", "LOAN"):
        # a personal debt with nobody attached would be impossible to read later
        raise nest_error(400, "Say who the debt is with (a person or a name)")
    account = await db.account.find_first(where={"id": body.accountId, "userId": user.id}) if body.accountId else None
    currency = body.currency or (account.currency if account else None) or ctx["fx"].default_currency
    start = dates.parse_date_only(body.startDate) or dates.today_date_only(ctx["tz"])
    due = dates.parse_date_only(body.dueDate)
    if due and due < start:
        raise nest_error(400, "The due date is before the start date")

    recurring_id = body.recurringPaymentId or None
    if body.installments:
        if body.direction != "I_OWE":
            raise nest_error(400, "Installments are only for money you owe")
        if body.installments.categoryId:
            await assert_owned("category", body.installments.categoryId, user.id)
        # made without auto-log first, so the first installment can't be paid before the link exists
        plan = await recurring_payments.create_payment(
            user,
            recurring_payments.CreateBody(
                kind="LOAN",
                name=body.title,
                amount=body.installments.amount,
                currency=currency,
                rrule=body.installments.rrule or "FREQ=MONTHLY",
                startDate=body.installments.firstDate,
                nextDate=body.installments.firstDate,
                categoryId=body.installments.categoryId,
                accountId=body.accountId,
                reminderDaysBefore=2 if "reminderDaysBefore" not in body.model_fields_set else body.reminderDaysBefore,
                autoLogExpense=False,
            ),
        )
        recurring_id = plan.id

    async with db.tx() as client:
        debt = await client.debt.create(
            data={
                "userId": user.id,
                "direction": body.direction,
                "kind": body.kind or "PERSONAL",
                "title": body.title,
                "personId": body.personId or None,
                "counterparty": (body.counterparty or "").strip() or None,
                "principal": as_decimal(body.principal),
                "currency": currency,
                "startDate": start,
                "dueDate": due,
                "interestRate": None if body.interestRate is None else as_decimal(body.interestRate),
                "accountId": body.accountId or None,
                "recurringPaymentId": recurring_id,
                "reminderDaysBefore": 3 if "reminderDaysBefore" not in body.model_fields_set else body.reminderDaysBefore,
                "notes": body.notes,
            }
        )
        if body.recordTransaction:
            # lending money out is money leaving; borrowing is money coming in
            kind = "EXPENSE" if body.direction == "OWED_TO_ME" else "INCOME"
            await _record_transaction(
                client, user.id, kind=kind, title=body.title, amount=body.principal, currency=currency,
                when=_noon_of(start, ctx["tz"]), account_id=body.accountId or None,
            )
    if body.installments and body.installments.autoLog and recurring_id:
        await db.recurringpayment.update(where={"id": recurring_id}, data={"autoLogExpense": True})
        await recurring_payments.auto_log_due(user.id)
    await log_activity(
        user.id, "debt.created", entity_id=debt.id, title=debt.title, direction=debt.direction, amount=body.principal, currency=currency
    )
    return await _get(user, debt.id)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    await _assert_links(user.id, body.personId, body.accountId, body.recurringPaymentId)
    if body.recurringPaymentId:
        await _assert_recurring_free(body.recurringPaymentId, id)
    start = dates.parse_date_only(body.startDate) if "startDate" in sent else existing.startDate
    due = dates.parse_date_only(body.dueDate) if "dueDate" in sent else existing.dueDate
    if start and due and dates._utc(due) < dates._utc(start):
        raise nest_error(400, "The due date is before the start date")
    person_id = (body.personId or None) if "personId" in sent else existing.personId
    counterparty = ((body.counterparty or "").strip() or None) if "counterparty" in sent else existing.counterparty
    kind = body.kind or existing.kind
    if not person_id and not counterparty and kind not in ("CREDIT_CARD", "LOAN"):
        raise nest_error(400, "Say who the debt is with (a person or a name)")
    data: dict = {"personId": person_id, "counterparty": counterparty, "dueDate": due}
    for field in ("direction", "kind", "title", "currency", "reminderDaysBefore", "notes"):
        if field in sent:
            data[field] = getattr(body, field)
    if "principal" in sent:
        data["principal"] = as_decimal(body.principal)
    if "interestRate" in sent:
        data["interestRate"] = None if body.interestRate is None else as_decimal(body.interestRate)
    if start:
        data["startDate"] = start
    for field in ("accountId", "recurringPaymentId"):
        if field in sent:
            data[field] = getattr(body, field) or None
    await db.debt.update(where={"id": id}, data=data)
    if "principal" in sent:  # a new principal can settle or reopen it
        async with db.tx() as client:
            await _settle_state(client, id)
    await log_activity(user.id, "debt.updated", entity_id=id, title=body.title or existing.title)
    return await _get(user, id)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    """Deletes the debt and its payments; transactions already recorded stay (real history)."""
    existing = await _owned(user.id, id)
    async with db.tx() as client:
        await client.debtpayment.update_many(where={"debtId": id}, data={"transactionId": None})
        await client.debt.delete(where={"id": id})
    await log_activity(user.id, "debt.deleted", entity_id=id, title=existing.title)
    return {"ok": True}


async def _add_payment(user, id: str, body: SettleBody, amount: float) -> dict:
    debt = await _owned(user.id, id)
    if debt.status != "OPEN":
        raise nest_error(400, "This debt is closed — reopen it first")
    if body.accountId:
        await assert_owned("account", body.accountId, user.id)
    ctx = await _context(user)
    day = dates.parse_date_only(body.date) or dates.today_date_only(ctx["tz"])
    record = body.recordTransaction if body.recordTransaction is not None else debt.kind in ("LOAN", "CREDIT_CARD")
    account_id = (body.accountId or None) if "accountId" in body.model_fields_set else debt.accountId
    async with db.tx() as client:
        remaining = await _remaining_of(client, debt.id, to_number(debt.principal))
        if exceeds_remaining(amount, remaining):
            raise nest_error(400, f"That is more than what is left ({_js_number(remaining)} {debt.currency})")
        transaction_id = None
        if record:
            # paying back what I owe is money out; being paid back is money in
            transaction_id = await _record_transaction(
                client, user.id, kind="EXPENSE" if debt.direction == "I_OWE" else "INCOME", title=debt.title,
                amount=amount, currency=debt.currency, when=_noon_of(day, ctx["tz"]), account_id=account_id,
            )
        await client.debtpayment.create(
            data={
                "userId": user.id, "debtId": debt.id, "amount": as_decimal(min(amount, remaining)), "date": day,
                "note": body.note, "transactionId": transaction_id,
            }
        )
        await _settle_state(client, debt.id)
    await log_activity(user.id, "debt.paymentAdded", entity_id=debt.id, title=debt.title, amount=amount, currency=debt.currency)
    return await _get(user, id)


def _js_number(value: float) -> str:
    """How JavaScript prints a number in a template string (12.0 → "12")."""
    return str(int(value)) if value == int(value) else repr(value)


@router.post("/{id}/payments", status_code=201)
async def add_payment(id: str, body: PaymentBody, user=Depends(plus_user)):
    return await _add_payment(user, id, body, body.amount)


@router.delete("/{id}/payments/{payment_id}")
async def remove_payment(id: str, payment_id: str, user=Depends(plus_user)):
    """Removes a payment and the transaction it recorded."""
    await _owned(user.id, id)
    payment = await db.debtpayment.find_first(where={"id": payment_id, "debtId": id, "userId": user.id})
    if payment is None:
        raise nest_error(404, "Debt payment not found")
    async with db.tx() as client:
        if payment.transactionId:
            await client.transaction.delete(where={"id": payment.transactionId})  # the payment cascades
        else:
            await client.debtpayment.delete(where={"id": payment.id})
        await _settle_state(client, id)
    if payment.transactionId:
        await log_activity(user.id, "transaction.deleted", entity_id=payment.transactionId)
        await on_transaction_deleted(user.id)
    return await _get(user, id)


@router.post("/{id}/settle")
async def settle(id: str, body: SettleBody, user=Depends(plus_user)):
    """Pays whatever is left in one payment."""
    debt = await _owned(user.id, id)
    remaining = await _remaining_of(db, id, to_number(debt.principal))
    if remaining <= PAID_EPSILON:
        raise nest_error(409, "Nothing is left to pay")
    return await _add_payment(user, id, body, remaining)


@router.post("/{id}/status")
async def set_status(id: str, body: StatusBody, user=Depends(plus_user)):
    """OPEN (reopen) or CANCELLED (forgiven / written off)."""
    debt = await _owned(user.id, id)
    if body.status == "OPEN":
        await db.debt.update(where={"id": id}, data={"status": "OPEN", "settledAt": None})
        async with db.tx() as client:  # reopening something fully paid puts it straight back
            await _settle_state(client, id)
    else:
        await db.debt.update(where={"id": id}, data={"status": "CANCELLED", "settledAt": datetime.now(timezone.utc)})
        await _stop_plan(db, debt.recurringPaymentId)
    await log_activity(user.id, "debt.reopened" if body.status == "OPEN" else "debt.cancelled", entity_id=id, title=debt.title)
    return await _get(user, id)
