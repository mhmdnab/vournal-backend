"""Repeat rules (port of vournalOS rrule.util + RecurrenceService, on python-dateutil).

Rules are RFC 5545 RRULE bodies stored WITHOUT DTSTART: the row's own date is the
start. Instants repeat on the wall clock of the user's timezone (DST-safe: "every
Monday 09:00 Beirut time"); date-only values repeat in UTC at 00:00.

Month-end safety (vournalOS's, not RFC 5545's): a monthly repeat on the 29th-31st
falls back to the last day of shorter months, and a yearly repeat on Feb 29 to
Feb 28. Answers are pinned to the JS implementation by tests/vournal_os/golden.
"""

import logging
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import rrule as du

log = logging.getLogger("vournal.os")

RRULE_MAX_LENGTH = 500
MAX_OCCURRENCES = 2000
AVG_DAYS_PER_MONTH = 365 / 12
WEEKS_PER_MONTH = 52 / 12

YEARLY, MONTHLY, WEEKLY, DAILY, HOURLY, MINUTELY, SECONDLY = range(7)  # same values as dateutil and rrule.js
_FREQS = {"YEARLY": YEARLY, "MONTHLY": MONTHLY, "WEEKLY": WEEKLY, "DAILY": DAILY, "HOURLY": HOURLY, "MINUTELY": MINUTELY, "SECONDLY": SECONDLY}
_FREQ_NAMES = {v: k for k, v in _FREQS.items()}
_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]  # 0 = Monday, as in rrule.js
_NUMBER_KEYS = {
    "COUNT": "count",
    "INTERVAL": "interval",
    "BYSETPOS": "bysetpos",
    "BYMONTH": "bymonth",
    "BYMONTHDAY": "bymonthday",
    "BYYEARDAY": "byyearday",
    "BYWEEKNO": "byweekno",
    "BYHOUR": "byhour",
    "BYMINUTE": "byminute",
    "BYSECOND": "bysecond",
    "BYEASTER": "byeaster",
}
_LIST_KEYS = ("bysetpos", "bymonth", "bymonthday", "byyearday", "byweekno", "byhour", "byminute", "bysecond")


@dataclass(frozen=True)
class Weekday:
    weekday: int  # 0 = Monday
    n: int | None = None  # 1MO = first Monday, -1FR = last Friday


# ---------------------------------------------------------------------------
# Parsing and validation (rrule.util)


def normalize_rrule(value: str) -> str:
    """The stored form: the bare body, no "RRULE:" prefix, no DTSTART, upper-case."""
    lines = [line for line in re.split(r"\r?\n", value.strip()) if not re.match(r"^DTSTART", line.strip(), re.I)]
    body = re.sub(r"^RRULE:", "", ";".join(lines), flags=re.I)
    parts = [p.strip() for p in body.split(";")]
    return ";".join(p for p in parts if p and not re.match(r"^DTSTART", p, re.I)).upper()


def _number(value: str):
    """rrule.js parseNumber: one value → a number (or the raw text), several → a list."""

    def one(v):
        return int(v) if re.fullmatch(r"[+-]?\d+", v) else v

    return [one(v) for v in value.split(",")] if "," in value else one(value)


def _weekdays(value: str) -> list[Weekday]:
    out = []
    for day in value.split(","):
        if len(day) == 2:
            if day not in _DAYS:
                raise ValueError(f"Invalid weekday string: {day}")
            out.append(Weekday(_DAYS.index(day)))
            continue
        match = re.fullmatch(r"([+-]?\d{1,2})([A-Z]{2})", day)
        if not match or match[2] not in _DAYS:
            raise ValueError(f"Invalid weekday string: {day}")
        out.append(Weekday(_DAYS.index(match[2]), int(match[1])))
    return out


def _until(value: str) -> datetime:
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(T(\d{2})(\d{2})(\d{2})Z?)?", value)
    if not match:
        raise ValueError(f"Invalid UNTIL value: {value}")
    return datetime(
        int(match[1]), int(match[2]), int(match[3]), int(match[5] or 0), int(match[6] or 0), int(match[7] or 0)
    )


