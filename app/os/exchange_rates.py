"""/api/exchange-rates (port of vournalOS ExchangeRatesService).

Each currency's rate in effect for the user: the one they typed (manual) →
else the live market rate → else the built-in default. Units per 1 USD.
"""

from decimal import Decimal
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from app.db import db
from app.os import market_rates
from app.os.common.defaults import CURRENCIES, DEFAULT_RATES_PER_USD, SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router

router = nest_router("/exchange-rates")

BASE_CURRENCY = "USD"


async def rates_view(user_id: str) -> dict:
    rows = {r.currency: r for r in await db.exchangerate.find_many(where={"userId": user_id})}
    live = await market_rates.current()
    items = []
    for c in CURRENCIES:
        code = c["code"]
        row = rows.get(code)
        is_base = code == BASE_CURRENCY
        default = Decimal(DEFAULT_RATES_PER_USD.get(code, "1"))
        live_rate = Decimal(1) if is_base else live.rates.get(code)
        manual = not is_base and bool(row and row.manual)
        if is_base:
            rate = Decimal(1)
        elif manual:
            rate = row.ratePerUsd
        else:
            rate = live_rate if live_rate is not None else default
        items.append(
            {
                "currency": code,
                "name": c["name"],
                "symbol": c["symbol"],
                "ratePerUsd": rate,
                "liveRatePerUsd": live_rate,
                "defaultRatePerUsd": default,
                "manual": manual,
                "isCustom": manual,  # deprecated alias of manual, kept for older clients
                "updatedAt": (row.updatedAt if row else None) if manual else live.fetched_at,
            }
        )
    return {
        "base": BASE_CURRENCY,
        "items": items,
        "total": len(items),
        "live": {"source": live.source, "fetchedAt": live.fetched_at, "available": bool(live.rates)},
    }


class RatesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rates: Any = None


class UseLiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currencies: Any = None


def _valid_rates(value) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        raise nest_error(400, ['rates must be an object like { "EUR": 0.92 }'])
    out, errors = {}, []
    for code, raw in value.items():
        number = market_rates._number(raw) if isinstance(raw, (int, float, str)) else None
        if isinstance(raw, str) and raw.strip() == "":
            number = 0.0  # JS Number("") is 0
        if code not in SUPPORTED_CURRENCIES:
            errors.append(f"{code} is not a supported currency")
        elif number is None or number <= 0 or number > 1e12:
            errors.append(f"rate for {code} must be a positive number")
        else:
            out[code] = Decimal(str(number))
    if errors:
        raise nest_error(400, errors)
    return out


@router.get("")
async def list_rates(user=Depends(plus_user)):
    return await rates_view(user.id)


@router.put("")
async def update_rates(body: RatesBody, user=Depends(plus_user)):
    """Rates the user types become manual overrides (USD is always 1)."""
    rates = {code: rate for code, rate in _valid_rates(body.rates).items() if code != BASE_CURRENCY}
    if rates:
        async with db.batch_() as batch:  # one atomic request
            for currency, rate in rates.items():
                batch.exchangerate.upsert(
                    where={"userId_currency": {"userId": user.id, "currency": currency}},
                    data={
                        "create": {"userId": user.id, "currency": currency, "ratePerUsd": rate, "manual": True},
                        "update": {"ratePerUsd": rate, "manual": True},
                    },
                )
    return await rates_view(user.id)


@router.post("/use-live")
async def use_live(body: UseLiveBody, user=Depends(plus_user)):
    """Back to the live rate for the given currencies (all of them when none are given)."""
    currencies = body.currencies
    if currencies is not None:
        if not isinstance(currencies, list):
            raise nest_error(400, ["currencies must be an array"])
        if len(currencies) > 50:
            raise nest_error(400, ["currencies must contain no more than 50 elements"])
        if any(c not in SUPPORTED_CURRENCIES for c in currencies):
            raise nest_error(400, ["each value in currencies must be one of the following values"])
    where = {"userId": user.id, "manual": True}
    if currencies:
        where["currency"] = {"in": currencies}
    await db.exchangerate.update_many(where=where, data={"manual": False})
    return await rates_view(user.id)


@router.post("/refresh")
async def refresh(user=Depends(plus_user)):
    """Ask the providers for fresh rates now (at most every 10 minutes server-wide)."""
    updated = await market_rates.refresh()
    return {**await rates_view(user.id), "refreshed": updated, "retryAfterSeconds": market_rates.retry_after_seconds()}
