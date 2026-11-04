"""Notification titles and bodies in English and Arabic (port of vournalOS
notifications.messages.ts). The tables, and the weekday and month names as Intl writes
them, are generated from the NestJS build (tests/vournal_os/golden/notifications_golden.mjs)."""

import json
from datetime import date as calendar_day, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.os.common import dates
from app.os.common.i18n import fmt, plural

_DATA = json.loads((Path(__file__).parent / "data" / "notification_texts.json").read_text())
# en-US currency affixes, shared with the overview sections' generated table
_MONEY_AFFIXES = json.loads((Path(__file__).parent / "data" / "insights_i18n.json").read_text())["currency"]
_LRI, _PDI = "\u2066", "\u2069"  # left-to-right isolate, pop directional isolate


class Texts:
    def __init__(self, lang: str):
        self.lang = lang
        self.messages = _DATA["messages"][lang]
        self.plurals = _DATA["plurals"][lang]
        self.calendar = _DATA["calendar"][lang]

    def t(self, key: str, **values) -> str:
        return fmt(self.messages[key], **values)

    def n(self, key: str, count: int, **values) -> str:
        return plural(self.lang, count, self.plurals[key], **values)

    def ltr(self, text: str) -> str:
        """Numbers, money and times stay in reading order inside Arabic text."""
        return f"{_LRI}{text}{_PDI}" if self.lang == "ar" else text

    def when(self, days: int) -> str:
        """"today" / "tomorrow" / "in 3 days"."""
        if days == 0:
            return self.messages["today"]
        if days == 1:
            return self.messages["tomorrow"]
        return plural(self.lang, days, self.plurals["inDays"])

    def join(self, parts: list[str]) -> str:
        return " · ".join(parts)

    def date(self, day: str) -> str:
        """"Thu 8 Oct" / "الخميس، 8 تشرين الأول" for a "YYYY-MM-DD"."""
        y, m, d = (int(x) for x in day[:10].split("-"))
        weekday = self.calendar["weekdays"][(calendar_day(y, m, d).weekday() + 1) % 7]
        month = self.calendar["months"][m - 1]
        return f"{weekday}، {d} {month}" if self.lang == "ar" else f"{weekday} {d} {month}"

    def money(self, amount: float, currency: str) -> str:
        """"$1,234.50" / "LBP 12.5": en-US with at most 2 decimals and the currency's own
        minimum, isolated in Arabic (fmtMoney)."""
        return self.ltr(format_money(amount, currency))

    def time(self, instant: datetime, tz: str, time_format: str) -> str:
        """"15:00" / "3:00 PM" / "3:00 م" in the user's time zone and time format."""
        local = instant.astimezone(ZoneInfo(dates.valid_zone(tz)))
        if time_format == "12h":
            hour = local.hour % 12 or 12
            mark = ("ص" if local.hour < 12 else "م") if self.lang == "ar" else ("AM" if local.hour < 12 else "PM")
            return f"{hour}:{local.minute:02d} {mark}"
        text = f"{local.hour:02d}:{local.minute:02d}"
        return self.ltr(text)


def format_money(amount: float, currency: str) -> str:
    """Intl.NumberFormat('en-US', {style: 'currency', maximumFractionDigits: 2}): halves
    rounded up from the number's shortest decimal form, then trailing zeros dropped down to
    the currency's own minimum (LBP 12.5, $12.50)."""
    value = float(amount)
    affix = _MONEY_AFFIXES.get(currency)
    if affix is None:  # Intl refused the code: toFixed(2) and the code after it
        return f"{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f} {currency}"
    whole, fraction = f"{Decimal(repr(abs(value))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}".split(".")
    keep = _DATA["minDigits"].get(currency, 2)
    fraction = fraction[:keep] + fraction[keep:].rstrip("0")
    return f"{'-' if value < 0 else ''}{affix['prefix']}{whole}{'.' + fraction if fraction else ''}{affix['suffix']}"


@lru_cache(maxsize=2)
def texts(lang: str) -> Texts:
    return Texts("ar" if lang == "ar" else "en")
