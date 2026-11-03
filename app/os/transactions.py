"""/api/transactions (port of vournalOS TransactionsService).

Amounts are always positive; type says which way money moves. A transfer leaves
one account and (in toAmount, when given, else converted) lands in another. A
repeating income/expense is a rolling series: the newest occurrence carries the
rule, and materialize_due() creates each occurrence once its date has come.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.db import db
from app.os import finance_utils as fu
from app.os.common import dates, rrule, validators
from app.os.common.activity import log_activity
from app.os.common.currency import Converter, get_converter, round_money, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.ownership import assert_owned
from app.os.common.query import string_list
from app.os.profile import ensure_os_profile
from app.os.tasks import _query_int
from app.os.transactions_import import mark_duplicates, suggest_categories

router = nest_router("/transactions")

TYPES = ("INCOME", "EXPENSE", "TRANSFER")
TypeName = Literal["INCOME", "EXPENSE", "TRANSFER"]
PaymentMethodName = Literal["CASH", "DEBIT_CARD", "CREDIT_CARD", "BANK_TRANSFER", "DIGITAL_WALLET", "CHECK", "OTHER"]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
SORTS = ("date", "-date", "amount", "-amount")
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 50, 200
MAX_CATCH_UP = 60  # occurrences made per series in one run (catch-up after downtime)
_FIELDS = (
    "id", "type", "title", "amount", "currency", "date", "categoryId", "accountId", "toAccountId", "toAmount",
    "paymentMethod", "source", "notes", "rrule", "recurringPaymentId", "projectId", "tripId", "goalId",
    "createdAt", "updatedAt",
)
INCLUDE = {
    "category": True, "account": True, "toAccount": True, "recurringPayment": True, "project": True, "trip": True, "goal": True,
}
_LINK_FIELDS = ("recurringPaymentId", "projectId", "tripId", "goalId")


async def _settings(user):
    settings = await ensure_os_profile(user)
    return settings, fu.safe_tz(settings.timezone), to_lang(settings.language)


def view(row, fx: Converter, tz: str, lang: str = "en") -> dict:
    nxt = rrule.next_occurrence(row.rrule, row.date, row.date, tz=tz) if row.rrule else None
    out = {f: getattr(row, f) for f in _FIELDS}
    out.update(
        category=fu.mini(row.category, fu.CATEGORY_MINI),
        account=fu.mini(row.account, fu.ACCOUNT_MINI),
        toAccount=fu.mini(row.toAccount, fu.ACCOUNT_MINI),
        recurringPayment=fu.mini(row.recurringPayment, ("id", "name", "kind")),
        project=fu.mini(row.project, ("id", "name")),
        trip=fu.mini(row.trip, ("id", "name")),
        goal=fu.mini(row.goal, ("id", "name")),
        amountInDefault=fx.to_default(row.amount, row.currency),
        defaultCurrency=fx.default_currency,
        isRecurring=bool(row.rrule),
        recurrenceLabel=rrule.describe(row.rrule, lang) if row.rrule else None,
        nextOccurrence=nxt,
    )
    return out


# ---------------------------------------------------------------------------
# Repeating series


async def materialize_due(user_id: str) -> int:
    """Create every due occurrence (date ≤ now) of each repeating series: the copy takes
    the rule, the previous one loses it. Idempotent and race-safe: the hand-over is a
    conditional "rrule IS NOT NULL" update inside a transaction."""
    now = datetime.now(timezone.utc)
    heads = await db.transaction.find_many(where={"userId": user_id, "rrule": {"not": None}, "date": {"lte": now}})
    if not heads:
        return 0
    settings = await db.usersettings.find_unique(where={"userId": user_id})
    tz = fu.safe_tz(settings.timezone if settings else None)
    created = 0
    for head in heads:
        cur = head
        for _ in range(MAX_CATCH_UP):
            if not cur.rrule:
                break
            nxt = rrule.next_occurrence(cur.rrule, cur.date, cur.date, tz=tz)
            if nxt is None or nxt > now:
                break
            rule, prev_id = cur.rrule, cur.id
            async with db.tx() as tx:
                released = await tx.transaction.update_many(
                    where={"id": prev_id, "userId": user_id, "rrule": {"not": None}}, data={"rrule": None}
                )
                copy = None
                if released == 1:
                    copy = await tx.transaction.create(
                        data={
                            "userId": user_id,
                            "type": cur.type,
                            "title": cur.title,
                            "amount": cur.amount,
                            "currency": cur.currency,
                            "date": nxt,
                            "categoryId": cur.categoryId,
                            "accountId": cur.accountId,
                            "toAccountId": cur.toAccountId,
                            "toAmount": cur.toAmount,
                            "paymentMethod": cur.paymentMethod,
                            "source": cur.source,
                            "notes": cur.notes,
                            "rrule": rule,
                            "projectId": cur.projectId,
                            "tripId": cur.tripId,
                            "goalId": cur.goalId,
                        }
                    )
            if copy is None:
                break  # someone else already moved this series on
            created += 1
            await log_activity(
                user_id, "transaction.created", entity_id=copy.id, type=copy.type, amount=to_number(copy.amount),
                currency=copy.currency, title=copy.title, recurring=True,
            )
            cur = copy
    return created


# ---------------------------------------------------------------------------
# Bodies


def _iso(value, field):
    if value is not None and not dates.is_iso_date_string(value):
        raise ValueError(f"{field} must be a valid ISO 8601 date string")
    return value


class _TxFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: TypeName | None = None
    amount: Any = None
    currency: CurrencyCode | None = None
    date: StrictStr | None = None
    title: StrictStr | None = None
    categoryId: StrictStr | None = None
    accountId: StrictStr | None = None
    toAccountId: StrictStr | None = None
    toAmount: Any = None
    paymentMethod: PaymentMethodName | None = None
    source: StrictStr | None = None
    notes: StrictStr | None = Field(default=None, max_length=5000)
    rrule: StrictStr | None = None
    recurringPaymentId: StrictStr | None = None
    projectId: StrictStr | None = None
    tripId: StrictStr | None = None
    goalId: StrictStr | None = None

    @field_validator("amount", "toAmount")
    @classmethod
    def _money(cls, v, info):
        return validators.money(v, info.field_name, 0.01)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return _iso(v, "date")

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return validators.trimmed(v, "title", 0, 200)

    @field_validator("source")
    @classmethod
    def _source(cls, v):
        return validators.trimmed(v, "source", 0, 120)

    @field_validator("rrule")
    @classmethod
    def _rrule(cls, v):
        problem = None if v is None else rrule.validate_rrule(v)
        if problem:
            raise ValueError(f"rrule {problem}")
        return v


class CreateBody(_TxFields):
    type: TypeName
    amount: Any

    @field_validator("amount")
    @classmethod
    def _required_amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return validators.money(v, "amount", 0.01)


class UpdateBody(_TxFields):
    pass


class ImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["INCOME", "EXPENSE"]
    amount: Any
    date: StrictStr
    title: StrictStr | None = Field(default=None, max_length=200)
    categoryId: StrictStr | None = None

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        if v is None:
            raise ValueError("amount must be a number conforming to the specified constraints")
        return validators.money(v, "amount", 0.01)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("date must match /^\\d{4}-\\d{2}-\\d{2}$/ regular expression")
        return v


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accountId: StrictStr | None = None
    currency: CurrencyCode | None = None
    rows: list[ImportRow] = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Helpers


def as_decimal(value) -> Decimal:
    """A JSON number (or a stored Decimal) as the exact Decimal it was written as."""
    if isinstance(value, Decimal):
        return value
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _pin_month_day(rule: str, moment: datetime, tz: str) -> str:
    """FREQ=MONTHLY with no BY* part → add BYMONTHDAY=<local day of the anchor>, so a
    series on the 31st keeps the month's last day instead of drifting to the 28th."""
    if not re.search(r"(^|;)FREQ=MONTHLY(;|$)", rule):
        return rule
    if re.search(r"(^|;)BY(MONTHDAY|DAY|SETPOS|YEARDAY|WEEKNO)=", rule):
        return rule
    return f"{rule};BYMONTHDAY={int(fu.day_key(moment, tz)[8:10])}"


