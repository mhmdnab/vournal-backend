"""Words and formats of the overview sections (port of vournalOS insights/lib/i18n.ts):
English and Arabic, with money and numbers isolated left-to-right inside Arabic text so
they never reorder. The tables are generated from the NestJS build
(tests/vournal_os/golden/insights_golden.mjs → app/os/data/insights_i18n.json)."""

import json
import math
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from app.os.common.i18n import fmt, plural

_DATA = json.loads((Path(__file__).parent / "data" / "insights_i18n.json").read_text())
MESSAGES = _DATA["messages"]
_LRI, _PDI = "⁦", "⁩"  # left-to-right isolate, pop directional isolate
_EN_MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EN_MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                   "November", "December")


def js_round(x: float) -> int:
    """Math.round: halves go up (towards +∞)."""
    r = math.floor(x)
    return int(r + 1 if x - r >= 0.5 else r)


def js_string(value) -> str:
    """String(number): whole numbers without a decimal point."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def ltr(lang: str, text: str) -> str:
    return f"{_LRI}{text}{_PDI}" if lang == "ar" and text else text


def _fixed(value: float, decimals: int) -> str:
    """Intl's rounding: the number's shortest decimal form (2.675 stays 2.675, as V8's ICU
    reads it), halves away from zero, with grouping."""
    rounded = Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return f"{rounded:,.{decimals}f}"


def money(lang: str, amount: float, currency: str) -> str:
    """"$1,234.50" in en-US conventions; whole amounts and ≥ 10,000 without cents."""
    amount = float(amount)
    abs_amount = abs(amount)
    decimals = 0 if abs_amount.is_integer() or abs_amount >= 10_000 else 2
    affix = _DATA["currency"].get(currency)
    if affix is None:  # Intl refused the code: toFixed and the code after it
        text = f"{Decimal(abs_amount).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP):.{decimals}f} {currency}"
    else:
        text = f"{affix['prefix']}{_fixed(abs_amount, decimals)}{affix['suffix']}"
    return ltr(lang, f"-{text}" if amount < 0 else text)


def percent(lang: str, value: float) -> str:
    return ltr(lang, f"{js_round(value)}%")


def num(lang: str, value) -> str:
    return ltr(lang, js_string(value))


def rel_day(lang: str, n: int) -> str:
    """"today" / "tomorrow" / "yesterday" / "in 4 days" / "3 days ago"."""
    words = _DATA["rel"][lang]
    if n == 0:
        return words["today"]
    if n == 1:
        return words["tomorrow"]
    if n == -1:
        return words["yesterday"]
    return plural(lang, -n, words["daysAgo"]) if n < 0 else plural(lang, n, words["inDays"])


def fmt_day(lang: str, key: str, *, day: bool = False, month: bool = True, year: bool = False) -> str:
    """A "YYYY-MM-DD" day with the long month, as Intl wrote it: "5 تشرين الأول",
    "5 تشرين الأول 2026", "October 5, 2026"."""
    y, m, d = int(key[:4]), int(key[5:7]), int(key[8:10])
    if lang == "ar":
        name = _DATA["months"]["long"][m - 1]
        return " ".join(p for p in (str(d) if day else "", name, str(y) if year else "") if p)
    name = _EN_MONTHS_LONG[m - 1]
    text = f"{name} {d}" if day else name
    return f"{text}, {y}" if year else text


def month_short(lang: str, year: int, month1: int) -> str:
    return _DATA["months"]["short"][month1 - 1] if lang == "ar" else _EN_MONTHS_SHORT[month1 - 1]


def enum_label(lang: str, name: str, value: str | None) -> str:
    """The enum's label in the user's language; an unknown value is humanised."""
    if not value:
        return ""
    table = _DATA["enums"][name]
    return table[lang].get(value) or table["en"].get(value) or value[0] + value[1:].lower().replace("_", " ")


def shared(lang: str, key: str, **values) -> str:
    return fmt(_DATA["shared"][lang][key], **values)