def _parse_body(body: str) -> dict:
    """rrule.js parseString on a bare body. Keys keep the rule's order (describe needs it)."""
    opts: dict = {}
    for attr in body.split(";"):
        key, _, value = attr.partition("=")
        key = key.upper()
        if key == "FREQ":
            opts["freq"] = _FREQS.get(value.upper())
        elif key == "WKST":
            opts["wkst"] = _DAYS.index(value.upper()) if value.upper() in _DAYS else None
        elif key in _NUMBER_KEYS:
            opts[_NUMBER_KEYS[key]] = _number(value)
        elif key in ("BYDAY", "BYWEEKDAY"):
            opts["byweekday"] = _weekdays(value)
        elif key == "UNTIL":
            opts["until"] = _until(value)
        elif key in ("DTSTART", "TZID"):
            continue
        else:
            raise ValueError(f"Unknown RRULE property '{key}'")
    return opts


def parse_rrule(value: str) -> dict:
    body = normalize_rrule(value)
    if not body:
        raise ValueError("Empty recurrence rule")
    opts = _parse_body(body)
    if opts.get("freq") is None:
        raise ValueError("Recurrence rule must include FREQ")
    return opts


def validate_rrule(value) -> str | None:
    """An error message when `value` isn't an acceptable rule, else None (validateRRule)."""
    if not isinstance(value, str) or value.strip() == "":
        return "must be a non-empty RRULE string"
    if len(value) > RRULE_MAX_LENGTH:
        return f"must be at most {RRULE_MAX_LENGTH} characters"
    if re.search("DTSTART", value, re.I):
        return "must not contain DTSTART (the entity date is the start)"
    try:
        opts = parse_rrule(value)
        if opts["freq"] in (MINUTELY, SECONDLY):
            return "frequency must be HOURLY, DAILY, WEEKLY, MONTHLY or YEARLY"
        interval = opts.get("interval")
        if interval is not None and (not isinstance(interval, int) or interval < 1 or interval > 1000):
            return "INTERVAL must be between 1 and 1000"
        start = datetime(2020, 1, 1)
        _build_rule(opts, start, None).after(start, inc=True)
        return None
    except Exception as exc:  # noqa: BLE001 — any failure means the rule can't be used
        return f"is not a valid recurrence rule ({exc})"


def is_valid(rule: str) -> bool:
    try:
        parse_rrule(rule)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Occurrences (RecurrenceService)


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _valid_zone(tz: str | None) -> ZoneInfo | None:
    if not tz or tz == "UTC":
        return None
    try:
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — unknown zone: evaluate in UTC, like NestJS
        return None


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _to_floating(moment: datetime, zone: ZoneInfo | None) -> datetime:
    """Instant → naive wall-clock time in `zone` (UTC when none)."""
    moment = _utc(moment)
    return (moment.astimezone(zone) if zone else moment).replace(tzinfo=None)


def _from_floating(floating: datetime, zone: ZoneInfo | None) -> datetime:
    """Inverse of _to_floating: wall-clock time in `zone` → aware UTC instant."""
    if zone is None:
        return floating.replace(tzinfo=timezone.utc)
    return floating.replace(tzinfo=zone).astimezone(timezone.utc)


def _month_end_fallback(opts: dict, dtstart: datetime) -> None:
    """Monthly on the 29th-31st → BYMONTHDAY=28..day;BYSETPOS=-1, and yearly on Feb 29
    → BYMONTH=2;BYMONTHDAY=28,29;BYSETPOS=-1, so short months aren't skipped."""
    if any(_as_list(opts.get(k)) for k in ("byweekday", "bysetpos", "byyearday", "byweekno")):
        return
    month_days = _as_list(opts.get("bymonthday"))
    if opts["freq"] == MONTHLY:
        day = dtstart.day if not month_days else month_days[0] if len(month_days) == 1 else 0
        if isinstance(day, int) and 29 <= day <= 31:
            opts["bymonthday"] = list(range(28, day + 1))
            opts["bysetpos"] = [-1]
    elif opts["freq"] == YEARLY:
        months = _as_list(opts.get("bymonth"))
        month = dtstart.month if not months else months[0] if len(months) == 1 else 0
        day = dtstart.day if not month_days else month_days[0] if len(month_days) == 1 else 0
        if month == 2 and day == 29:
            opts["bymonth"], opts["bymonthday"], opts["bysetpos"] = [2], [28, 29], [-1]


