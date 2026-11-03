"""Helpers shared by the money sections (port of vournalOS finance.utils)."""

import math
from datetime import date, datetime, time, timedelta

from app.os.common import dates
from app.os.common.currency import round_money

safe_tz = dates.valid_zone

CATEGORY_MINI = ("id", "name", "color", "icon", "parentId", "scope")
ACCOUNT_MINI = ("id", "name", "type", "currency", "color", "icon", "isArchived")


def mini(row, fields) -> dict | None:
    return None if row is None else {f: getattr(row, f) for f in fields}


def _at(day: date, clock: time, tz: str) -> datetime:
    return dates._at(day, clock, safe_tz(tz))


def parse_instant(value, tz: str) -> datetime | None:
    """For an instant column (Transaction.date): "YYYY-MM-DD" → 12:00 local, so it
    stays on its calendar day in every report; a full ISO value → that instant."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return dates._utc(value)
    if dates.DATE_ONLY_RE.match(value):
        return _at(date.fromisoformat(value), time(12), tz)
    return dates.parse_instant(value)


def parse_boundary(value, tz: str, edge: str) -> datetime | None:
    """List filter bound: "YYYY-MM-DD" → 00:00 / 23:59:59.999 of that local day."""
    if not value:
        return None
    if dates.DATE_ONLY_RE.match(value):
        return _at(date.fromisoformat(value), time(0) if edge == "start" else time(23, 59, 59, 999000), tz)
    return dates.parse_instant(value)


def day_key(moment, tz: str) -> str:
    """Instant → "YYYY-MM-DD" in `tz`."""
    if isinstance(moment, str):
        moment = dates.parse_instant(moment)
    return dates.local_date_string(moment, safe_tz(tz))


def date_only_to_noon(value: datetime, tz: str) -> datetime:
    """Noon (local) of a date-only value: a bill's due date becoming a transaction date."""
    return _at(dates._utc(value).date(), time(12), tz)


round2 = round_money


def _js_round(value: float) -> float:
    return math.floor(value + 0.5)


def pct_change(cur: float, prev: float) -> float | None:
    """Change from prev to cur in percent, 1 decimal; None when prev is 0."""
    if not prev:
        return None
    return _js_round(((cur - prev) / abs(prev)) * 1000) / 10


def pct(a: float, b: float) -> float:
    """a / b in percent with 1 decimal; 0 when b is 0."""
    if not b:
        return 0
    return _js_round((a / b) * 1000) / 10


def month_key(day: str) -> str:
    return day[:7]


def enumerate_days(start: str, end: str, limit: int = 1000) -> list[str]:
    out, cur, last = [], date.fromisoformat(start), date.fromisoformat(end)
    while cur <= last and len(out) < limit:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def last_months(end_month: str, n: int) -> list[str]:
    """The last `n` month keys ending with `end_month`, oldest first."""
    year, month = (int(p) for p in end_month.split("-"))
    out = []
    for i in range(n - 1, -1, -1):
        total = year * 12 + month - 1 - i
        out.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return out


def local_start(y: int, m: int, d: int, tz: str) -> datetime:
    return _at(date(y, m, d), time(0), tz)


def local_end(y: int, m: int, d: int, tz: str) -> datetime:
    return _at(date(y, m, d), time(23, 59, 59, 999000), tz)


def days_in_month(y: int, m: int) -> int:
    first_next = date(y + m // 12, m % 12 + 1, 1)
    return (first_next - timedelta(days=1)).day

