"""Notification titles and bodies in English and Arabic (port of vournalOS
notifications.messages.ts). The tables, and the weekday and month names as Intl writes
them, are generated from the NestJS build (tests/vournal_os/golden/notifications_golden.mjs)."""

import json
from datetime import date as calendar_day, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from app.os.common import dates
from app.os.common.i18n import fmt, plural

_DATA = json.loads((Path(__file__).parent / "data" / "notification_texts.json").read_text())
_LRI, _PDI = "⁦", "⁩"


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

    def time(self, instant: datetime, tz: str, time_format: str) -> str:
        """"15:00" / "3:00 PM" / "3:00 م" in the user's time zone and time format."""
        local = instant.astimezone(ZoneInfo(dates.valid_zone(tz)))
        if time_format == "12h":
            hour = local.hour % 12 or 12
            mark = ("ص" if local.hour < 12 else "م") if self.lang == "ar" else ("AM" if local.hour < 12 else "PM")
            return f"{hour}:{local.minute:02d} {mark}"
        text = f"{local.hour:02d}:{local.minute:02d}"
        return self.ltr(text)


@lru_cache(maxsize=2)
def texts(lang: str) -> Texts:
    return Texts("ar" if lang == "ar" else "en")