def _build_rule(opts: dict, dtstart: datetime, until: datetime | None) -> du.rrule:
    """A dateutil rule on naive (floating) times. dtstart is whole seconds here."""
    kwargs = {"dtstart": dtstart, "interval": opts.get("interval") or 1}
    for key in _LIST_KEYS:
        if opts.get(key) is not None:
            kwargs[key] = _as_list(opts[key])
    if opts.get("byweekday"):
        kwargs["byweekday"] = [du.weekday(w.weekday, w.n or None) for w in opts["byweekday"]]
    if opts.get("wkst") is not None:
        kwargs["wkst"] = opts["wkst"]
    if opts.get("count") is not None:
        kwargs["count"] = opts["count"]
    if opts.get("byeaster") is not None:
        kwargs["byeaster"] = opts["byeaster"]
    if until is not None:
        kwargs["until"] = until
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # COUNT with UNTIL: allowed, as in rrule.js
        return du.rrule(opts["freq"], **kwargs)


@dataclass
class _Context:
    rule: du.rrule
    zone: ZoneInfo | None
    shift: timedelta  # the anchor's sub-second part: rrule.js keeps milliseconds, dateutil doesn't


def _context(rule: str, anchor: datetime, tz, date_only: bool, until: datetime | None) -> _Context | None:
    if not rule or not isinstance(anchor, datetime):
        return None
    try:
        opts = parse_rrule(rule)
    except Exception as exc:  # noqa: BLE001
        log.warning('Invalid rrule "%s": %s', rule, exc)
        return None
    zone = None if date_only else _valid_zone(tz)
    start = _to_floating(anchor, zone)
    shift = timedelta(microseconds=start.microsecond - start.microsecond % 1000)
    start = start.replace(microsecond=0)
    _month_end_fallback(opts, start)
    end = opts.pop("until", None)
    if until is not None:
        cap = _to_floating(until, zone)
        if end is None or cap < end:
            end = cap
    try:
        built = _build_rule(opts, start, None if end is None else end - shift)
    except Exception as exc:  # noqa: BLE001
        log.warning('Cannot build rrule "%s": %s', rule, exc)
        return None
    return _Context(built, zone, shift)


def next_occurrence(
    rule: str,
    anchor: datetime,
    after: datetime,
    *,
    tz: str | None = None,
    date_only: bool = False,
    until: datetime | None = None,
    inclusive: bool = False,
) -> datetime | None:
    """First occurrence strictly after `after` (at it with `inclusive`); None when the
    series has ended or the rule is invalid."""
    ctx = _context(rule, anchor, tz, date_only, until)
    if ctx is None:
        return None
    found = ctx.rule.after(_to_floating(after, ctx.zone) - ctx.shift, inc=inclusive)
    return None if found is None else _from_floating(found + ctx.shift, ctx.zone)


def between(
    rule: str,
    anchor: datetime,
    start: datetime,
    end: datetime,
    ex_dates=(),
    *,
    tz: str | None = None,
    date_only: bool = False,
    until: datetime | None = None,
) -> list[datetime]:
    """Every occurrence in [start, end], minus `ex_dates`, at most 2000."""
    if _utc(end) < _utc(start):
        return []
    ctx = _context(rule, anchor, tz, date_only, until)
    if ctx is None:
        return []
    low = _to_floating(start, ctx.zone) - ctx.shift
    high = _to_floating(end, ctx.zone) - ctx.shift
    raw = []
    for found in ctx.rule.xafter(low, inc=True):
        if found > high or len(raw) >= MAX_OCCURRENCES:
            break
        raw.append(found)
    excluded = {_utc(d) for d in ex_dates}
    excluded_days = None if ctx.zone else {_utc(d).date() for d in ex_dates}
    out = []
    for found in raw:
        moment = _from_floating(found + ctx.shift, ctx.zone)
        if moment in excluded or (excluded_days is not None and moment.date() in excluded_days):
            continue
        out.append(moment)
    return out