async def _resolve(user_id: str, tz: str, default_currency: str, v: dict, previous_rule: str | None = None) -> dict:
    """Checks a full (merged) state and returns the row data: ownership of every
    linked id, the category's scope matches the type, a transfer has two different
    accounts; currency defaults to the account's, then the user's."""
    for field in ("categoryId", "accountId", "toAccountId", *_LINK_FIELDS):
        v[field] = v[field] or None  # "" (a cleared select) behaves like null
    for model, field in (("recurringpayment", "recurringPaymentId"), ("project", "projectId"), ("trip", "tripId"), ("goal", "goalId")):
        await assert_owned(model, v[field], user_id)
    account = None
    if v["accountId"]:
        account = await db.account.find_first(where={"id": v["accountId"], "userId": user_id})
        if account is None:
            raise nest_error(404, "Account not found")
    if v["type"] == "TRANSFER":
        if not v["accountId"] or not v["toAccountId"]:
            raise nest_error(400, "A transfer needs both a source account (accountId) and a destination account (toAccountId)")
        if v["accountId"] == v["toAccountId"]:
            raise nest_error(400, "Source and destination accounts must be different")
        await assert_owned("account", v["toAccountId"], user_id)
        v["categoryId"] = None
        v["source"] = None
    else:
        v["toAccountId"] = None
        v["toAmount"] = None
    if v["type"] == "INCOME":
        v["paymentMethod"] = None
    if v["categoryId"]:
        category = await db.category.find_first(where={"id": v["categoryId"], "userId": user_id})
        if category is None:
            raise nest_error(404, "Category not found")
        if category.scope != v["type"]:
            raise nest_error(
                400,
                f"Category must be an {'income' if v['type'] == 'INCOME' else 'expense'} category (got {category.scope.lower()})",
            )
    rule = rrule.normalize_rrule(v["rrule"]) if v["rrule"] else None
    if rule and rule != previous_rule:
        rule = _pin_month_day(rule, v["date"], tz)
    return {
        "type": v["type"],
        "amount": as_decimal(v["amount"]),
        "currency": v["currency"] or (account.currency if account else None) or default_currency,
        "date": v["date"],
        "title": v["title"] or None,
        "categoryId": v["categoryId"],
        "accountId": v["accountId"],
        "toAccountId": v["toAccountId"],
        "toAmount": None if v["toAmount"] is None else as_decimal(v["toAmount"]),
        "paymentMethod": v["paymentMethod"],
        "source": v["source"] or None,
        "notes": v["notes"] or None,
        "rrule": rule,
        "recurringPaymentId": v["recurringPaymentId"],
        "projectId": v["projectId"],
        "tripId": v["tripId"],
        "goalId": v["goalId"],
    }


