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
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import email, security
from app.db import db
from app.entitlements import confirm_email, effective_plan
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
    AuthChallengeOut,
    ConfirmEmailRequest,
    VerifyChallengeRequest,
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
    user = await user_for_access_token(token)
    if user is None:
        raise HTTPException(401, "Invalid or expired token", headers=_UNAUTHENTICATED)
    return user


async def user_for_access_token(token: str):
    """The user an access token belongs to, or None when it's malformed, expired, or
    from before the account's sessions were ended. Shared with the web (vournal OS)."""
    decoded = security.decode_access_token(token)
    if not decoded:
        return None
    user_id, generation = decoded
    user = await db.user.find_unique(where={"id": user_id})
    if user is None or user.sessionGeneration != generation:
        return None
    return user


async def end_all_sessions(user_id: str) -> None:
    """Sign the account out everywhere: every access token stops working at once
    (generation) and every refresh token is spent."""
    await db.user.update(where={"id": user_id}, data={"sessionGeneration": {"increment": 1}})
    await db.refreshtoken.update_many(
        where={"userId": user_id, "revoked": False}, data={"revoked": True}
    )


async def _issue_tokens(user_id: str, generation: int | None = None) -> TokenResponse:
    """Mint an access JWT and persist a new (hashed) refresh token. Callers that read
    the account before doing slow work pass the generation they saw, so a session
    change in between kills what they're about to issue."""
    if generation is None:
        user = await db.user.find_unique(where={"id": user_id})
        generation = user.sessionGeneration if user else 0
    raw_refresh = security.generate_refresh_token()
    await db.refreshtoken.create(
        data={
            "userId": user_id,
            "tokenHash": security.hash_refresh_token(raw_refresh),
            "expiresAt": security.refresh_expiry(),
            "generation": generation,
        }
    )
    return TokenResponse(
        accessToken=security.create_access_token(user_id, generation),
        refreshToken=raw_refresh,
    )


_BAD_CODE = "Invalid or expired code. After five attempts, sign in again."
# Per account, in the database, so they hold across IPs and deploys.
_CODES_PER_HOUR = 6
_CODES_PER_DAY = 15
_WRONG_GUESSES_PER_DAY = 20
_CODE_PURPOSES = ["verify_email", "link_identity"]
_CONFIRM_LINK_DAYS = 7
_CLEARS_PASSWORD_NOTE = (
    "Entering this code connects your sign-in to this vournal account and removes its "
    'old password. You can set a new one any time with "Forgot password".'
)


def _code_digest(user_id: str, code: str) -> str:
    from app import mfa

    return mfa.digest(f"{user_id}:{code}", "email")


async def begin_email_challenge(
    user,
    purpose: str = "verify_email",
    issuer: str | None = None,
    subject: str | None = None,
    sealed_apple_token: str | None = None,
) -> AuthChallengeOut:
    """Email a 6-digit code. Only the latest challenge per account is valid."""
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    clears_password = (
        purpose == "link_identity"
        and user.emailVerifiedAt is None
        and user.passwordHash is not None
    )
    async with db.tx() as tx:
        await tx.query_raw('SELECT id FROM "User" WHERE id = $1 FOR UPDATE', user.id)
        for window, limit in ((timedelta(hours=1), _CODES_PER_HOUR), (timedelta(days=1), _CODES_PER_DAY)):
            sent = await tx.authchallenge.count(
                where={"userId": user.id, "purpose": {"in": _CODE_PURPOSES}, "createdAt": {"gt": now - window}}
            )
            if sent >= limit:
                raise HTTPException(429, "Too many codes. Please wait and try again.")
        await tx.authchallenge.update_many(
            where={"userId": user.id, "usedAt": None, "purpose": {"in": _CODE_PURPOSES}},
            data={"usedAt": now},
        )
        await tx.authchallenge.create(
            data={
                "userId": user.id,
                "tokenHash": security.hash_refresh_token(raw),
                "purpose": purpose,
                "expiresAt": now + timedelta(minutes=10),
                "codeHash": _code_digest(user.id, code),
                "pendingSecret": sealed_apple_token,
                **({"pendingIssuer": issuer, "pendingSubject": subject} if issuer else {}),
            }
        )
    # The local runner replaces delivery with a private local mailbox file.
    note = _CLEARS_PASSWORD_NOTE if clears_password else None
    if not await email.send_verification_code(user.email, code, note):
        raise HTTPException(503, "Verification email could not be sent. Sign in to retry.")
    return AuthChallengeOut(
        challengeToken=raw,
        step=purpose,
        expiresAt=now + timedelta(minutes=10),
        clearsPassword=clears_password,
    )


