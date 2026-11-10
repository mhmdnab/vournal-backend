"""Web sign-in and sign-up (vournal OS) with vournal accounts.

The same accounts as the iOS app (owner, 2026-11-09: accounts can be made on the web
too). Sign-up is the app's email-and-code flow; a new Google or Apple sign-in creates
the account as the app does. Linking a sign-in to an existing unconfirmed account
still happens in the app, which can email a code first. Tokens are the app's own
(the same access JWT and rotating refresh token), carried in httpOnly cookies.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import oauth, security
from app.auth import (
    _BAD_CODE,
    _finish_link,
    _issue_tokens,
    _spend_code,
    begin_email_challenge,
    end_all_sessions,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.db import db
from app.entitlements import confirm_email, effective_plan, plan_details
from app.os.common.deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
    web_user,
)
from app.os.common.http import NestJSONResponse, nest_error, nest_router
from app.rate_limit import limiter, rate_limited
from app.schemas import AuthChallengeOut, RegisterRequest, TokenResponse

router = nest_router("/auth")

ACCOUNT_EXISTS = "An account with that email already exists."
NO_VERIFIED_EMAIL = "We couldn't confirm your email. Use your email and password."
LINK_IN_APP = "Sign in with your email and password, or use this sign-in once in the vournal app."
# NestJS let a just-rotated refresh token through for this long: several dashboard
# tabs refresh with the same cookie, and only one can win the rotation.
_ROTATION_GRACE = timedelta(seconds=60)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    totpCode: str | None = Field(default=None, max_length=16)  # no 2FA on the web yet


class ProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idToken: str = Field(min_length=1, max_length=8192)
    nonce: str = Field(min_length=1, max_length=256)
    timezone: str | None = Field(default=None, max_length=64)


class WebRegisterBody(RegisterRequest):
    """The app's sign-up fields and checks; the web always takes the code step."""

    model_config = ConfigDict(extra="forbid")
    confirmEmail: bool = True


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challengeToken: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=16)


def _secure() -> bool:
    return os.environ.get("WEB_COOKIE_SECURE", "1") != "0"


