"""Live market exchange rates shared by every user (port of vournalOS MarketRatesService).

Kept in memory and in the MarketRate table, so a restart or a provider outage
never loses them. Refreshed lazily once older than 12 hours (the 6-hourly job is
ported with the scheduler in a later phase) and on request, at most once every 10
minutes for the whole server. Never raises: without data, callers fall back to
the built-in defaults.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.db import db
from app.os.common.defaults import DEFAULT_RATES_PER_USD, SUPPORTED_CURRENCIES

log = logging.getLogger("vournal.os")

STALE_SECONDS = 12 * 3600
MIN_REFRESH_GAP_SECONDS = 10 * 60
FETCH_TIMEOUT_SECONDS = 8
# A rate moving more than this factor against the last known value is a provider glitch.
MAX_JUMP = 3
MIN_USABLE = 5


def _read_er_api(payload):
    if isinstance(payload, dict) and payload.get("result") == "success" and isinstance(payload.get("rates"), dict):
        return payload["rates"]
    return None


def _read_currency_api(payload):
    usd = payload.get("usd") if isinstance(payload, dict) else None
    return {code.upper(): rate for code, rate in usd.items()} if isinstance(usd, dict) else None


# Public, key-less sources (both update about once a day). The first that answers wins.
PROVIDERS = [
    ("Exchange Rate API", "https://open.er-api.com/v6/latest/USD", _read_er_api),
    (
        "Currency API",
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.min.json",
        _read_currency_api,
    ),
]


@dataclass
class Snapshot:
    rates: dict[str, Decimal] = field(default_factory=dict)  # units per 1 USD
    source: str | None = None
    fetched_at: datetime | None = None


_snapshot = Snapshot()
_loaded = False
_refreshing: asyncio.Task | None = None
_last_attempt = 0.0


def reset_for_tests() -> None:
    global _snapshot, _loaded, _refreshing, _last_attempt
    _snapshot, _loaded, _refreshing, _last_attempt = Snapshot(), False, None, 0.0


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def sanitize_rates(raw: dict, previous: dict) -> dict[str, float]:
    """The supported currencies' rates, minus anything implausible: not a positive
    number, or more than ×3 / ÷3 against the previous value (the last stored rate,
    else the built-in default)."""
    out: dict[str, float] = {}
    for code in SUPPORTED_CURRENCIES:
        if code == "USD":
            continue
        value = _number(raw.get(code))
        if value is None or value <= 0:
            continue
        before = previous.get(code) or DEFAULT_RATES_PER_USD.get(code)
        if before:
            before = float(before)
            if value / before > MAX_JUMP or before / value > MAX_JUMP:
                continue
        out[code] = value
    return out


async def _fetch_json(url: str):
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers={"accept": "application/json"})
        response.raise_for_status()
        return response.json()


async def _load() -> None:
    global _snapshot, _loaded
    try:
        rows = await db.marketrate.find_many()
        if rows:
            newest = max(rows, key=lambda r: r.fetchedAt)
            _snapshot = Snapshot(
                rates={r.currency: r.ratePerUsd for r in rows},
                source=newest.source,
                fetched_at=newest.fetchedAt,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load stored market rates: %s", exc)
    _loaded = True


async def current() -> Snapshot:
    """The latest known rates; schedules a background refresh when they're stale."""
    if not _loaded:
        await _load()
    fetched = _snapshot.fetched_at
    if fetched is not None and fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - fetched).total_seconds() if fetched else float("inf")
    if age > STALE_SECONDS:
        asyncio.ensure_future(refresh())
    return _snapshot


async def refresh(force: bool = False) -> bool:
    """Fetch fresh rates; True when new ones were stored. One fetch at a time, and at
    most one every 10 minutes unless forced."""
    global _refreshing, _last_attempt
    if _refreshing is not None and not _refreshing.done():
        return await asyncio.shield(_refreshing)
    if not force and time.monotonic() - _last_attempt < MIN_REFRESH_GAP_SECONDS and _last_attempt:
        return False
    _last_attempt = time.monotonic()
    _refreshing = asyncio.ensure_future(_do_refresh())
    return await asyncio.shield(_refreshing)


async def wait_for_refresh() -> None:
    """For tests: let a scheduled background refresh finish."""
    await asyncio.sleep(0)
    if _refreshing is not None:
        await _refreshing


def retry_after_seconds() -> int:
    """Seconds until a manual refresh is allowed again (0 = now)."""
    if not _last_attempt:
        return 0
    remaining = MIN_REFRESH_GAP_SECONDS - (time.monotonic() - _last_attempt)
    return max(0, int(-(-remaining // 1)))


async def _do_refresh() -> bool:
    global _snapshot
    if not _loaded:
        await _load()
    for name, url, read in PROVIDERS:
        try:
            raw = read(await _fetch_json(url))
            if not raw:
                raise ValueError("unexpected response")
            rates = sanitize_rates(raw, _snapshot.rates)
            if len(rates) < MIN_USABLE:
                raise ValueError("too few usable rates")
            fetched_at = datetime.now(timezone.utc)
            async with db.tx() as tx:
                for currency, rate in rates.items():
                    value = Decimal(str(rate))
                    await tx.marketrate.upsert(
                        where={"currency": currency},
                        data={
                            "create": {"currency": currency, "ratePerUsd": value, "source": name, "fetchedAt": fetched_at},
                            "update": {"ratePerUsd": value, "source": name, "fetchedAt": fetched_at},
                        },
                    )
            _snapshot = Snapshot(
                rates={**_snapshot.rates, **{c: Decimal(str(r)) for c, r in rates.items()}},
                source=name,
                fetched_at=fetched_at,
            )
            log.info("Market rates updated from %s (%d currencies)", name, len(rates))
            return True
        except Exception as exc:  # noqa: BLE001 — any failure: try the next provider
            log.warning('Rates provider "%s" failed: %s', name, exc)
    return False
