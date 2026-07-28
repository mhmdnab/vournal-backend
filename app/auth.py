"""Authentication: the /auth routes and the get_current_user dependency.

Token model:
  - register / login  -> issue an access JWT + a fresh opaque refresh token
  - refresh           -> verify the refresh token against its stored hash,
                         ROTATE it (revoke old, issue new), return a new pair
  - logout            -> revoke the presented refresh token

Ownership/enforcement live in main.py; this module only proves *who* you are.
Nothing here logs passwords or tokens.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import email, security
from app.db import db
from app.entitlements import effective_plan
from app.quota import usage_snapshot
from app.rate_limit import limiter, rate_limited
from app.storage import delete_audio
from app.schemas import (
    DeleteAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateMeRequest,
    UsageOut,
    UserOut,
)

# Password-reset token lifetime, and the web app base used to build reset links.
RESET_TTL_MIN = int(os.environ.get("RESET_TOKEN_TTL_MIN", "30"))


def _web_base() -> str:
    return (os.environ.get("WEB_BASE_URL") or "http://localhost:3000").rstrip("/")

router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


async def get_current_user(authorization: Optional[str] = Header(default=None)):
    """Resolve the bearer access token to a User, or raise 401.

    Applied to every data endpoint. Any failure — missing header, malformed
    token, expired/invalid signature, wrong token type, or a user that no longer
    exists — is a flat 401 with no detail about which.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not authenticated", headers=_UNAUTHENTICATED)

    token = authorization[len("bearer ") :].strip()
    user_id = security.decode_access_token(token)
    if not user_id:
        raise HTTPException(401, "Invalid or expired token", headers=_UNAUTHENTICATED)

    user = await db.user.find_unique(where={"id": user_id})
    if user is None:
        raise HTTPException(401, "Invalid or expired token", headers=_UNAUTHENTICATED)
    return user


async def _issue_tokens(user_id: str) -> TokenResponse:
    """Mint an access JWT and persist a new (hashed) refresh token."""
    raw_refresh = security.generate_refresh_token()
    await db.refreshtoken.create(
        data={
            "userId": user_id,
            "tokenHash": security.hash_refresh_token(raw_refresh),
            "expiresAt": security.refresh_expiry(),
        }
    )
    return TokenResponse(
        accessToken=security.create_access_token(user_id),
        refreshToken=raw_refresh,
    )


@router.post("/register", response_model=TokenResponse)
@limiter.limit("3/hour")  # per IP — signups are rare; this stops account-spam
async def register(request: Request, body: RegisterRequest):
    # RegisterRequest already normalized/validated email + password length.
    existing = await db.user.find_unique(where={"email": body.email})
    if existing is not None:
        # Register unavoidably reveals existence; email verification (a later
        # step) is where enumeration gets mitigated.
        raise HTTPException(409, "An account with that email already exists.")

    user = await db.user.create(
        data={
            "email": body.email,
            "passwordHash": security.hash_password(body.password),
            "timezone": body.timezone,
        }
    )
    return await _issue_tokens(user.id)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/15 minutes")  # per IP
async def login(request: Request, body: LoginRequest):
    email = body.email.strip().lower()
    # ...and per email, so an attacker can't spread a single-account brute force
    # across many IPs.
    if rate_limited("login", email, 5, 15 * 60):
        raise HTTPException(429, "Too many attempts. Please wait and try again.")
    user = await db.user.find_unique(where={"email": email})
    # One generic error whether the email is unknown, the password is wrong, or
    # the account has no password (OAuth-only / legacy) — never reveal which.
    if user is None or not security.verify_password(body.password, user.passwordHash):
        raise HTTPException(401, "Invalid email or password.")
    return await _issue_tokens(user.id)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    token_hash = security.hash_refresh_token(body.refreshToken)
    stored = await db.refreshtoken.find_unique(where={"tokenHash": token_hash})

    now = datetime.now(timezone.utc)
    if stored is None or stored.expiresAt < now:
        raise HTTPException(401, "Invalid or expired refresh token.")

    # Reuse detection: a token is revoked as soon as it's rotated away (or on
    # logout). Presenting a REVOKED token means either a replay of a stolen token
    # or the two legitimately-rotated halves racing — either way, treat it as
    # compromise and revoke the whole family, so a stolen 60-day token can't be a
    # 60-day account takeover.
    if stored.revoked:
        await db.refreshtoken.update_many(
            where={"userId": stored.userId, "revoked": False}, data={"revoked": True}
        )
        raise HTTPException(401, "Invalid or expired refresh token.")

    # Rotate: the presented token is now spent (revoked); a fresh pair is issued.
    await db.refreshtoken.update(where={"id": stored.id}, data={"revoked": True})
    return await _issue_tokens(stored.userId)


