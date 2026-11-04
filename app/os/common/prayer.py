"""Prayer times for the user's chosen place, and times relative to a prayer ("10 minutes
after Maghrib") on tasks, events and reminders (port of vournalOS prayer.util and
PrayerService).

Without prayer settings every anchor resolves to None and the stored times are used as
they are. The maths is `adhan` (common/adhan.py); "LebanonAICP" is the official Lebanese
calendar (data/aicp_beirut.json), carried to other places by their difference to Beirut.
"""

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from prisma import Json
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.db import db
from app.os.common import adhan
from app.os.common.dbnull import set_db_null
from app.os.common.http import nest_error

PRAYERS = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
PrayerName = Literal["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]
PRAYER_METHODS = (  # in prayer.util's order (its validation message lists them)
    "MuslimWorldLeague", "Egyptian", "Karachi", "UmmAlQura", "Dubai", "Qatar", "Kuwait", "Turkey", "Tehran",
    "Singapore", "NorthAmerica", "MoonsightingCommittee", "LebanonAICP",
)
MAX_ADJUSTMENT = 30
NO_ADJUSTMENTS = {p: 0 for p in PRAYERS}
SETTINGS_MISSING = "Choose your city for prayer times first (Settings → Preferences)"

Resolver = Callable[[str, Any], "tuple[datetime, datetime | None] | None"]


class TimePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prayer: PrayerName
    offset: StrictInt = Field(ge=-240, le=240)  # minutes after the prayer (negative = before)


class TimeAnchor(BaseModel):
    """At / before / after a prayer, or (events) between two prayers (TimeAnchorDto)."""

    model_config = ConfigDict(extra="forbid")
    start: TimePoint
    end: TimePoint | None = None


# ----- the maths (prayer.util) ---------------------------------------------------------


def _js_integer(value) -> bool:
    """Number.isInteger: a JS number (not a bool) with no fraction."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value == int(value)


def _calendar_day(day: str) -> date:
    y, m, d = (int(part) for part in day.split("-"))
    return date(y, m, 1) + timedelta(days=d - 1)  # new Date(y, m - 1, d) rolls over


def _calculate(lat: float, lng: float, method: str, madhab: str, day: str) -> dict[str, float]:
    times = adhan.prayer_times(lat, lng, method, madhab, _calendar_day(day))
    return {p: times[p] for p in PRAYERS}


_BEIRUT = (33.8938, 35.5018)
_BEIRUT_ZONE = ZoneInfo("Asia/Beirut")
_calendar_offsets: dict[str, dict[str, float]] | None = None


def _beirut_instant(day: str, hhmm: str) -> float:
    """"YYYY-MM-DD" + "HH:mm" on Beirut's clock → epoch ms (UTC+2 in winter, +3 in summer)."""
    y, m, d = (int(part) for part in day.split("-"))
    h, mi = (int(part) for part in hhmm.split(":"))
    midnight = datetime(y, m, d, tzinfo=timezone.utc)
    for offset in (2, 3):
        moment = midnight + timedelta(hours=h - offset, minutes=mi)
        if moment.astimezone(_BEIRUT_ZONE).strftime("%H:%M") == hhmm:
            return moment.timestamp() * 1000
    return (midnight + timedelta(hours=h - 2, minutes=mi)).timestamp() * 1000


def _lebanon_calendar(lat: float, lng: float, madhab: str, day: str) -> dict[str, float]:
    """Official calendar time = calculated time + that day's offset (calendar − calculation,
    for Beirut). Other places keep their own difference to Beirut; other years reuse the
    same day's offset."""
    global _calendar_offsets
    if _calendar_offsets is None:
        table = json.loads((Path(__file__).parents[1] / "data" / "aicp_beirut.json").read_text())
        offsets = {}
        for mmdd, line in table["days"].items():
            calendar_day = f"{table['year']}-{mmdd}"
            calc = _calculate(*_BEIRUT, "MuslimWorldLeague", "shafi", calendar_day)
            clock = line.split(" ")
            offsets[mmdd] = {p: _beirut_instant(calendar_day, clock[i]) - calc[p] for i, p in enumerate(PRAYERS)}
        _calendar_offsets = offsets
    mmdd = "02-28" if day[5:] == "02-29" else day[5:]
    off = _calendar_offsets[mmdd]
    calc = _calculate(lat, lng, "MuslimWorldLeague", madhab, day)
    return {p: calc[p] + off[p] for p in PRAYERS}


def _instant(ms: float) -> datetime | None:
    """A JS Date as a datetime; an invalid one (no such time that day) as None."""
    return None if ms != ms else datetime.fromtimestamp(ms / 1000, timezone.utc)


def _day_ms(settings: dict, day: str) -> dict[str, float]:
    adj = settings.get("adjustments") or NO_ADJUSTMENTS
    lat, lng, method, madhab = settings["lat"], settings["lng"], settings["method"], settings["madhab"]
    t = _lebanon_calendar(lat, lng, madhab, day) if method == "LebanonAICP" else _calculate(lat, lng, method, madhab, day)
    return {p: t[p] + adj[p] * 60_000 for p in PRAYERS}


def prayer_times(settings: dict, day: str) -> dict[str, datetime | None]:
    """Prayer instants for a calendar day ("YYYY-MM-DD") at the place in `settings`."""
    return {p: _instant(ms) for p, ms in _day_ms(settings, day).items()}


def resolve_anchor(settings: dict, day: str, anchor: dict) -> tuple[datetime | None, datetime | None]:
    """Start / end instants of an anchor on a day (end None when it has no end). "Between
    Isha and Fajr" ends the next morning."""
    times = _day_ms(settings, day)

    def point(t: dict, p: dict) -> float:
        return t[p["prayer"]] + p["offset"] * 60_000

    start = point(times, anchor["start"])
    end_point = anchor.get("end")
    end = point(times, end_point) if end_point else None
    if end is not None and end <= start:
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        end = point(_day_ms(settings, tomorrow), end_point)
    return _instant(start), None if end is None else _instant(end)


def _is_point(value) -> bool:
    return (
        isinstance(value, dict)
        and value.get("prayer") in PRAYERS
        and _js_integer(value.get("offset"))
        and abs(value["offset"]) <= 240
    )


def read_anchor(raw) -> dict | None:
    """A stored anchor in its canonical shape; anything malformed is "no anchor"."""
    if not isinstance(raw, dict) or not _is_point(raw.get("start")):
        return None
    start, end = raw["start"], raw.get("end")
    return {
        "start": {"prayer": start["prayer"], "offset": start["offset"]},
        "end": {"prayer": end["prayer"], "offset": end["offset"]} if _is_point(end) else None,
    }


def read_adjustments(raw) -> dict:
    """Stored adjustments; anything missing or out of range counts as 0."""
    source = raw if isinstance(raw, dict) else {}

    def one(value):
        return value if _js_integer(value) and abs(value) <= MAX_ADJUSTMENT else 0

    return {p: one(source.get(p)) for p in PRAYERS}


def _js_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_prayer_settings(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    lat, lng = raw.get("lat"), raw.get("lng")
    if not _js_number(lat) or not _js_number(lng) or abs(lat) > 90 or abs(lng) > 180:
        return None
    return {
        "cityId": raw["cityId"] if isinstance(raw.get("cityId"), str) else None,
        "lat": lat,
        "lng": lng,
        "method": raw["method"] if raw.get("method") in PRAYER_METHODS else "MuslimWorldLeague",
        "madhab": "hanafi" if raw.get("madhab") == "hanafi" else "shafi",
        "adjustments": read_adjustments(raw.get("adjustments")),
    }


# ----- the user's place (PrayerService) ------------------------------------------------


async def settings(user_id: str) -> dict | None:
    row = await db.usersettings.find_unique(where={"userId": user_id})
    return read_prayer_settings(row.prayerSettings if row else None)


async def save_settings(user_id: str, s: dict) -> dict:
    value = {
        "cityId": s["cityId"], "lat": s["lat"], "lng": s["lng"], "method": s["method"], "madhab": s["madhab"],
        "adjustments": read_adjustments(s["adjustments"]),
    }
    await db.usersettings.upsert(
        where={"userId": user_id},
        data={"update": {"prayerSettings": Json(value)}, "create": {"userId": user_id, "prayerSettings": Json(value)}},
    )
    return value


async def clear_settings(user_id: str) -> None:
    row = await db.usersettings.find_unique(where={"userId": user_id})
    if row is not None:
        await set_db_null(db, "UserSettings", row.id, ["prayerSettings"])


async def resolver(user_id: str) -> Resolver:
    """Loads the user's settings once; resolves an anchor on any local day."""
    s = await settings(user_id)

    def resolve(day: str, raw):
        if s is None:
            return None
        anchor = read_anchor(raw)
        return resolve_anchor(s, day, anchor) if anchor else None

    return resolve


async def resolve_or_fail(user_id: str, day: str, anchor: dict) -> tuple[datetime, datetime | None]:
    """The anchored moment, or 400 when no city is set (so the dashboard can ask for one)."""
    s = await settings(user_id)
    if s is None:
        raise nest_error(400, SETTINGS_MISSING)
    return resolve_anchor(s, day, anchor)
