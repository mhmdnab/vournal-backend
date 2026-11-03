"""Date helpers shared by the calendar, events, reminders and notifications
(port of vournalOS calendar.utils). An all-day value is stored as the LOCAL
midnight of its calendar date in the user's timezone, an instant.
"""

from datetime import date, datetime, time, timedelta, timezone

from app.os.common import dates

DAY = timedelta(days=1)
DATE_ONLY_RE = dates.DATE_ONLY_RE

safe_tz = dates.valid_zone


def is_date_only_string(value) -> bool:
    return isinstance(value, str) and bool(DATE_ONLY_RE.match(value))


def local_midnight(day: str, tz: str) -> datetime:
    return dates.local_midnight(day[:10], safe_tz(tz))


def local_date_time(day: str, hhmm: str, tz: str) -> datetime:
    """The instant of local wall-clock HH:mm on `day` in `tz`."""
    year, month, dd = (int(p) for p in day[:10].split("-"))
    hour, minute = (int(p or 0) for p in (hhmm.split(":") + ["0"])[:2])
    return dates._at(date(year, month, dd), time(hour, minute), safe_tz(tz))


def local_date_str(moment: datetime, tz: str) -> str:
    return dates.local_date_string(moment, safe_tz(tz))


def local_time_str(moment: datetime, tz: str) -> str:
    return dates._local(moment, safe_tz(tz)).strftime("%H:%M")


def local_weekday(moment: datetime, tz: str) -> int:
    """0 = Sunday."""
    return dates._js_weekday(dates._local(moment, safe_tz(tz)).date())


def is_local_midnight(moment: datetime, tz: str) -> bool:
    local = dates._local(moment, safe_tz(tz))
    return (local.hour, local.minute, local.second, local.microsecond // 1000) == (0, 0, 0, 0)


def to_all_day_instant(value: str, tz: str) -> datetime:
    """"YYYY-MM-DD" or any ISO instant → local midnight of that local calendar date."""
    if is_date_only_string(value):
        return local_midnight(value, tz)
    return local_midnight(local_date_str(dates.parse_instant(value), tz), tz)


def date_only_str(value: datetime) -> str:
    return dates._utc(value).date().isoformat()


def date_only_value(day: str) -> datetime:
    year, month, dd = (int(p) for p in day[:10].split("-"))
    return datetime(year, month, dd, tzinfo=timezone.utc)


def add_days_str(day: str, n: int) -> str:
    return date_only_str(date_only_value(day) + n * DAY)


def diff_days_str(start: str, end: str) -> int:
    return round((date_only_value(end) - date_only_value(start)) / DAY)


def anniversary_str(source: datetime, year: int) -> str:
    """Yearly anniversary in `year`; Feb 29 → Feb 28 in other years."""
    return dates._anniversary(year, dates._utc(source).month, dates._utc(source).day).date().isoformat()


ENTITY_LINKS = {
    "TASK": lambda i: f"/tasks?id={i}",
    "EVENT": lambda i: f"/calendar?id={i}",
    "PROJECT": lambda i: f"/projects/{i}",
    "TRANSACTION": lambda i: f"/finance/transactions?id={i}",
    "RECURRING_PAYMENT": lambda i: f"/finance/recurring?id={i}",
    "BUDGET": lambda i: "/finance/budgets",
    "GOAL": lambda i: f"/goals?id={i}",
    "HABIT": lambda i: f"/habits?id={i}",
    "MAINTENANCE": lambda i: f"/maintenance?id={i}",
    "DOCUMENT": lambda i: f"/documents?id={i}",
    "SHOPPING_LIST": lambda i: f"/shopping?list={i}",
    "SHOPPING_ITEM": lambda i: "/shopping",
    "MEAL": lambda i: "/meals",
    "RECIPE": lambda i: f"/meals?recipe={i}",
    "NOTE": lambda i: f"/notes?id={i}",
    "COURSE": lambda i: f"/study?id={i}",
    "ASSESSMENT": lambda i: f"/study?assessment={i}",
    "TRIP": lambda i: f"/travel/{i}",
    "PERSON": lambda i: f"/people?id={i}",
    "IMPORTANT_DATE": lambda i: "/people",
    "REMINDER": lambda i: f"/reminders?id={i}",
    "DEBT": lambda i: f"/finance/debts?id={i}",
}


def entity_link(kind: str | None, id: str | None) -> str | None:
    if not kind or not id:
        return None
    link = ENTITY_LINKS.get(kind)
    return link(id) if link else None
