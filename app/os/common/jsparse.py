"""Reading request values the way vournalOS's JavaScript did, pinned by
tests/vournal_os/golden/jsparse.json (printed by Node):

- is_iso8601: validator.js isISO8601, behind class-validator's IsDateString.
- js_date: `new Date(value)` on the server (UTC), for the forms that validator accepts.
  Day 29–31 rolls over (30 February is 2 March); "24:00" is the next midnight.
- js_number: `Number(text)` for a query string or text input.
- js_to_string / decimal_places: Number#toString, and class-validator's
  maxDecimalPlaces count built on it.
- js_sum: `values.reduce((s, v) => s + v, 0)`. Python 3.12's sum() compensates float
  rounding, so its last bit can differ from JavaScript's.
"""

import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# JavaScript's \s and String#trim whitespace (WhiteSpace + LineTerminator)
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000\ufeff"

# validator.js lib/isISO8601.js `iso8601`, as written; \Z because JS $ never matches before a newline
ISO8601 = re.compile(
    r"^([\+-]?\d{4}(?!\d{2}\b))((-?)((0[1-9]|1[0-2])(\3([12]\d|0[1-9]|3[01]))?|W([0-4]\d|5[0-3])(-?[1-7])?|"
    r"(00[1-9]|0[1-9]\d|[12]\d{2}|3([0-5]\d|6[1-6])))([T" + re.escape(_JS_SPACE) + r"]((([01]\d|2[0-3])((:?)[0-5]\d)?|24:?00)"
    r"([\.,]\d+(?!:))?)?(\17[0-5]\d([\.,]\d+)?)?([zZ]|([\+-])([01]\d|2[0-3]):?([0-5]\d)?)?)?)?\Z",
    re.ASCII,
)

# what V8's Date parser makes of the calendar forms (week and ordinal dates are Invalid Date)
_V8 = re.compile(
    r"([+-]?\d{4})(?:-(\d{2})(?:-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
    r"([Zz]|[+-]\d{2}:?\d{2})?)?)?)?",
    re.ASCII,
)


def is_iso8601(value) -> bool:
    return isinstance(value, str) and ISO8601.match(value) is not None


def roll_date(year: int, month: int, day: int) -> date | None:
    """Date.UTC(year, month - 1, day): days past the month's end roll into the next."""
    months = year * 12 + month - 1
    try:
        return date(months // 12, months % 12 + 1, 1) + timedelta(days=day - 1)
    except (ValueError, OverflowError):
        return None


def js_date(value) -> datetime | None:
    """`new Date(value)` as an aware UTC datetime, None for Invalid Date. A time with no
    offset is the server's local time, which is UTC on Render."""
    if not isinstance(value, str):
        return None
    m = _V8.fullmatch(value)
    if not m:
        return None
    year, month, day = int(m[1]), int(m[2] or 1), int(m[3] or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    hour, minute, second = int(m[4] or 0), int(m[5] or 0), int(m[6] or 0)
    if hour > 24 or minute > 59 or second > 59 or (hour == 24 and (minute or second or m[7] and int(m[7]))):
        return None
    millis = int((m[7] or "0")[:3].ljust(3, "0"))
    offset = timedelta(0)
    if m[8] and m[8] not in ("Z", "z"):
        sign = -1 if m[8][0] == "-" else 1
        digits = m[8][1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:])
        if hours > 23 or minutes > 59:
            return None
        offset = sign * timedelta(hours=hours, minutes=minutes)
    start = roll_date(year, month, day)
    if start is None:
        return None
    moment = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    return moment + timedelta(hours=hour, minutes=minute, seconds=second, milliseconds=millis) - offset


_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_RADIX = {"x": (16, "0123456789abcdefABCDEF"), "o": (8, "01234567"), "b": (2, "01")}


def js_number(text) -> float:
    """Number(text): trimmed; "" → 0; Infinity; 0x / 0o / 0b; NaN for anything else."""
    if not isinstance(text, str):
        return math.nan
    s = text.strip(_JS_SPACE)
    if s == "":
        return 0.0
    if s in ("Infinity", "+Infinity"):
        return math.inf
    if s == "-Infinity":
        return -math.inf
    if len(s) > 2 and s[0] == "0" and s[1].lower() in _RADIX:
        base, digits = _RADIX[s[1].lower()]
        return float(int(s[2:], base)) if all(c in digits for c in s[2:]) else math.nan
    return float(s) if _DECIMAL.fullmatch(s) else math.nan


def js_to_string(value: float) -> str:
    """Number#toString for a finite number: shortest round-trip digits, exponent form
    from 1e21 up and below 1e-6."""
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    t = Decimal(repr(abs(float(value)))).as_tuple()
    raw = "".join(map(str, t.digits))
    n = len(raw) + t.exponent  # value = 0.<digits> × 10^n
    digits = raw.lstrip("0")
    n -= len(raw) - len(digits)
    digits = digits.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    body = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{body}e{'+' if e >= 0 else '-'}{abs(e)}"


def decimal_places(value) -> int | None:
    """class-validator's maxDecimalPlaces count: the characters after "." in toString()
    ("1.5e-7" → 4); None where class-validator throws (1e-7 has no ".")."""
    if isinstance(value, int) or math.fmod(value, 1) == 0:
        return 0
    _, dot, frac = js_to_string(value).partition(".")
    return len(frac) if dot else None


def js_sum(values) -> float:
    """Left-to-right float addition, as Array#reduce added."""
    total = 0
    for value in values:
        total += value
    return total
