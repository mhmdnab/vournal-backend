"""Multi-currency conversion (port of vournalOS CurrencyService).

Rates are units of a currency per 1 USD. A user's effective rates: the built-in
defaults, overlaid by the live market rates, overlaid by the rates they typed
(manual). Results are JavaScript numbers (64-bit floats) rounded to 2 decimals
the way NestJS rounded them, so totals match to the cent.
"""

import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from app.db import db
from app.os import market_rates
from app.os.common.defaults import DEFAULT_CURRENCY, DEFAULT_RATES_PER_USD

log = logging.getLogger("vournal.os")
_DEFAULTS = {code: float(rate) for code, rate in DEFAULT_RATES_PER_USD.items()}


def to_number(amount) -> float:
    """number | str | Decimal | None → float (None or invalid → 0), like Number()."""
    if amount is None:
        return 0
    try:
        value = float(amount) if not isinstance(amount, str) else float(amount.strip() or 0)
    except (TypeError, ValueError):
        return 0
    return value if math.isfinite(value) else 0


def _js_round(value: float) -> float:
    return math.floor(value + 0.5)


def round_money(value: float, decimals: int = 2) -> float:
    """Half away from zero; toPrecision(15) first strips binary noise
    (1.005 * 100 = 100.49999999999999 → 100.5)."""
    factor = 10**decimals
    rounded = _js_round(float(f"{abs(value) * factor:.15g}")) / factor
    return -rounded if value < 0 else rounded


async def get_rates(user_id: str) -> dict[str, float]:
    rows = await db.exchangerate.find_many(where={"userId": user_id, "manual": True})
    live = await market_rates.current()
    rates = {**_DEFAULTS, **{code: float(rate) for code, rate in live.rates.items()}}
    for row in rows:
        value = to_number(row.ratePerUsd)
        if value > 0:
            rates[row.currency] = value
    rates["USD"] = 1
    return rates


def convert_with_rates(amount, source: str, target: str, rates: dict[str, float]) -> float:
    """Unrounded; an unknown currency is left unconverted (and logged)."""
    value = to_number(amount)
    if source == target or value == 0:
        return value
    rate_from = rates.get(source) or _DEFAULTS.get(source)
    rate_to = rates.get(target) or _DEFAULTS.get(target)
    if not rate_from or not rate_to:
        log.warning("Missing exchange rate for %s; returning unconverted amount", target if rate_from else source)
        return value
    return (value / rate_from) * rate_to


@dataclass
class Converter:
    """Bound to one user's rates: load once, convert many amounts."""

    default_currency: str
    rates: dict[str, float]

    def convert(self, amount, source: str, target: str) -> float:
        return round_money(convert_with_rates(amount, source, target, self.rates))

    def to_default(self, amount, currency: str | None) -> float:
        return round_money(convert_with_rates(amount, currency or self.default_currency, self.default_currency, self.rates))


async def get_converter(user_id: str) -> Converter:
    rates = await get_rates(user_id)
    settings = await db.usersettings.find_unique(where={"userId": user_id})
    return Converter(settings.defaultCurrency if settings else DEFAULT_CURRENCY, rates)


async def convert(amount, source: str, target: str, user_id: str) -> float:
    if source == target:
        return round_money(to_number(amount))
    return round_money(convert_with_rates(amount, source, target, await get_rates(user_id)))


async def to_default(amount, currency: str | None, user_id: str) -> float:
    return (await get_converter(user_id)).to_default(amount, currency)


def as_decimal(value: float) -> Decimal:
    """A rounded float amount back to an exact 2-decimal Decimal for storing."""
    return Decimal(repr(round_money(value)))
