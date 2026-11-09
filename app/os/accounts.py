"""/api/accounts (port of vournalOS AccountsService).

balance = initialBalance + INCOME − EXPENSE − transfers out + transfers in (toAmount
when given, which is already in the target account's currency), every amount
converted from its transaction's currency to the account's.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from app.db import db
from app.os import finance_utils as fu
from app.os.common import validators
from app.os.common.jsparse import js_sum
from app.os.common.activity import log_activity
from app.os.common.currency import Converter, get_converter, round_money, to_number
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.ownership import assert_many_owned
from app.os.common.query import _query_bool, string_list, unknown_params
from app.os.profile import ensure_os_profile
from app.os.transactions import as_decimal, materialize_due

router = nest_router("/accounts")

TYPES = ("CASH", "BANK", "SAVINGS", "CREDIT_CARD", "DEBIT_CARD", "DIGITAL_WALLET", "INVESTMENT", "OTHER")
TYPE_ORDER = ("CASH", "BANK", "SAVINGS", "DEBIT_CARD", "CREDIT_CARD", "DIGITAL_WALLET", "INVESTMENT", "OTHER")
NOT_SPENT = ("SAVINGS", "INVESTMENT")  # new accounts of these types start with spendFrom off
AccountTypeName = Literal[TYPES]  # type: ignore[valid-type]
CurrencyCode = Literal[tuple(SUPPORTED_CURRENCIES)]  # type: ignore[valid-type]
_FIELDS = (
    "id", "name", "type", "currency", "initialBalance", "color", "icon", "isArchived", "includeInTotal",
    "spendFrom", "sortOrder", "createdAt", "updatedAt",
)
GROUP_LABELS = {
    "en": {
        "CASH": "Cash", "BANK": "Bank accounts", "SAVINGS": "Savings", "CREDIT_CARD": "Credit cards",
        "DEBIT_CARD": "Debit cards", "DIGITAL_WALLET": "Digital wallets", "INVESTMENT": "Investments", "OTHER": "Other",
    },
    "ar": {
        "CASH": "نقدي", "BANK": "الحسابات المصرفية", "SAVINGS": "المدّخرات", "CREDIT_CARD": "بطاقات الائتمان",
        "DEBIT_CARD": "بطاقات الخصم", "DIGITAL_WALLET": "المحافظ الرقمية", "INVESTMENT": "الاستثمارات", "OTHER": "أخرى",
    },
}


async def with_balances(user_id: str, rows, fx: Converter, until: datetime | None = None) -> list[dict]:
    """Each account with its balance; with `until`, only transactions dated up to then count."""
    if not rows:
        return []
    ids = [r.id for r in rows]
    by_id = {r.id: r for r in rows}
    # summed in the database, as NestJS's groupBy did: never every row on the shared server
    group = db.transaction.group_by
    mine = {"userId": user_id, **({"date": {"lte": until}} if until else {})}
    out_sums = await group(by=["accountId", "type", "currency"], where={**mine, "accountId": {"in": ids}}, sum={"amount": True})
    in_with_to = await group(
        by=["toAccountId"], where={**mine, "type": "TRANSFER", "toAccountId": {"in": ids}, "toAmount": {"not": None}}, sum={"toAmount": True}
    )
    in_without_to = await group(
        by=["toAccountId", "currency"], where={**mine, "type": "TRANSFER", "toAccountId": {"in": ids}, "toAmount": None}, sum={"amount": True}
    )
    stats = [
        *await group(by=["accountId"], where={**mine, "accountId": {"in": ids}}, count={"_all": True}, max={"date": True}),
        *await group(by=["toAccountId"], where={**mine, "toAccountId": {"in": ids}}, count={"_all": True}, max={"date": True}),
    ]

    delta: dict[str, float] = {}
    for g in out_sums:
        value = fx.convert(fu.group_decimal(g["_sum"]["amount"]), g["currency"], by_id[g["accountId"]].currency)
        delta[g["accountId"]] = delta.get(g["accountId"], 0) + (value if g["type"] == "INCOME" else -value)
    for g in in_with_to:
        delta[g["toAccountId"]] = delta.get(g["toAccountId"], 0) + to_number(fu.group_decimal(g["_sum"]["toAmount"]))
    for g in in_without_to:
        account_id = g["toAccountId"]
        delta[account_id] = delta.get(account_id, 0) + fx.convert(fu.group_decimal(g["_sum"]["amount"]), g["currency"], by_id[account_id].currency)

    count: dict[str, int] = {}
    last: dict[str, datetime] = {}
    for g in stats:
        account_id = g.get("accountId") or g.get("toAccountId")
        count[account_id] = count.get(account_id, 0) + g["_count"]["_all"]
        latest = fu.group_instant(g["_max"]["date"])
        if latest and (account_id not in last or latest > last[account_id]):
            last[account_id] = latest

    out = []
    for r in rows:
        balance = round_money(to_number(r.initialBalance) + delta.get(r.id, 0))
        view = {f: getattr(r, f) for f in _FIELDS}
        view.update(
            balance=balance,
            balanceInDefault=fx.convert(balance, r.currency, fx.default_currency),
            defaultCurrency=fx.default_currency,
            transactionCount=count.get(r.id, 0),
            lastTransactionAt=last.get(r.id),
        )
        out.append(view)
    return out


async def net_worth(user_id: str, fx: Converter) -> float:
    """Net worth in the default currency (used by /api/finance/summary)."""
    rows = await db.account.find_many(where={"userId": user_id, "isArchived": False, "includeInTotal": True})
    return round_money(js_sum(a["balanceInDefault"] for a in await with_balances(user_id, rows, fx)))


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    type: AccountTypeName | None = None
    currency: CurrencyCode | None = None
    initialBalance: object = None
    color: StrictStr | None = None
    icon: StrictStr | None = None
    isArchived: StrictBool | None = None
    includeInTotal: StrictBool | None = None
    spendFrom: StrictBool | None = None
    sortOrder: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return validators.trimmed(v, "name", 1, 60)

    @field_validator("initialBalance")
    @classmethod
    def _balance(cls, v):
        return validators.money(v, "initialBalance")

    @field_validator("color")
    @classmethod
    def _color(cls, v):
        return validators.color(v)

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        return validators.icon(v)


class CreateBody(_Fields):
    name: StrictStr

    @field_validator("name")
    @classmethod
    def _required_name(cls, v):
        return validators.trimmed(v, "name", 1, 60)


class UpdateBody(_Fields):
    pass


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1)
    sortOrder: StrictInt = Field(ge=0)


class ReorderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[OrderItem] = Field(min_length=1, max_length=200)


async def _owned(user_id: str, id: str):
    row = await db.account.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Account not found")
    return row


async def _get(user_id: str, id: str) -> dict:
    [view] = await with_balances(user_id, [await _owned(user_id, id)], await get_converter(user_id))
    return view


_LIST_ACCOUNTS_QUERY = ("includeArchived", "type")  # the NestJS query DTO's fields


@router.get("")
async def list_accounts(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, _LIST_ACCOUNTS_QUERY)
    include_archived = _query_bool(request, "includeArchived", errors)
    types = string_list(request.query_params.getlist("type"))
    if types and any(t not in TYPES for t in types):
        errors.append(f"each value in type must be one of the following values: {', '.join(TYPES)}")
    if errors:
        raise nest_error(400, errors)
    await materialize_due(user.id)
    fx = await get_converter(user.id)
    where = {"userId": user.id, **({} if include_archived else {"isArchived": False}), **({"type": {"in": types}} if types else {})}
    rows = await db.account.find_many(where=where, order=[{"isArchived": "asc"}, {"sortOrder": "asc"}, {"createdAt": "asc"}])
    items = await with_balances(user.id, rows, fx)
    return {"items": items, "total": len(items)}


@router.get("/summary")
async def summary(user=Depends(plus_user)):
    """Balances plus totals in the default currency. The total (net worth) counts
    accounts that are active and included in totals."""
    await materialize_due(user.id)
    fx = await get_converter(user.id)
    labels = GROUP_LABELS[to_lang((await ensure_os_profile(user)).language)]
    rows = await db.account.find_many(where={"userId": user.id}, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
    every = await with_balances(user.id, rows, fx)
    active = [a for a in every if not a["isArchived"]]
    counted = [a for a in active if a["includeInTotal"]]
    by_type = []
    for kind in TYPE_ORDER:
        group = [a for a in active if a["type"] == kind]
        if group:
            by_type.append(
                {
                    "type": kind,
                    "label": labels[kind],
                    "count": len(group),
                    "total": round_money(js_sum(a["balanceInDefault"] for a in group if a["includeInTotal"])),
                    "accountIds": [a["id"] for a in group],
                }
            )
    return {
        "currency": fx.default_currency,
        "total": round_money(js_sum(a["balanceInDefault"] for a in counted)),
        "assets": round_money(js_sum(a["balanceInDefault"] for a in counted if a["balanceInDefault"] > 0)),
        "liabilities": round_money(js_sum(a["balanceInDefault"] for a in counted if a["balanceInDefault"] < 0)),
        "accountCount": len(active),
        "archivedCount": len(every) - len(active),
        "accounts": active,
        "byType": by_type,
    }


@router.post("/reorder")
async def reorder(body: ReorderBody, user=Depends(plus_user)):
    await assert_many_owned("account", [i.id for i in body.items], user.id)
    async with db.tx() as tx:
        for item in body.items:
            await tx.account.update_many(where={"id": item.id, "userId": user.id}, data={"sortOrder": item.sortOrder})
    return {"ok": True}


@router.post("", status_code=201)
async def create(body: CreateBody, user=Depends(plus_user)):
    settings = await ensure_os_profile(user)
    sort_order = body.sortOrder
    if sort_order is None:
        last = await db.account.find_first(where={"userId": user.id}, order={"sortOrder": "desc"})
        sort_order = (last.sortOrder if last else -1) + 1
    row = await db.account.create(
        data={
            "userId": user.id,
            "name": body.name,
            "type": body.type or "BANK",
            "currency": body.currency or settings.defaultCurrency,
            "initialBalance": as_decimal(body.initialBalance if body.initialBalance is not None else 0),
            "color": body.color,
            "icon": body.icon,
            "isArchived": bool(body.isArchived),
            "includeInTotal": True if body.includeInTotal is None else body.includeInTotal,
            # the spending plan doesn't spend savings or investments unless told to
            "spendFrom": (body.type or "BANK") not in NOT_SPENT if body.spendFrom is None else body.spendFrom,
            "sortOrder": sort_order,
        }
    )
    await log_activity(user.id, "account.created", entity_id=row.id, name=row.name, type=row.type)
    return await _get(user.id, row.id)


@router.get("/{id}")
async def get(id: str, user=Depends(plus_user)):
    return await _get(user.id, id)


async def _update(user, id: str, body: UpdateBody) -> dict:
    existing = await _owned(user.id, id)
    sent = body.model_fields_set
    # `?? undefined` in NestJS: a null leaves these unchanged; color and icon can be cleared.
    data = {f: getattr(body, f) for f in ("name", "type", "currency", "isArchived", "includeInTotal", "spendFrom", "sortOrder") if getattr(body, f) is not None}
    if body.initialBalance is not None:
        data["initialBalance"] = as_decimal(body.initialBalance)
    for field in ("color", "icon"):
        if field in sent:
            data[field] = getattr(body, field)
    if data:
        await db.account.update(where={"id": existing.id}, data=data)
    if body.isArchived is True and not existing.isArchived:
        verb = "archived"
    elif body.isArchived is False and existing.isArchived:
        verb = "unarchived"
    else:
        verb = "updated"
    await log_activity(user.id, f"account.{verb}", entity_id=id, name=body.name or existing.name)
    return await _get(user.id, id)


@router.patch("/{id}")
async def update(id: str, body: UpdateBody, user=Depends(plus_user)):
    return await _update(user, id, body)


@router.post("/{id}/archive")
async def archive(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(isArchived=True))


@router.post("/{id}/unarchive")
async def unarchive(id: str, user=Depends(plus_user)):
    return await _update(user, id, UpdateBody(isArchived=False))


@router.delete("/{id}")
async def remove(id: str, user=Depends(plus_user)):
    """An account with transactions can't be deleted (409): archive it instead."""
    existing = await _owned(user.id, id)
    count = await db.transaction.count(where={"userId": user.id, "OR": [{"accountId": id}, {"toAccountId": id}]})
    if count:
        raise nest_error(
            409,
            f'"{existing.name}" has {count} transaction{"" if count == 1 else "s"} and can\'t be deleted. '
            "Archive it instead to hide it and keep your history.",
        )
    await db.account.delete(where={"id": existing.id})
    await log_activity(user.id, "account.deleted", entity_id=id, name=existing.name)
    return {"ok": True}