def monthly_factor(rule: str) -> float:
    """Average occurrences per month (the "monthly cost" of a recurring payment)."""
    try:
        o = parse_rrule(rule)
    except Exception:  # noqa: BLE001
        return 0
    interval = o.get("interval") if isinstance(o.get("interval"), int) and o.get("interval") > 0 else 1
    weekdays = _as_list(o.get("byweekday"))
    month_days = _as_list(o.get("bymonthday"))
    months = _as_list(o.get("bymonth"))
    set_pos = _as_list(o.get("bysetpos"))
    month_share = len(months) / 12 if months else 1
    freq = o["freq"]
    if freq == YEARLY:
        per_year = len(set_pos) if set_pos else max(len(months), 1) * max(len(month_days), len(weekdays) or 1)
        per_month = per_year / 12
    elif freq == MONTHLY:
        if set_pos:
            n = len(set_pos)
        elif month_days:
            n = len(month_days)
        elif weekdays:
            n = 0
            for w in weekdays:
                n = n + (1 if w.n else WEEKS_PER_MONTH)
        else:
            n = 1
        per_month = n * month_share
    elif freq == WEEKLY:
        per_month = max(len(weekdays), 1) * WEEKS_PER_MONTH * month_share
    elif freq == DAILY:
        per_month = (len(weekdays) / 7 if weekdays else 1) * AVG_DAYS_PER_MONTH * month_share
    elif freq == HOURLY:
        per_month = 24 * AVG_DAYS_PER_MONTH
    elif freq == MINUTELY:
        per_month = 60 * 24 * AVG_DAYS_PER_MONTH
    elif freq == SECONDLY:
        per_month = 3600 * 24 * AVG_DAYS_PER_MONTH
    else:
        return 0
    return per_month / interval


# ---------------------------------------------------------------------------
# Descriptions: English is a port of rrule.js's toText, Arabic of describeArabic

_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_COMMON = ["count", "until", "interval", "byweekday", "bymonthday", "bymonth"]
_IMPLEMENTED = {
    HOURLY: _COMMON,
    MINUTELY: _COMMON,
    DAILY: ["byhour", *_COMMON],
    WEEKLY: _COMMON,
    MONTHLY: _COMMON,
    YEARLY: ["byweekno", "byyearday", *_COMMON],
}
_AR_WEEKDAYS = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]


def _nth(n) -> str:
    n = int(n)
    if n == -1:
        return "last"
    npos = abs(n)
    suffix = "st" if npos in (1, 21, 31) else "nd" if npos in (2, 22) else "rd" if npos in (3, 23) else "th"
    return f"{npos}{suffix} last" if n < 0 else f"{npos}{suffix}"


def _plural(n: int) -> bool:
    return n % 100 != 1


def _list(items, callback=str, final=None, delim=",") -> str:
    texts = [callback(i) for i in items]
    if not final:
        return f"{delim} ".join(texts)
    out = ""
    for i, text in enumerate(texts):
        if i:
            out += f" {final} " if i == len(texts) - 1 else f"{delim} "
        out += text
    return out


def _weekday_text(w: Weekday) -> str:
    return (f"{_nth(w.n)} " if w.n else "") + _DAY_NAMES[(w.weekday + 1) % 7]