async def begin_confirm_link(user) -> str:
    """A one-time token for emailing a link, for when there's no app to type a code
    into: a build 4 phone, or someone who only used the website."""
    raw = secrets.token_urlsafe(32)
    await db.authchallenge.create(
        data={
            "userId": user.id,
            "tokenHash": security.hash_refresh_token(raw),
            "purpose": "confirm_link",
            "expiresAt": datetime.now(timezone.utc) + timedelta(days=_CONFIRM_LINK_DAYS),
        }
    )
    return raw


async def _spend_code(user_id: str, find_challenge, code: str):
    """Check a code under lock: attempts commit even when it's wrong. The spent
    challenge if it matched, else None."""
    import hmac

    now = datetime.now(timezone.utc)
    async with db.tx() as tx:
        await tx.query_raw('SELECT id FROM "User" WHERE id = $1 FOR UPDATE', user_id)
        challenge = await find_challenge(tx)
        if (
            challenge is None
            or challenge.usedAt is not None
            or challenge.expiresAt <= now
            or challenge.attempts >= 5
            or challenge.purpose not in _CODE_PURPOSES
        ):
            return None
        recent = await tx.authchallenge.find_many(
            where={"userId": user_id, "createdAt": {"gt": now - timedelta(days=1)}}
        )
        if sum(item.attempts for item in recent) >= _WRONG_GUESSES_PER_DAY:
            return None
        await tx.authchallenge.update(
            where={"id": challenge.id}, data={"attempts": {"increment": 1}}
        )
        if not hmac.compare_digest(challenge.codeHash or "", _code_digest(user_id, code)):
            return None
        return await tx.authchallenge.update(
            where={"id": challenge.id}, data={"usedAt": now}
        )


@router.post("/challenge/verify", response_model=TokenResponse)
@limiter.limit("20/15 minutes")
async def verify_challenge(request: Request, body: VerifyChallengeRequest):
    """Answer the code step of sign-up, or of linking a provider to an account."""
    token_hash = security.hash_refresh_token(body.challengeToken)
    preliminary = await db.authchallenge.find_unique(where={"tokenHash": token_hash})
    if preliminary is None:
        raise HTTPException(400, _BAD_CODE)
    challenge = await _spend_code(
        preliminary.userId,
        lambda tx: tx.authchallenge.find_unique(where={"tokenHash": token_hash}),
        body.code,
    )
    if challenge is None:
        raise HTTPException(400, _BAD_CODE)
    user = await db.user.find_unique(where={"id": challenge.userId})
    if user is None:
        raise HTTPException(400, _BAD_CODE)
    if challenge.purpose == "link_identity" and challenge.pendingIssuer:
        await _finish_link(user, challenge)
        user = await db.user.find_unique(where={"id": user.id})
    await confirm_email(db, user)
    return await _issue_tokens(user.id)


async def _finish_link(user, challenge) -> None:
    """All or nothing. If the account had never proved its email, whoever set its
    password proved nothing: that password and every session end here, and the
    person just proved the address. A confirmed account keeps its password."""
    now = datetime.now(timezone.utc)
    data = {"emailVerifiedAt": user.emailVerifiedAt or now}
    if challenge.pendingSecret:
        data["appleRefreshToken"] = challenge.pendingSecret
    async with db.tx() as tx:
        known = await tx.externalidentity.find_unique(
            where={"issuer_subject": {"issuer": challenge.pendingIssuer, "subject": challenge.pendingSubject}}
        )
        if known is None:
            await tx.externalidentity.create(
                data={
                    "userId": user.id,
                    "issuer": challenge.pendingIssuer,
                    "subject": challenge.pendingSubject,
                }
            )
        elif known.userId != user.id:
            raise HTTPException(409, "That sign-in already belongs to another account.")
        if user.emailVerifiedAt is None:
            data.update(passwordHash=None, sessionGeneration={"increment": 1})
            await tx.refreshtoken.update_many(
                where={"userId": user.id, "revoked": False}, data={"revoked": True}
            )
        await tx.user.update(where={"id": user.id}, data=data)