async def category_tree_ids(user_id: str, category_id: str) -> list[str]:
    """A category and its sub-categories."""
    children = await db.category.find_many(where={"userId": user_id, "parentId": category_id})
    return [category_id, *(c.id for c in children)]


async def build_where(user_id: str, request: Request, tz: str) -> dict:
    """List filters (shared with the finance reports). Raises 400 on a bad value."""
    q = request.query_params
    errors: list[str] = []
    types = string_list(q.getlist("type"))
    if types and any(t not in TYPES for t in types):
        errors.append(f"each value in type must be one of the following values: {', '.join(TYPES)}")
    for name in ("from", "to"):
        if q.get(name) is not None and not dates.is_iso_date_string(q.get(name)):
            errors.append(f"{name} must be a valid ISO 8601 date string")
    text = q.get("q")
    if text is not None and not 1 <= len(text) <= 100:
        errors.append("q must be longer than or equal to 1 and shorter than or equal to 100 characters")
    amounts = {}
    for name in ("minAmount", "maxAmount"):
        raw = q.get(name)
        if raw is None:
            continue
        try:
            amounts[name] = float(raw)
        except ValueError:
            errors.append(f"{name} must be a number conforming to the specified constraints")
            continue
        if amounts[name] < 0:
            errors.append(f"{name} must not be less than 0")
    if q.get("sort") is not None and q.get("sort") not in SORTS:
        errors.append(f"sort must be one of the following values: {', '.join(SORTS)}")
    if errors:
        raise nest_error(400, errors)

    and_: list[dict] = [{"userId": user_id}]
    if types:
        and_.append({"type": {"in": types}})
    start, end = fu.parse_boundary(q.get("from"), tz, "start"), fu.parse_boundary(q.get("to"), tz, "end")
    if start or end:
        and_.append({"date": {**({"gte": start} if start else {}), **({"lte": end} if end else {})}})
    if q.get("categoryId"):
        if q.get("categoryId") == "none":
            and_.append({"categoryId": None})
        else:
            and_.append({"categoryId": {"in": await category_tree_ids(user_id, q.get("categoryId"))}})
    if q.get("accountId"):
        and_.append({"OR": [{"accountId": q.get("accountId")}, {"toAccountId": q.get("accountId")}]})
    for field in ("projectId", "tripId", "goalId", "recurringPaymentId"):
        if q.get(field):
            and_.append({field: q.get(field)})
    if amounts:
        and_.append(
            {
                "amount": {
                    **({"gte": as_decimal(amounts["minAmount"])} if "minAmount" in amounts else {}),
                    **({"lte": as_decimal(amounts["maxAmount"])} if "maxAmount" in amounts else {}),
                }
            }
        )
    if text and text.strip():
        contains = {"contains": text.strip(), "mode": "insensitive"}
        and_.append(
            {
                "OR": [
                    {"title": contains},
                    {"notes": contains},
                    {"source": contains},
                    {"category": {"is": {"name": contains}}},
                    {"account": {"is": {"name": contains}}},
                ]
            }
        )
    return {"AND": and_}


