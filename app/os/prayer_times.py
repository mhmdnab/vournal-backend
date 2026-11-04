"""/api/prayer-times (port of vournalOS PrayerController): the place prayer times are
worked out for, and the times themselves. The maths is in common/prayer.py."""

import math

from fastapi import Depends, Request

from app.os.common import dates, prayer
from app.os.common.body import RuleBody, choice, nested, number, text
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.query import _query_int, unknown_params
from app.os.profile import ensure_os_profile

router = nest_router("/prayer-times")

_ADJUSTMENT_RULES = tuple(
    (p, False, False, number(-prayer.MAX_ADJUSTMENT, prayer.MAX_ADJUSTMENT)) for p in prayer.PRAYERS
)


class SettingsBody(RuleBody):
    RULES = (
        ("cityId", True, False, text(60)),
        ("lat", False, False, number(-90, 90, math.inf)),
        ("lng", False, False, number(-180, 180, math.inf)),
        ("method", False, False, choice(prayer.PRAYER_METHODS)),
        ("madhab", False, False, choice(("shafi", "hanafi"))),
        ("adjustments", True, False, nested(_ADJUSTMENT_RULES)),  # left out: keep the current ones
    )
    cityId: object = None
    lat: object = None
    lng: object = None
    method: object = None
    madhab: object = None
    adjustments: object = None


async def _times(user, date: str | None = None, days: int | None = None) -> dict:
    """{settings, days: [{date, times}]}; settings null (and no days) when no place is chosen."""
    s = await prayer.settings(user.id)
    prefs = await ensure_os_profile(user)
    first = dates.parse_date_only(date) or dates.today_date_only(prefs.timezone)
    out = []
    if s is not None:
        for i in range(days or 1):
            day = dates.format_date_only(dates.add_days_date_only(first, i))
            out.append({"date": day, "times": prayer.prayer_times(s, day)})
    return {"settings": s, "days": out}


@router.get("")
async def times(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("date", "days"))
    date = request.query_params.get("date")
    if date is not None and not dates.is_iso_date_string(date):
        errors.append("date must be a valid ISO 8601 date string")
    days = _query_int(request, "days", errors, maximum=31, minimum=1)
    if errors:
        raise nest_error(400, errors)
    return await _times(user, date, days)


@router.put("/settings")
async def save(body: SettingsBody, user=Depends(plus_user)):
    current = await prayer.settings(user.id)
    await prayer.save_settings(user.id, {
        "cityId": body.cityId,
        "lat": body.lat,
        "lng": body.lng,
        "method": body.method,
        "madhab": body.madhab,
        "adjustments": body.adjustments or (current or {}).get("adjustments") or prayer.NO_ADJUSTMENTS,
    })
    return await _times(user)


@router.delete("/settings")
async def clear(user=Depends(plus_user)):
    await prayer.clear_settings(user.id)
    return {"settings": None, "days": []}