class _ToText:
    def __init__(self, o: dict):
        self.o = o
        self.interval = o.get("interval") or 1
        self.parts = ["every"]
        self.bymonthday = None
        if o.get("bymonthday") is not None:
            values = _as_list(o["bymonthday"])
            positive = sorted(v for v in values if v > 0)
            negative = sorted((v for v in values if v < 0), reverse=True)
            self.bymonthday = (positive + negative) or None
        self.weekdays = None
        if o.get("byweekday"):
            days = {_DAYS[w.weekday] for w in o["byweekday"]}
            all_weeks = sorted((w for w in o["byweekday"] if not w.n), key=lambda w: w.weekday)
            some_weeks = sorted((w for w in o["byweekday"] if w.n), key=lambda w: w.weekday)
            self.weekdays = {
                "all": all_weeks or None,
                "some": some_weeks or None,
                "is_weekdays": {"MO", "TU", "WE", "TH", "FR"} <= days and not days & {"SA", "SU"},
                "is_every_day": set(_DAYS) <= days,
            }

    def add(self, text: str) -> "_ToText":
        self.parts.extend([" ", text])
        return self

    def fully_convertible(self) -> bool:
        freq = self.o["freq"]
        if freq not in _IMPLEMENTED:
            return False
        if self.o.get("until") and self.o.get("count"):
            return False
        for key in [*self.o.keys(), "dtstart"]:
            if key in ("dtstart", "tzid", "wkst", "freq"):
                return True
            if key not in _IMPLEMENTED[freq]:
                return False
        return True

    def text(self) -> str:
        freq = self.o["freq"]
        if freq not in _IMPLEMENTED:
            return "RRule error: Unable to fully convert this rrule to text"
        getattr(self, _FREQ_NAMES[freq])()
        if self.o.get("until"):
            until = self.o["until"]
            self.add("until").add(f"{_MONTH_NAMES[until.month - 1]} {until.day}, {until.year}")
        elif self.o.get("count"):
            count = self.o["count"]
            self.add("for").add(str(count)).add("times" if _plural(count) else "time")
        if not self.fully_convertible():
            self.add("(~ approximate)")
        return "".join(self.parts)

    def _months(self):
        self.add(_list(_as_list(self.o["bymonth"]), lambda m: _MONTH_NAMES[m - 1], "and"))

    def _month_days(self):
        if self.weekdays and self.weekdays["all"]:
            self.add("on").add(_list(self.weekdays["all"], _weekday_text, "or")).add("the").add(
                _list(self.bymonthday, _nth, "or")
            )
        else:
            self.add("on the").add(_list(self.bymonthday, _nth, "and"))

    def _by_weekday(self):
        w = self.weekdays
        if w["all"] and not w["is_weekdays"]:
            self.add("on").add(_list(w["all"], _weekday_text))
        if w["some"]:
            if w["all"]:
                self.add("and")
            self.add("on the").add(_list(w["some"], _weekday_text, "and"))

    def _by_hour(self):
        self.add("at").add(_list(_as_list(self.o["byhour"]), str, "and"))

    def HOURLY(self):
        if self.interval != 1:
            self.add(str(self.interval))
        self.add("hours" if _plural(self.interval) else "hour")

    def MINUTELY(self):
        if self.interval != 1:
            self.add(str(self.interval))
        self.add("minutes" if _plural(self.interval) else "minute")

    def DAILY(self):
        if self.interval != 1:
            self.add(str(self.interval))
        if self.weekdays and self.weekdays["is_weekdays"]:
            self.add("weekdays" if _plural(self.interval) else "weekday")
        else:
            self.add("days" if _plural(self.interval) else "day")
        if self.o.get("bymonth") is not None:
            self.add("in")
            self._months()
        if self.bymonthday:
            self._month_days()
        elif self.weekdays:
            self._by_weekday()
        elif self.o.get("byhour") is not None:
            self._by_hour()

    def WEEKLY(self):
        if self.interval != 1:
            self.add(str(self.interval)).add("weeks" if _plural(self.interval) else "week")
        if self.weekdays and self.weekdays["is_weekdays"]:
            if self.interval == 1:
                self.add("weekdays" if _plural(self.interval) else "weekday")
            else:
                self.add("on").add("weekdays")
        elif self.weekdays and self.weekdays["is_every_day"]:
            self.add("days" if _plural(self.interval) else "day")
        else:
            if self.interval == 1:
                self.add("week")
            if self.o.get("bymonth") is not None:
                self.add("in")
                self._months()
            if self.bymonthday:
                self._month_days()
            elif self.weekdays:
                self._by_weekday()
            if self.o.get("byhour") is not None:
                self._by_hour()

    def MONTHLY(self):
        if self.o.get("bymonth") is not None:
            if self.interval != 1:
                self.add(str(self.interval)).add("months")
                if _plural(self.interval):
                    self.add("in")
            self._months()
        else:
            if self.interval != 1:
                self.add(str(self.interval))
            self.add("months" if _plural(self.interval) else "month")
        if self.bymonthday:
            self._month_days()
        elif self.weekdays and self.weekdays["is_weekdays"]:
            self.add("on").add("weekdays")
        elif self.weekdays:
            self._by_weekday()

    def YEARLY(self):
        if self.o.get("bymonth") is not None:
            if self.interval != 1:
                self.add(str(self.interval)).add("years")
            self._months()
        else:
            if self.interval != 1:
                self.add(str(self.interval))
            self.add("years" if _plural(self.interval) else "year")
        if self.bymonthday:
            self._month_days()
        elif self.weekdays:
            self._by_weekday()
        if self.o.get("byyearday") is not None:
            self.add("on the").add(_list(_as_list(self.o["byyearday"]), _nth, "and")).add("day")
        if self.o.get("byweekno") is not None:
            weeks = _as_list(self.o["byweekno"])
            self.add("in").add("weeks" if _plural(len(weeks)) else "week").add(_list(weeks, str, "and"))


