"""Web sign-in (vournal OS) with vournal accounts.

Accounts are only ever made in the iOS app, so these routes sign existing
accounts in and never create or link one. Tokens are the app's own (the same
access JWT and rotating refresh token), carried in httpOnly cookies.
"""

import os

from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import oauth, security
from app.auth import _issue_tokens, end_all_sessions, revoke_refresh_token, rotate_refresh_token
from app.db import db
from app.entitlements import effective_plan
from app.os.common.deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
    web_user,
)
from app.os.common.http import nest_error, nest_router
from app.rate_limit import rate_limited
from app.schemas import TokenResponse

router = nest_router("/auth")

CREATE_IN_APP = "Create your account in the vournal app."
CONFIRM_IN_APP = "Confirm your email in the vournal app first."


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    totpCode: str | None = Field(default=None, max_length=16)  # no 2FA on the web yet


class ProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idToken: str = Field(min_length=1, max_length=8192)
    nonce: str = Field(min_length=1, max_length=256)


def _secure() -> bool:
    return os.environ.get("WEB_COOKIE_SECURE", "1") != "0"


def set_session_cookies(response: Response, tokens: TokenResponse) -> None:
    common = {"httponly": True, "secure": _secure(), "samesite": "lax"}
    response.set_cookie(
        ACCESS_COOKIE,
        tokens.accessToken,
        max_age=int(security._access_ttl().total_seconds()),
        path="/",
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refreshToken,
        max_age=int(security._refresh_ttl().total_seconds()),
        path=REFRESH_COOKIE_PATH,
        **common,
    )


def clear_session_cookies(response: Response) -> None:
    common = {"httponly": True, "secure": _secure(), "samesite": "lax"}
    response.delete_cookie(ACCESS_COOKIE, path="/", **common)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, **common)


def web_user_out(user, settings) -> dict:
    """vournalOS's User shape, from the app's account plus the OS settings row."""
    return {
        "id": user.id,
        "email": user.email,
        "name": (settings.name if settings is not None else None) or "",
        "avatarColor": settings.avatarColor if settings is not None else None,
        "twoFactorEnabled": False,  # app accounts have no 2FA sign-in yet (spec)
        "lastLoginAt": None,  # the app doesn't track it
        "createdAt": user.createdAt,
        "plan": effective_plan(user),
    }


async def _settings_for(user):
    from app.os.profile import ensure_os_profile

    return await ensure_os_profile(user)


async def _signed_in(user, response: Response) -> dict:
    set_session_cookies(response, await _issue_tokens(user.id, user.sessionGeneration))
    return {"user": web_user_out(user, await _settings_for(user))}


@router.post("/login")
async def login(body: LoginBody, response: Response):
    email = body.email.strip().lower()
    if rate_limited("login", email, 5, 15 * 60):
        raise nest_error(429, "Too many attempts. Please wait and try again.")
    user = await db.user.find_unique(where={"email": email})
    # One answer for an unknown email, a wrong password and an account with no
    # password (Apple/Google only): never reveal which.
    if user is None or not security.verify_password(body.password, user.passwordHash):
        raise nest_error(401, "Invalid email or password")
    if user.emailConfirmRequired and user.emailVerifiedAt is None:
        raise nest_error(403, CONFIRM_IN_APP, code="confirm_in_app")
    return await _signed_in(user, response)


@router.post("/oauth/{provider}")
async def provider_sign_in(provider: oauth.Provider, body: ProviderBody, response: Response):
    if not oauth._audiences(provider):
        raise nest_error(503, "This sign-in option isn't available yet.")
    user = await oauth.identity_user(provider, body.idToken, body.nonce)
    if user is None:
        raise nest_error(403, CREATE_IN_APP, code="create_in_app")
    return await _signed_in(user, response)


@router.post("/register")
async def register():
    raise nest_error(403, CREATE_IN_APP, code="create_in_app")


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise nest_error(401, "Not authenticated")
    set_session_cookies(response, await rotate_refresh_token(raw))
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        await revoke_refresh_token(raw)
    clear_session_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(response: Response, user=Depends(web_user)):
    await end_all_sessions(user.id)
    clear_session_cookies(response)
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(web_user)):
    from app.os.settings import settings_view

    settings = await _settings_for(user)
    return {"user": web_user_out(user, settings), "settings": settings_view(settings)}