def _confirm_page(title: str, body: str, form: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{title}</title><body style='font-family:system-ui;max-width:28rem;margin:4rem auto;padding:0 1.5rem'>"
        f"<h1 style='font-weight:500'>{title}</h1><p>{body}</p>{form}"
    )


async def _live_confirm_link(token: str):
    challenge = await db.authchallenge.find_unique(
        where={"tokenHash": security.hash_refresh_token(token)}
    )
    if (
        challenge is None
        or challenge.purpose != "confirm_link"
        or challenge.usedAt is not None
        or challenge.expiresAt <= datetime.now(timezone.utc)
    ):
        return None
    return challenge


@router.get("/email/confirm-link", response_class=HTMLResponse)
async def confirm_link_page(token: str):
    """Only shows a button: mail scanners open links, and that mustn't confirm."""
    if await _live_confirm_link(token) is None:
        return _confirm_page("This link has expired", "Open vournal to confirm your email instead.")
    form = (
        f"<form method=post action='/auth/email/confirm-link?token={quote(token)}'>"
        "<button style='font:inherit;padding:.6rem 1.2rem'>Confirm my email</button></form>"
    )
    return _confirm_page("Confirm your email", "This turns on what you bought with this address.", form)


@router.post("/email/confirm-link", response_class=HTMLResponse)
@limiter.limit("20/15 minutes")
async def confirm_link(request: Request, token: str):
    challenge = await _live_confirm_link(token)
    if challenge is None:
        raise HTTPException(400, "This link has expired.")
    await db.authchallenge.update(
        where={"id": challenge.id}, data={"usedAt": datetime.now(timezone.utc)}
    )
    user = await db.user.find_unique(where={"id": challenge.userId})
    if user is None:
        raise HTTPException(400, "This link has expired.")
    await confirm_email(db, user)
    return _confirm_page(
        "You’re all set",
        "Your email is confirmed, and Plus is on. Open vournal to use it. "
        "If you never made a vournal account, someone else used your email: use "
        '"Forgot password" in vournal to set your own password, which signs them out.',
    )


@router.post("/email/send-code")
async def send_confirmation_code(current=Depends(get_current_user)):
    """Profile's "Confirm your email" prompt, for accounts older than the code step."""
    if current.emailVerifiedAt is not None:
        return {"ok": True, "alreadyConfirmed": True}
    if rate_limited("confirm-email", current.id, 3, 3600):
        raise HTTPException(429, "Too many codes. Please wait and try again.")
    await begin_email_challenge(current)
    return {"ok": True, "alreadyConfirmed": False}


@router.post("/email/confirm", response_model=UserOut)
@limiter.limit("20/15 minutes")
async def confirm_email_code(
    request: Request, body: ConfirmEmailRequest, current=Depends(get_current_user)
):
    challenge = await _spend_code(
        current.id,
        lambda tx: tx.authchallenge.find_first(
            where={"userId": current.id, "purpose": "verify_email", "usedAt": None},
            order={"createdAt": "desc"},
        ),
        body.code.strip(),
    )
    if challenge is None:
        raise HTTPException(400, _BAD_CODE)
    return _user_out(await confirm_email(db, current))


@router.post("/register", response_model=TokenResponse | AuthChallengeOut)
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
            "emailConfirmRequired": body.confirmEmail,
        }
    )
    if body.confirmEmail:
        return await begin_email_challenge(user)
    return await _issue_tokens(user.id)


@router.post("/login", response_model=TokenResponse | AuthChallengeOut)
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
    if body.confirmEmail and user.emailConfirmRequired and user.emailVerifiedAt is None:
        return await begin_email_challenge(user)
    # The generation read before the slow password check: a link in between kills this.
    return await _issue_tokens(user.id, user.sessionGeneration)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    return await rotate_refresh_token(body.refreshToken)