def _arabic_every(n: int, one: str, two: str, few: str, many: str) -> str:
    if n <= 1:
        return f"كل {one}"
    if n == 2:
        return f"كل {two}"
    return f"كل {n} {few}" if n <= 10 else f"كل {n} {many}"


def _arabic_list(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{'، '.join(items[:-1])} و{items[-1]}"


def _describe_arabic(o: dict) -> str:
    n = o.get("interval") if isinstance(o.get("interval"), int) and o["interval"] > 1 else 1
    weekdays = [_AR_WEEKDAYS[(w.weekday + 1) % 7] for w in _as_list(o.get("byweekday"))]
    month_days = _as_list(o.get("bymonthday"))
    freq = o["freq"]
    if freq == DAILY:
        text = _arabic_every(n, "يوم", "يومين", "أيام", "يومًا")
    elif freq == WEEKLY:
        text = _arabic_every(n, "أسبوع", "أسبوعين", "أسابيع", "أسبوعًا")
        if weekdays:
            text += f" {'يوم' if len(weekdays) == 1 else 'أيام'} {_arabic_list(weekdays)}"
    elif freq == MONTHLY:
        text = _arabic_every(n, "شهر", "شهرين", "أشهر", "شهرًا")
        if month_days:
            days = ["الأخير" if d == -1 else str(d) for d in month_days]
            text += f" في {'اليوم' if len(month_days) == 1 else 'الأيام'} {_arabic_list(days)}"
    elif freq == YEARLY:
        text = _arabic_every(n, "سنة", "سنتين", "سنوات", "سنة")
    elif freq == HOURLY:
        text = _arabic_every(n, "ساعة", "ساعتين", "ساعات", "ساعة")
    else:
        return "جدول مخصّص"
    count = o.get("count")
    if count:
        text += "، " + ("مرة واحدة" if count == 1 else "مرتين" if count == 2 else f"{count} {'مرات' if count <= 10 else 'مرة'}")
    until = o.get("until")
    if until:
        # Intl ar-LB-u-nu-latn, dateStyle "medium", UTC: dd‏/MM‏/yyyy with right-to-left marks
        text += f" حتى {until.day:02d}‏/{until.month:02d}‏/{until.year}"
    return text


def describe(rule: str | None, lang: str = "en") -> str:
    """Human text, e.g. "Every week on Monday, Wednesday, Friday"."""
    if not rule:
        return "لا يتكرّر" if lang == "ar" else "Does not repeat"
    try:
        o = parse_rrule(rule)
        if lang == "ar":
            return _describe_arabic(o)
        _build_rule(o, datetime(2020, 1, 1), None)  # a rule rrule.js couldn't build has no text
        text = _ToText(o).text()
        return text[:1].upper() + text[1:]
    except Exception:  # noqa: BLE001
        return "جدول مخصّص" if lang == "ar" else "Custom schedule"
