"""Timezone-aware date helpers (port of vournalOS DateService).

Two kinds of values live in the database:
  1. Instants (DateTime): Task.dueAt, Event.startAt... Ranges are built in the
     user's timezone: range("day", now, tz) → today's [from, to].
  2. Date-only (@db.Date): RecurringPayment.nextDate, Person.birthday... stored as
     00:00:00Z of the calendar date; compare them with today_date_only(tz).

Every function returns aware UTC datetimes. An unknown timezone counts as UTC.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_DAY = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _zone(tz: str | None):
    try:
        return ZoneInfo(tz) if tz else timezone.utc
    except Exception:  # noqa: BLE001
        return timezone.utc


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _local(moment: datetime, tz: str | None) -> datetime:
    return _utc(moment).astimezone(_zone(tz))


def _at(day: date, clock: time, tz: str | None) -> datetime:
    """The instant a local calendar day and clock time happen in `tz`."""
    return datetime.combine(day, clock, tzinfo=_zone(tz)).astimezone(timezone.utc)


_MIDNIGHT = time(0, 0)
_LAST = time(23, 59, 59, 999000)


def _js_weekday(day: date) -> int:
    return (day.weekday() + 1) % 7  # 0 = Sunday, as Date.getDay()


def _week_start(n) -> int:
    return n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 6 else 1


# ----- instants in the user's timezone -----------------------------------------


def start_of_day(moment: datetime, tz: str) -> datetime:
    return _at(_local(moment, tz).date(), _MIDNIGHT, tz)


def end_of_day(moment: datetime, tz: str) -> datetime:
    return _at(_local(moment, tz).date(), _LAST, tz)


def start_of_week(moment: datetime, tz: str, week_starts_on: int = 1) -> datetime:
    day = _local(moment, tz).date()
    back = (_js_weekday(day) - _week_start(week_starts_on)) % 7
    return _at(day - back * _DAY, _MIDNIGHT, tz)


def end_of_week(moment: datetime, tz: str, week_starts_on: int = 1) -> datetime:
    day = _local(moment, tz).date()
    ahead = 6 - (_js_weekday(day) - _week_start(week_starts_on)) % 7
    return _at(day + ahead * _DAY, _LAST, tz)


def start_of_month(moment: datetime, tz: str) -> datetime:
    return _at(_local(moment, tz).date().replace(day=1), _MIDNIGHT, tz)


def end_of_month(moment: datetime, tz: str) -> datetime:
    day = _local(moment, tz).date()
    first_next = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return _at(first_next - _DAY, _LAST, tz)


def start_of_year(moment: datetime, tz: str) -> datetime:
    return _at(date(_local(moment, tz).year, 1, 1), _MIDNIGHT, tz)


def end_of_year(moment: datetime, tz: str) -> datetime:
    return _at(date(_local(moment, tz).year, 12, 31), _LAST, tz)


def today(tz: str) -> datetime:
    """Start of "today" (an instant) in the user's timezone."""
    return start_of_day(_now(), tz)


def range(period: str, moment: datetime, tz: str, week_starts_on: int = 1) -> dict:  # noqa: A001 — DateService.range
    """{"from", "to"} instants covering the period that contains `moment` (inclusive)."""
    if period == "day":
        return {"from": start_of_day(moment, tz), "to": end_of_day(moment, tz)}
    if period == "week":
        return {"from": start_of_week(moment, tz, week_starts_on), "to": end_of_week(moment, tz, week_starts_on)}
    if period == "month":
        return {"from": start_of_month(moment, tz), "to": end_of_month(moment, tz)}
    if period == "year":
        return {"from": start_of_year(moment, tz), "to": end_of_year(moment, tz)}
    raise ValueError(f"Unknown period: {period}")


def add_days(moment: datetime, days: int, tz: str) -> datetime:
    """Calendar days in the user's timezone: the same local clock time, DST-safe."""
    local = _local(moment, tz)
    return _at(local.date() + days * _DAY, local.timetz().replace(tzinfo=None), tz)


# ----- date-only (@db.Date) helpers ----------------------------------------------


def _date_only(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def local_date_string(moment: datetime, tz: str) -> str:
    """"YYYY-MM-DD" of `moment` as seen in `tz`."""
    return _local(moment, tz).date().isoformat()


def to_date_only(moment: datetime, tz: str | None = None) -> datetime:
    return _date_only(_local(moment, tz or "UTC").date())


def today_date_only(tz: str) -> datetime:
    return to_date_only(_now(), tz)


def parse_date_only(value) -> datetime | None:
    """"YYYY-MM-DD…" (or a datetime) → date-only value; None for anything else.
    Out-of-range parts roll over as JavaScript's Date.UTC does (Feb 30 → Mar 2)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _date_only(_utc(value).date())
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return None
    year, month, day = int(match[1]), int(match[2]), int(match[3])
    months = year * 12 + month - 1
    try:
        return _date_only(date(months // 12, months % 12 + 1, 1) + (day - 1) * _DAY)
    except (ValueError, OverflowError):
        return None


def format_date_only(value: datetime) -> str:
    return _utc(value).date().isoformat()


def add_days_date_only(value: datetime, days: int) -> datetime:
    return value + days * _DAY


def diff_days(a: datetime, b: datetime) -> int:
    """Whole calendar days from `a` to `b` (b − a) for date-only values."""
    return (_utc(b).date() - _utc(a).date()).days


def days_until(value: datetime, tz: str) -> int:
    """Days from the user's today until a date-only value (negative = past)."""
    return diff_days(today_date_only(tz), value)


def diff_calendar_days(later: datetime, earlier: datetime, tz: str) -> int:
    """Calendar-day difference between two instants as seen in `tz`."""
    return (_local(later, tz).date() - _local(earlier, tz).date()).days


def _anniversary(year: int, month: int, day: int) -> datetime:
    first_next = date(year + month // 12, month % 12 + 1, 1)
    return _date_only(date(year, month, min(day, (first_next - _DAY).day)))


def next_anniversary(value: datetime, tz: str) -> datetime:
    """Next yearly anniversary on/after the user's today; Feb 29 → Feb 28 in other years."""
    now = today_date_only(tz)
    value = _utc(value)
    for year in (now.year, now.year + 1):
        candidate = _anniversary(year, value.month, value.day)
        if candidate >= now:
            return candidate
    return _anniversary(now.year + 1, value.month, value.day)


# ----- request values ------------------------------------------------------------

DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?$",
    re.I,
)


def is_iso_date_string(value) -> bool:
    """class-validator's IsDateString for the forms the dashboard sends."""
    return isinstance(value, str) and bool(_ISO_RE.match(value))


def parse_instant(value: str) -> datetime | None:
    """An ISO date/date-time → aware UTC, or None. A time without an offset counts as
    UTC (the server's own zone in production, as JavaScript's Date read it there)."""
    if not is_iso_date_string(value):
        return None
    text = value.strip().replace(" ", "T")
    if text[-1:] in ("z", "Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _utc(moment)


def local_midnight(date_only: str, tz: str) -> datetime:
    """"YYYY-MM-DD" → the instant that local day starts in `tz`."""
    year, month, day = (int(part) for part in date_only.split("-"))
    return _at(date(year, month, day), _MIDNIGHT, tz)


def valid_zone(tz: str | None) -> str:
    """The timezone if it's a real IANA name, else "UTC"."""
    if not tz:
        return "UTC"
    try:
        ZoneInfo(tz)
        return tz
    except Exception:  # noqa: BLE001
        return "UTC"