async def _get(user_id: str, id: str, fx: Converter, tz: str, lang: str) -> dict:
    row = await db.transaction.find_first(where={"id": id, "userId": user_id}, include=INCLUDE)
    if row is None:
        raise nest_error(404, "Transaction not found")
    return view(row, fx, tz, lang)


# ---------------------------------------------------------------------------
# Routes


@router.get("")
async def list_transactions(request: Request, user=Depends(plus_user)):
    errors: list[str] = []
    page = _query_int(request, "page", errors)
    page_size = _query_int(request, "pageSize", errors, MAX_PAGE_SIZE)
    if errors:
        raise nest_error(400, errors)
    await materialize_due(user.id)
    settings, tz, lang = await _settings(user)
    where = await build_where(user.id, request, tz)
    sort = request.query_params.get("sort")
    order = {
        "amount": [{"amount": "asc"}, {"date": "desc"}],
        "-amount": [{"amount": "desc"}, {"date": "desc"}],
        "date": [{"date": "asc"}, {"createdAt": "asc"}],
    }.get(sort, [{"date": "desc"}, {"createdAt": "desc"}])
    page, size = page or 1, page_size or DEFAULT_PAGE_SIZE
    skip = (page - 1) * size
    rows = await db.transaction.find_many(where=where, include=INCLUDE, order=order, skip=skip, take=size)
    total = await db.transaction.count(where=where)
    fx = await get_converter(user.id)
    income = expenses = transfers = 0
    # summed in the database (NestJS's groupBy), not over every matching row
    for g in await db.transaction.group_by(by=["type", "currency"], where=where, sum={"amount": True}):
        kind = g["type"]
        value = fx.to_default(fu.group_decimal(g["_sum"]["amount"]), g["currency"])
        if kind == "INCOME":
            income += value
        elif kind == "EXPENSE":
            expenses += value
        else:
            transfers += value
    return {
        "items": [view(r, fx, tz, lang) for r in rows],
        "total": total,
        "page": page,
        "pageSize": size,
        "hasMore": skip + len(rows) < total,
        "totals": {
            "currency": fx.default_currency,
            "income": round_money(income),
            "expenses": round_money(expenses),
            "transfers": round_money(transfers),
            "net": round_money(income - expenses),
        },
    }


@router.post("/import/preview")
async def import_preview(body: ImportBody, user=Depends(plus_user)):
    """For each incoming row: is it stored already, and which category did the user
    give this text before?"""
    settings, tz, _ = await _settings(user)
    await assert_owned("account", body.accountId, user.id)
    days = sorted(r.date for r in body.rows)
    start, end = fu.parse_boundary(days[0], tz, "start"), fu.parse_boundary(days[-1], tz, "end")
    existing = await db.transaction.find_many(
        where={
            "userId": user.id,
            "type": {"in": ["INCOME", "EXPENSE"]},
            "date": {"gte": start, "lte": end},
            **({"accountId": body.accountId} if body.accountId else {}),
        },
        take=20000,
    )
    history = await db.transaction.find_many(
        where={"userId": user.id, "type": {"in": ["INCOME", "EXPENSE"]}, "categoryId": {"not": None}, "title": {"not": None}},
        order={"date": "desc"},
        take=3000,
    )
    duplicates = mark_duplicates(
        [(fu.day_key(e.date, tz), e.type, float(e.amount), e.title) for e in existing],
        [(r.date, r.type, float(r.amount), r.title) for r in body.rows],
    )
    categories = suggest_categories(history, [(r.title, r.type) for r in body.rows])
    return {
        "rows": [{"duplicate": d, "categoryId": c} for d, c in zip(duplicates, categories)],
        "duplicates": sum(duplicates),
        "categorized": sum(1 for c in categories if c),
    }


