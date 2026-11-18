"""The app's push tokens (design 2026-11-18, notifications that nudge).

One row per phone, keyed by its Expo push token. Registering moves the token to whoever is signed
in, so on a shared phone only the current account gets nudges. Sign-out removes it; account
deletion cascades. The timezone is where the phone was when last seen: nudges follow the most
recently seen phone."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth import get_current_user
from app.db import db
from app.os.common.dates import valid_zone

router = APIRouter()

TOKEN_PATTERN = r"^ExponentPushToken\[[^\]]{1,180}\]$"


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=TOKEN_PATTERN, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    appVersion: str | None = Field(default=None, max_length=32)


class ForgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=200)


@router.post("/devices/push-token", status_code=204)
async def register(body: TokenBody, current=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    fields = {"userId": current.id, "timezone": valid_zone(body.timezone), "appVersion": body.appVersion, "lastSeenAt": now}
    await db.pushdevice.upsert(where={"token": body.token}, data={"create": {"token": body.token, **fields}, "update": fields})
    return Response(status_code=204)


@router.delete("/devices/push-token", status_code=204)
async def forget(body: ForgetBody, current=Depends(get_current_user)):
    await db.pushdevice.delete_many(where={"token": body.token, "userId": current.id})
    return Response(status_code=204)


async def latest_device_tz(user_id: str) -> str | None:
    """The timezone of the person's most recently seen phone, or None without one."""
    device = await db.pushdevice.find_first(where={"userId": user_id}, order={"lastSeenAt": "desc"})
    return device.timezone if device else None
