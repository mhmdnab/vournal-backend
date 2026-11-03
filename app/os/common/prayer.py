"""Times relative to a prayer ("10 minutes after Maghrib") on tasks, events and reminders.

The interface vournalOS's PrayerService gave the other sections. Prayer times need
the user's city, which the web sets in the prayer section (a later phase, with the
prayer maths). Until then no account can have prayer settings, so every anchor
resolves as "no city set", exactly as NestJS behaves for a user without one.
"""

import logging
from datetime import datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.db import db
from app.os.common.http import nest_error

log = logging.getLogger("vournal.os")

PRAYERS = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
PrayerName = Literal["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]
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


def _is_point(value) -> bool:
    return isinstance(value, dict) and value.get("prayer") in PRAYERS and isinstance(value.get("offset"), (int, float))


def read_anchor(raw) -> dict | None:
    """A stored anchor in its canonical shape, or None (readAnchor)."""
    if not isinstance(raw, dict) or not _is_point(raw.get("start")):
        return None
    start, end = raw["start"], raw.get("end")
    return {
        "start": {"prayer": start["prayer"], "offset": start["offset"]},
        "end": {"prayer": end["prayer"], "offset": end["offset"]} if _is_point(end) else None,
    }


async def _has_settings(user_id: str) -> bool:
    row = await db.usersettings.find_unique(where={"userId": user_id})
    if row is not None and row.prayerSettings:
        # Only the prayer section (not ported yet) writes this; until it is, it's ignored.
        log.warning("Prayer settings for %s ignored until the prayer section is ported", user_id)
    return False


async def resolver(user_id: str) -> Resolver:
    """Loads the user's settings once; resolves an anchor on any local day. With no
    city set, every anchor resolves to None and the stored times are used as they are."""
    await _has_settings(user_id)
    return lambda day, raw: None


async def resolve_or_fail(user_id: str, day: str, anchor: dict) -> tuple[datetime, datetime | None]:
    """The anchored moment, or 400 when no city is set (so the dashboard can ask for one)."""
    await _has_settings(user_id)
    raise nest_error(400, SETTINGS_MISSING)