@router.post("/import", status_code=201)
async def import_rows(body: ImportBody, user=Depends(plus_user)):
    """Creates the rows as sent (the dashboard already dropped unwanted duplicates)."""
    settings, tz, _ = await _settings(user)
    account = None
    if body.accountId:
        account = await db.account.find_first(where={"id": body.accountId, "userId": user.id})
        if account is None:
            raise nest_error(404, "Account not found")
    currency = body.currency or (account.currency if account else None) or settings.defaultCurrency
    # A category is kept only when it's the user's and matches the row's direction.
    wanted = list(dict.fromkeys(r.categoryId for r in body.rows if r.categoryId))
    owned = (
        await db.category.find_many(where={"userId": user.id, "id": {"in": wanted}, "scope": {"in": ["INCOME", "EXPENSE"]}})
        if wanted
        else []
    )
    scope_of = {c.id: c.scope for c in owned}
    count = await db.transaction.create_many(
        data=[
            {
                "userId": user.id,
                "type": r.type,
                "amount": as_decimal(r.amount),
                "currency": currency,
                "date": fu.parse_instant(r.date, tz) or datetime.now(timezone.utc),
                "title": (r.title or "").strip() or None,
                "accountId": account.id if account else None,
                "categoryId": r.categoryId if r.categoryId and scope_of.get(r.categoryId) == r.type else None,
            }
            for r in body.rows
        ]
    )
    await log_activity(
        user.id, "transaction.imported", entity_id=None, count=count, accountId=account.id if account else None, currency=currency
    )
    return {"imported": count, "currency": currency}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    settings, tz, lang = await _settings(user)
    moment = fu.parse_instant(body.date, tz) or datetime.now(timezone.utc)
    data = await _resolve(
        user.id,
        tz,
        settings.defaultCurrency,
        {**{f: getattr(body, f) for f in _TxFields.model_fields}, "date": moment},
    )
    row = await db.transaction.create(data={"userId": user.id, **data})
    await log_activity(
        user.id, "transaction.created", entity_id=row.id, type=row.type, amount=to_number(row.amount),
        currency=row.currency, categoryId=row.categoryId, title=row.title,
    )
    if row.rrule:  # a back-dated repeating entry may already have due occurrences
        await materialize_due(user.id)
    return await _get(user.id, row.id, await get_converter(user.id), tz, lang)


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    _, tz, lang = await _settings(user)
    return await _get(user.id, id, await get_converter(user.id), tz, lang)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    existing = await db.transaction.find_first(where={"id": id, "userId": user.id})
    if existing is None:
        raise nest_error(404, "Transaction not found")
    settings, tz, lang = await _settings(user)
    sent = body.model_fields_set

    def pick(field, fallback):
        return getattr(body, field) if field in sent else fallback

    merged = {
        "type": pick("type", existing.type) or existing.type,
        "amount": body.amount if body.amount is not None else float(existing.amount),
        "currency": pick("currency", existing.currency) or existing.currency,
        "date": (fu.parse_instant(body.date, tz) or existing.date) if body.date else existing.date,
        "title": pick("title", existing.title),
        "categoryId": pick("categoryId", existing.categoryId),
        "accountId": pick("accountId", existing.accountId),
        "toAccountId": pick("toAccountId", existing.toAccountId),
        "toAmount": pick("toAmount", None if existing.toAmount is None else float(existing.toAmount)),
        "paymentMethod": pick("paymentMethod", existing.paymentMethod),
        "source": pick("source", existing.source),
        "notes": pick("notes", existing.notes),
        "rrule": pick("rrule", existing.rrule),
        "recurringPaymentId": pick("recurringPaymentId", existing.recurringPaymentId),
        "projectId": pick("projectId", existing.projectId),
        "tripId": pick("tripId", existing.tripId),
        "goalId": pick("goalId", existing.goalId),
    }
    data = await _resolve(user.id, tz, settings.defaultCurrency, merged, existing.rrule)
    await db.transaction.update(where={"id": existing.id}, data=data)
    await log_activity(user.id, "transaction.updated", entity_id=id, type=data["type"], amount=data["amount"])
    if data["rrule"]:
        await materialize_due(user.id)
    return await _get(user.id, id, await get_converter(user.id), tz, lang)


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    existing = await db.transaction.find_first(where={"id": id, "userId": user.id})
    if existing is None:
        raise nest_error(404, "Transaction not found")
    await db.transaction.delete(where={"id": existing.id})
    await log_activity(
        user.id, "transaction.deleted", entity_id=id, type=existing.type, amount=to_number(existing.amount), title=existing.title
    )
    # NestJS's "transaction.deleted" listener: its debt payment went with it (cascade)
    from app.os.debts import on_transaction_deleted

    await on_transaction_deleted(user.id)
    return {"ok": True}
