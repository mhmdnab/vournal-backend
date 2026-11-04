"""Day keys for the overview sections (port of vournalOS insights/lib/time.ts): "YYYY-MM-DD"
strings, the user's local day for instants and the stored UTC day for date-only columns."""

from datetime import date, datetime, timedelta, timezone

from app.os.common import dates
from app.os.insight_text import js_round


def day_key(instant: datetime, tz: str) -> str:
    """The local calendar day of an instant in `tz` (UTC when the zone is unknown)."""
    return dates.local_date_string(instant, dates.valid_zone(tz))


def date_only_key(value: datetime) -> str:
    return dates._utc(value).date().isoformat()


def key_to_date(key: str) -> datetime:
    return datetime(int(key[:4]), int(key[5:7]), int(key[8:10]), tzinfo=timezone.utc)


def add_days_key(key: str, days: int) -> str:
    return (date.fromisoformat(key) + timedelta(days=days)).isoformat()


def diff_days_key(a: str, b: str) -> int:
    """b − a in whole days."""
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def each_day_key(start: str, end: str) -> list[str]:
    out, k = [], start
    while k <= end and len(out) < 800:
        out.append(k)
        k = add_days_key(k, 1)
    return out


def weekday_of_key(key: str) -> int:
    """0 = Sunday … 6 = Saturday."""
    return (date.fromisoformat(key).weekday() + 1) % 7


def week_start_key(key: str, week_starts_on: int) -> str:
    return add_days_key(key, -((weekday_of_key(key) - week_starts_on + 7) % 7))


def days_in_month(year: int, month1: int) -> int:
    nxt = date(year + month1 // 12, month1 % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def pct_change(previous: float, current: float) -> float | None:
    """Percentage change previous → current to one decimal (None when previous is 0)."""
    if not previous:
        return None
    return js_round(((current - previous) / abs(previous)) * 1000) / 10


def round2(n: float) -> float:
    return js_round(n * 100) / 100


def pct(done: float, total: float) -> int | None:
    return js_round((done / total) * 100) if total else None
