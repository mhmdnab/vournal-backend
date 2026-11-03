"""/api/users/me (port of vournalOS UsersController): the account as the web sees it.

Name and avatar colour live on the OS settings row; the app's User has neither.
Free accounts may read and delete their account (spec: Plus unlocks the web).
"""

import re

from fastapi import Depends, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import security
from app.auth import delete_account
from app.db import db
from app.os.auth import clear_session_cookies, web_user_out
from app.os.common.deps import plus_user, web_user
from app.os.common.http import nest_error, nest_router
from app.os.profile import ensure_os_profile

router = nest_router("/users")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class UpdateMeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    avatarColor: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not 1 <= len(v) <= 80:
            raise ValueError("name must be longer than or equal to 1 and shorter than or equal to 80 characters")
        return v

    @field_validator("avatarColor")
    @classmethod
    def _color(cls, v):
        if v is not None and not _HEX.match(v):
            raise ValueError("avatarColor must be a hex color like #6366f1")
        return v


class DeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # An account with a password confirms with it; one without (Apple/Google only)
    # types DELETE, as in the app.
    password: str | None = Field(default=None, max_length=128)
    confirmWord: str | None = Field(default=None, max_length=16)


@router.get("/me")
async def get_me(user=Depends(web_user)):
    return web_user_out(user, await ensure_os_profile(user))


@router.patch("/me")
async def update_me(body: UpdateMeBody, user=Depends(plus_user)):
    await ensure_os_profile(user)
    data = {}
    if "name" in body.model_fields_set and body.name is not None:
        data["name"] = body.name
    if "avatarColor" in body.model_fields_set:
        data["avatarColor"] = body.avatarColor
    settings = await db.usersettings.update(where={"userId": user.id}, data=data)
    return web_user_out(user, settings)


@router.delete("/me")
async def delete_me(body: DeleteBody, response: Response, user=Depends(web_user)):
    if user.passwordHash is None:
        if body.confirmWord != "DELETE":
            raise nest_error(403, "Type DELETE to confirm.")
    elif not body.password or not security.verify_password(body.password, user.passwordHash):
        raise nest_error(403, "Password is incorrect.")
    await delete_account(user)
    clear_session_cookies(response)
    return {"ok": True}