def set_session_cookies(response: Response, tokens: TokenResponse) -> None:
    common = {"httponly": True, "secure": _secure(), "samesite": "lax"}
    # The access token inside expires on its own (a 401, then a refresh). The cookie lives
    # as long as the session: the dashboard's page guard only sees life_at (life_rt is
    # scoped to /api/auth), and a cookie gone after 30 minutes sent every page load to /login.
    response.set_cookie(
        ACCESS_COOKIE,
        tokens.accessToken,
        max_age=int(security._refresh_ttl().total_seconds()),
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
        **plan_details(user),
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
        # Signed up and never typed the code: a fresh one, and the dashboard asks for it.
        return _challenge(await begin_email_challenge(user))
    return await _signed_in(user, response)


@router.post("/oauth/{provider}")
async def provider_sign_in(provider: oauth.Provider, body: ProviderBody, response: Response):
    if not oauth._audiences(provider):
        raise nest_error(503, "This sign-in option isn't available yet.")
    user, identity = await oauth.identity_user(provider, body.idToken, body.nonce)
    if user is None:
        existing = await db.user.find_unique(where={"email": identity.email.lower()}) if identity.email else None
        if existing is None:
            # A new account, as the app makes it: only with an email the provider verified.
            if identity.email is None or not identity.email_verified:
                raise nest_error(409, NO_VERIFIED_EMAIL)
            user = await db.user.create(data={"email": identity.email.lower(), "timezone": body.timezone})
            await oauth.link_identity(user.id, identity.issuer, identity.subject)
            return await _signed_in(await confirm_email(db, user), response)
        # The app's own rule: the account proved this address, and the provider runs it (Gmail
        # through Google, iCloud through Apple) and says it's verified. Anything less is linked
        # in the app, which can email a code first.
        if not (existing.emailVerifiedAt is not None and identity.authoritative and identity.email_verified):
            raise nest_error(403, LINK_IN_APP, code="link_in_app")
        await oauth.link_identity(existing.id, identity.issuer, identity.subject)
        user = existing
    return await _signed_in(user, response)


def _challenge(out: AuthChallengeOut) -> dict:
    return {"challengeToken": out.challengeToken, "step": out.step, "expiresAt": out.expiresAt}


async def _unfinished_sign_up(user) -> bool:
    """An account that took the code step and never finished it, and has no other way in.
    Such an account never had a session (the code comes first), so signing up again with
    its email starts over. Accounts from app builds without the code step are in use and
    are never replaced."""
    if not user.emailConfirmRequired or user.emailVerifiedAt is not None:
        return False
    return await db.externalidentity.find_first(where={"userId": user.id}) is None


@router.post("/register")
@limiter.limit("3/hour")  # per IP, as the app's sign-up
async def register(request: Request, body: WebRegisterBody):
    if rate_limited("web-register", body.email, 3, 60 * 60):
        raise nest_error(429, "Too many attempts. Please wait and try again.")
    existing = await db.user.find_unique(where={"email": body.email})
    data = {"passwordHash": security.hash_password(body.password), "emailConfirmRequired": True}
    if existing is None:
        user = await db.user.create(data={"email": body.email, "timezone": body.timezone, **data})
    elif await _unfinished_sign_up(existing):
        user = await db.user.update(where={"id": existing.id}, data=data)
    else:
        raise nest_error(409, ACCOUNT_EXISTS)
    return _challenge(await begin_email_challenge(user))


@router.post("/register/verify")
@limiter.limit("20/15 minutes")
async def verify_code(request: Request, body: VerifyBody, response: Response):
    """The code step of sign-up (or of signing in before it was finished)."""
    token_hash = security.hash_refresh_token(body.challengeToken)
    preliminary = await db.authchallenge.find_unique(where={"tokenHash": token_hash})
    if preliminary is None:
        raise nest_error(400, _BAD_CODE)
    challenge = await _spend_code(
        preliminary.userId,
        lambda tx: tx.authchallenge.find_unique(where={"tokenHash": token_hash}),
        body.code,
    )
    user = None if challenge is None else await db.user.find_unique(where={"id": challenge.userId})
    if user is None:
        raise nest_error(400, _BAD_CODE)
    if challenge.purpose == "link_identity" and challenge.pendingIssuer:
        await _finish_link(user, challenge)
    return await _signed_in(await confirm_email(db, user), response)


def _signed_out(message: str) -> NestJSONResponse:
    """A 401 that also drops the cookies: the dashboard sends anyone holding one back
    from /login to /dashboard, so a dead cookie would bounce them between the two."""
    failed = NestJSONResponse(
        {"statusCode": 401, "message": message, "error": "Unauthorized"}, status_code=401
    )
    clear_session_cookies(failed)
    return failed


async def _rotated_by_another_tab(raw: str) -> bool:
    """True when this refresh token was just spent by another tab of the same
    session: its account has a fresh live token of the same generation. The app's
    own /auth/refresh would treat it as theft and end every session, the phone's too."""
    stored = await db.refreshtoken.find_unique(where={"tokenHash": security.hash_refresh_token(raw)})
    if stored is None or not stored.revoked or stored.expiresAt < datetime.now(timezone.utc):
        return False
    owner = await db.user.find_unique(where={"id": stored.userId})
    if owner is None or stored.generation != owner.sessionGeneration:
        return False
    fresh = await db.refreshtoken.find_first(
        where={
            "userId": stored.userId,
            "revoked": False,
            "generation": owner.sessionGeneration,
            "createdAt": {"gte": datetime.now(timezone.utc) - _ROTATION_GRACE},
        }
    )
    return fresh is not None


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        return _signed_out("Not authenticated")
    if await _rotated_by_another_tab(raw):
        # The other tab already holds the new cookies; nothing is issued here, so a
        # stolen token gains nothing from this answer.
        return {"ok": True}
    try:
        tokens = await rotate_refresh_token(raw)
    except HTTPException as exc:
        return _signed_out(str(exc.detail))
    set_session_cookies(response, tokens)
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