@router.post("/logout")
async def logout(body: LogoutRequest):
    """Revoke the presented refresh token. Idempotent and always 200 — it must
    not reveal whether the token existed."""
    token_hash = security.hash_refresh_token(body.refreshToken)
    stored = await db.refreshtoken.find_unique(where={"tokenHash": token_hash})
    if stored is not None and not stored.revoked:
        await db.refreshtoken.update(where={"id": stored.id}, data={"revoked": True})
    return {"ok": True}


def _user_out(user) -> UserOut:
    """Serialize a user, resolving the EFFECTIVE plan and hiding a stale expiry
    once the entitlement has lapsed to free."""
    plan = effective_plan(user)
    return UserOut(
        id=user.id,
        email=user.email,
        timezone=user.timezone,
        plan=plan,
        planExpiresAt=user.planExpiresAt if plan != "free" else None,
        usage=UsageOut(**usage_snapshot(user)),
        createdAt=user.createdAt,
    )


@router.get("/me", response_model=UserOut)
async def me(current=Depends(get_current_user)):
    return _user_out(current)


@router.patch("/me", response_model=UserOut)
async def update_me(body: UpdateMeRequest, current=Depends(get_current_user)):
    """Update mutable profile fields (timezone only). The client sends the device
    timezone here when it changes, so times keep composing against the right
    clock without any UI."""
    updated = await db.user.update(
        where={"id": current.id}, data={"timezone": body.timezone}
    )
    return _user_out(updated)


@router.delete("/me", status_code=204)
async def delete_me(body: DeleteAccountRequest, current=Depends(get_current_user)):
    """Permanently delete the account — required by App Store review, and the
    right default for a privacy product: no soft-delete, "deleted" means deleted.

    Re-confirm with the password (deletion is irreversible). ORDER MATTERS: the
    recordings live in external storage the DB cascade knows nothing about, so we
    enumerate and delete every audio object FIRST (while the rows still tell us the
    keys), THEN delete the user — whose cascade removes entries, todos, topics,
    people, and refresh tokens. Getting this backwards orphans every recording in
    the bucket forever.
    """
    if not security.verify_password(body.password, current.passwordHash):
        raise HTTPException(403, "Password is incorrect.")

    # 1) External storage first — best-effort per object (a missing one is fine).
    entries = await db.entry.find_many(where={"userId": current.id})
    for entry in entries:
        if entry.audioUrl:
            await delete_audio(entry.audioUrl)

    # 2) Then the account. Cascade (schema onDelete) removes everything else.
    await db.user.delete(where={"id": current.id})


@router.post("/forgot-password")
@limiter.limit("5/hour")  # per IP — a coarse flood guard on top of the per-email one
async def forgot_password(request: Request, body: ForgotPasswordRequest):
    """Start a password reset. ALWAYS returns 200, whether or not the email is
    registered — never reveal which addresses have accounts. If it exists, a
    single-use, short-lived token is stored (hash only) and a reset link emailed."""
    email_addr = body.email
    # Per-email rate limit (slowapi can't key on the body). Over the limit we
    # still return 200 and simply don't send again — no enumeration signal.
    if not rate_limited("forgot-password", email_addr, 3, 3600):
        user = await db.user.find_unique(where={"email": email_addr})
        if user is not None:
            raw = secrets.token_urlsafe(32)
            await db.passwordresettoken.create(
                data={
                    "userId": user.id,
                    "tokenHash": security.hash_refresh_token(raw),
                    "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=RESET_TTL_MIN),
                }
            )
            reset_url = f"{_web_base()}/reset-password?token={raw}"
            await email.send_password_reset(user.email, reset_url, RESET_TTL_MIN)
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    """Complete a reset: verify the token (hash + unused + unexpired), set the new
    password, mark the token used, and REVOKE ALL of the user's refresh tokens — a
    reset must log out every existing session."""
    token_hash = security.hash_refresh_token(body.token)
    stored = await db.passwordresettoken.find_unique(where={"tokenHash": token_hash})

    now = datetime.now(timezone.utc)
    if stored is None or stored.usedAt is not None or stored.expiresAt < now:
        raise HTTPException(400, "This reset link is invalid or has expired.")

    await db.user.update(
        where={"id": stored.userId},
        data={"passwordHash": security.hash_password(body.newPassword)},
    )
    await db.passwordresettoken.update(where={"id": stored.id}, data={"usedAt": now})
    # Every existing session is now stale — revoke all refresh tokens.
    await db.refreshtoken.update_many(
        where={"userId": stored.userId, "revoked": False}, data={"revoked": True}
    )
    return {"ok": True}