async def rotate_refresh_token(raw: str) -> TokenResponse:
    """Spend a refresh token for a fresh pair, or raise 401. Shared with the web."""
    token_hash = security.hash_refresh_token(raw)
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

    # Issued before the account's sessions were ended (a link or a reset).
    owner = await db.user.find_unique(where={"id": stored.userId})
    if owner is None or stored.generation != owner.sessionGeneration:
        raise HTTPException(401, "Invalid or expired refresh token.")

    # Rotate: the presented token is now spent (revoked); a fresh pair is issued.
    await db.refreshtoken.update(where={"id": stored.id}, data={"revoked": True})
    return await _issue_tokens(stored.userId, stored.generation)


@router.post("/logout")
async def logout(body: LogoutRequest):
    """Revoke the presented refresh token. Idempotent and always 200 — it must
    not reveal whether the token existed."""
    await revoke_refresh_token(body.refreshToken)
    return {"ok": True}


async def revoke_refresh_token(raw: str) -> None:
    token_hash = security.hash_refresh_token(raw)
    stored = await db.refreshtoken.find_unique(where={"tokenHash": token_hash})
    if stored is not None and not stored.revoked:
        await db.refreshtoken.update(where={"id": stored.id}, data={"revoked": True})


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
        emailConfirmed=user.emailVerifiedAt is not None,
        hasPassword=user.passwordHash is not None,
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

    Re-confirm with the password, or the word DELETE for an account with none
    (deletion is irreversible). ORDER MATTERS: the
    recordings live in external storage the DB cascade knows nothing about, so we
    enumerate and delete every audio object FIRST (while the rows still tell us the
    keys), THEN delete the user — whose cascade removes entries, todos, topics,
    people, and refresh tokens. Getting this backwards orphans every recording in
    the bucket forever.
    """
    if current.passwordHash is None:
        if body.confirmWord != "DELETE":
            raise HTTPException(403, "Type DELETE to confirm.")
    elif not body.password or not security.verify_password(
        body.password, current.passwordHash
    ):
        raise HTTPException(403, "Password is incorrect.")
    await delete_account(current)


async def delete_account(current) -> None:
    """Delete an account and everything it owns, once the caller has confirmed it.
    Shared with the web's DELETE /api/users/me."""
    # Apple requires revoking its token when the account goes.
    from app.oauth import APPLE_ISSUER, log as oauth_log, revoke_apple

    if current.appleRefreshToken:
        await revoke_apple(current.appleRefreshToken)
    elif await db.externalidentity.find_first(
        where={"userId": current.id, "issuer": APPLE_ISSUER}
    ):
        oauth_log.error("Apple account %s deleted with no token to revoke.", current.id)

    # 1) External storage first — best-effort per object (a missing one is fine).
    entries = await db.entry.find_many(where={"userId": current.id})
    for entry in entries:
        if entry.audioUrl:
            await delete_audio(entry.audioUrl)
    # vournal OS attachments live in the same bucket, under their own prefix.
    from app import storage

    await storage.delete_objects([a.storageKey for a in await db.attachment.find_many(where={"userId": current.id})])

    # 2) Then the account. Cascade (schema onDelete) removes everything else; a vournal
    #    OS household it was in must not be left empty or without an owner.
    from app.os.household import settle_household

    async with db.tx() as tx:
        membership = await tx.householdmember.find_unique(where={"userId": current.id})
        await tx.user.delete(where={"id": current.id})
        if membership:
            await settle_household(tx, membership.householdId)


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
                    "expiresAt": datetime.now(timezone.utc)
                    + timedelta(minutes=RESET_TTL_MIN),
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
        data={
            "passwordHash": security.hash_password(body.newPassword),
            "sessionGeneration": {"increment": 1},
        },
    )
    await db.passwordresettoken.update(where={"id": stored.id}, data={"usedAt": now})
    await db.authchallenge.update_many(
        where={"userId": stored.userId, "usedAt": None, "purpose": {"in": _CODE_PURPOSES}},
        data={"usedAt": now},
    )
    # The reset link went to this address, so it's proven theirs.
    owner = await db.user.find_unique(where={"id": stored.userId})
    if owner is not None:
        await confirm_email(db, owner)
    # Every existing session is now stale — revoke all refresh tokens.
    await db.refreshtoken.update_many(
        where={"userId": stored.userId, "revoked": False}, data={"revoked": True}
    )
    return {"ok": True}
