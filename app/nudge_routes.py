"""The app's nudge settings (design 2026-11-18, notifications that nudge).

Free and Plus alike, so on root routes rather than /api. The switches live in
UserSettings.notificationPrefs beside the website's keys (a patch keeps those); the morning time
is the app's own, separate from the website's dailyDigestTime. aiWriting is the person's agreement
to AI-written nudges, given in the app's explainer: nudgeAiConsentAt."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from prisma import Json
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from app.auth import get_current_user
from app.db import db
from app.entitlements import FREE, effective_plan
from app.os.profile import ensure_os_profile
from app.os.settings import MORNING_EARLIEST, MORNING_LATEST, normalize_prefs, valid_morning_time

router = APIRouter()

# the API's names -> the notificationPrefs keys
KEYS = {"nudges": "nudges", "morningTime": "nudgeMorningTime", "plan": "nudgePlan",
        "checkIns": "nudgeCheckIns", "suggestions": "nudgeSuggestions"}


class PrefsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nudges: StrictBool | None = None
    morningTime: str | None = None
    plan: StrictBool | None = None
    checkIns: StrictBool | None = None
    suggestions: StrictBool | None = None
    aiWriting: StrictBool | None = None

    @field_validator("morningTime")
    @classmethod
    def _morning(cls, v):
        if v is not None and not valid_morning_time(v):
            raise ValueError(f"morningTime must be HH:mm between {MORNING_EARLIEST} and {MORNING_LATEST}")
        return v


def view(user, settings) -> dict:
    prefs = normalize_prefs(settings.notificationPrefs)
    return {**{name: prefs[key] for name, key in KEYS.items()},
            "aiWriting": settings.nudgeAiConsentAt is not None, "plus": effective_plan(user) != FREE}


@router.get("/nudges/prefs")
async def get_prefs(current=Depends(get_current_user)):
    return view(current, await ensure_os_profile(current))


@router.patch("/nudges/prefs")
async def patch_prefs(body: PrefsPatch, current=Depends(get_current_user)):
    settings = await ensure_os_profile(current)
    given = body.model_dump(exclude_unset=True, exclude_none=True)
    data: dict = {}
    changes = {KEYS[name]: value for name, value in given.items() if name in KEYS}
    if changes:
        data["notificationPrefs"] = Json({**normalize_prefs(settings.notificationPrefs), **changes})
    if given.get("aiWriting") is True and settings.nudgeAiConsentAt is None:
        data["nudgeAiConsentAt"] = datetime.now(timezone.utc)
    elif given.get("aiWriting") is False:
        data["nudgeAiConsentAt"] = None
    if data:
        settings = await db.usersettings.update(where={"userId": current.id}, data=data)
    return view(current, settings)
