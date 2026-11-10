"""Sign in with Apple and Google. Nothing from the client is trusted until the
provider's signature, issuer, audience, expiry and nonce check out on the server.

Identities are keyed by (issuer, subject). An email only ever links to an existing
account when the provider says it's verified and the account proved it too; an
unconfirmed account has to answer a code sent to that email first.
"""

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Literal

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from prisma.errors import UniqueViolationError
from pydantic import BaseModel, Field

from app import mfa
from app.auth import _issue_tokens, begin_email_challenge
from app.db import db
from app.entitlements import confirm_email
from app.rate_limit import limiter
from app.schemas import AuthChallengeOut, TokenResponse

log = logging.getLogger("vournal.oauth")
router = APIRouter(prefix="/auth/oauth", tags=["auth"])

Provider = Literal["apple", "google"]
APPLE_ISSUER = "https://appleid.apple.com"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
# Addresses each provider runs itself. For any other address, "verified" only means
# someone proved it once, and they may not own it now.
_APPLE_RUN_DOMAINS = {"privaterelay.appleid.com", "icloud.com", "me.com", "mac.com"}
_GOOGLE_RUN_DOMAINS = {"gmail.com", "googlemail.com"}
_JWKS = {
    # The key set is cached for an hour; an unknown key id fetches it again.
    "apple": jwt.PyJWKClient(f"{APPLE_ISSUER}/auth/keys", lifespan=3600),
    "google": jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", lifespan=3600),
}


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    # The provider runs this address, so its word is good enough to link directly.
    authoritative: bool = False


class ProviderSignIn(BaseModel):
    idToken: str = Field(min_length=1, max_length=8192)
    nonce: str = Field(min_length=1, max_length=256)
    # Apple only: exchanged for the token that deleting the account revokes.
    authorizationCode: str | None = Field(default=None, max_length=4096)
    timezone: str | None = Field(default=None, max_length=64)


def _bundle_id() -> str:
    return _audiences("apple")[0] if _audiences("apple") else "app.vournal.mobile"


def _audiences(provider: Provider) -> list[str]:
    raw = (
        os.environ.get("OAUTH_APPLE_AUDIENCES", "app.vournal.mobile")
        if provider == "apple"
        else os.environ.get("GOOGLE_OAUTH_CLIENT_IDS", "")
    )
    return [value.strip() for value in raw.split(",") if value.strip()]


async def _signing_key(provider: Provider, id_token: str):
    client = _JWKS[provider]
    signing = await asyncio.to_thread(client.get_signing_key_from_jwt, id_token)
    return signing.key


async def verify_identity(provider: Provider, id_token: str, nonce: str) -> VerifiedIdentity:
    """The verified identity in a provider's token, or 401. 503 if keys can't load."""
    try:
        key = await _signing_key(provider, id_token)
    except (jwt.PyJWKClientConnectionError, jwt.PyJWKSetError) as exc:
        log.warning("Could not load %s signing keys: %r", provider, exc)
        raise HTTPException(503, "Sign-in is unavailable right now. Please try again.")
    except jwt.PyJWTError:  # unknown key id, or not a token at all
        raise HTTPException(401, "Sign-in could not be verified.")
    except Exception as exc:  # noqa: BLE001 — a bad reply from the provider isn't the user's fault
        log.warning("Could not load %s signing keys: %r", provider, exc)
        raise HTTPException(503, "Sign-in is unavailable right now. Please try again.")
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=_audiences(provider),
            issuer=APPLE_ISSUER if provider == "apple" else GOOGLE_ISSUERS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(401, "Sign-in could not be verified.")
    # Apple always echoes the nonce. Google's does when the client passed one.
    expected = hashlib.sha256(nonce.encode()).hexdigest()
    if (provider == "apple" or "nonce" in claims) and claims.get("nonce") != expected:
        raise HTTPException(401, "Sign-in could not be verified.")
    email = claims.get("email")
    email = email.strip().lower() if isinstance(email, str) and email.strip() else None
    domain = email.rsplit("@", 1)[-1] if email else ""
    return VerifiedIdentity(
        issuer=claims["iss"],
        subject=claims["sub"],
        email=email,
        authoritative=(
            domain in _APPLE_RUN_DOMAINS
            if provider == "apple"
            else domain in _GOOGLE_RUN_DOMAINS or claims.get("hd") == domain
        ),
        # Apple sends this as the string "true".
        email_verified=claims.get("email_verified") in (True, "true"),
    )


def _apple_client_secret() -> str | None:
    key = os.environ.get("APPLE_SIGNIN_PRIVATE_KEY", "").replace("\\n", "\n")
    team = os.environ.get("APPLE_TEAM_ID")
    key_id = os.environ.get("APPLE_SIGNIN_KEY_ID")
    if not (key and team and key_id):
        return None
    now = int(time.time())
    return jwt.encode(
        {"iss": team, "iat": now, "exp": now + 300, "aud": APPLE_ISSUER, "sub": _bundle_id()},
        key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


async def exchange_apple_code(code: str) -> str | None:
    """Apple's refresh token, sealed for storage. None if this can't be done now:
    sign-in still works, and the failure is logged because deletion relies on it."""
    try:
        secret = _apple_client_secret()
        if secret is None:
            log.error("Sign in with Apple key isn't configured; no token to revoke later.")
            return None
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{APPLE_ISSUER}/auth/token",
                data={
                    "client_id": _bundle_id(),
                    "client_secret": secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
        refresh = response.json().get("refresh_token")
        return mfa.encrypt(refresh) if refresh else None
    except Exception as exc:  # noqa: BLE001 — never fail a sign-in over this
        log.error("Apple code exchange failed: %r", exc)
        return None


async def revoke_apple(sealed: str) -> None:
    """Apple requires revoking on account deletion. A failure mustn't stop deleting."""
    try:
        secret = _apple_client_secret()
        if secret is None:
            log.error("Sign in with Apple key isn't configured; token not revoked.")
            return
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{APPLE_ISSUER}/auth/revoke",
                data={
                    "client_id": _bundle_id(),
                    "client_secret": secret,
                    "token": mfa.decrypt(sealed),
                    "token_type_hint": "refresh_token",
                },
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("Apple token revoke failed: %r", exc)


def config_problems() -> list[str]:
    """What stops deletion revoking Apple's token, which Apple requires. Logged at
    startup, so a bad deploy is visible before anyone deletes an account."""
    problems = []
    try:
        if _apple_client_secret() is None:
            problems.append("APPLE_TEAM_ID, APPLE_SIGNIN_KEY_ID and APPLE_SIGNIN_PRIVATE_KEY must all be set")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"APPLE_SIGNIN_PRIVATE_KEY isn't a usable key: {type(exc).__name__}")
    try:
        mfa.decrypt(mfa.encrypt("check"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"VOURNAL_FIELD_ENCRYPTION_KEY isn't usable: {type(exc).__name__}")
    return problems


@router.get("/providers")
async def providers():
    return {name: {"enabled": bool(_audiences(name))} for name in ("apple", "google")}


async def link_identity(user_id: str, issuer: str, subject: str) -> None:
    try:
        await db.externalidentity.create(
            data={"userId": user_id, "issuer": issuer, "subject": subject}
        )
    except UniqueViolationError:
        pass  # a double tap got here first


async def identity_user(provider: Provider, id_token: str, nonce: str):
    """(the account this provider sign-in is already linked to or None, the verified
    identity). Never creates or links anything itself: the web route decides."""
    identity = await verify_identity(provider, id_token, nonce)
    known = await db.externalidentity.find_unique(
        where={"issuer_subject": {"issuer": identity.issuer, "subject": identity.subject}}
    )
    user = None if known is None else await db.user.find_unique(where={"id": known.userId})
    return user, identity


@router.post("/{provider}", response_model=TokenResponse | AuthChallengeOut)
@limiter.limit("20/15 minutes")
async def sign_in(provider: Provider, request: Request, body: ProviderSignIn):
    if not _audiences(provider):
        raise HTTPException(503, "This sign-in option isn't available yet.")
    identity = await verify_identity(provider, body.idToken, body.nonce)
    sealed = (
        await exchange_apple_code(body.authorizationCode)
        if provider == "apple" and body.authorizationCode
        else None
    )

    known = await db.externalidentity.find_unique(
        where={"issuer_subject": {"issuer": identity.issuer, "subject": identity.subject}}
    )
    if known is not None:
        if sealed:
            await db.user.update(where={"id": known.userId}, data={"appleRefreshToken": sealed})
        return await _issue_tokens(known.userId)

    if identity.email is None or not identity.email_verified:
        raise HTTPException(409, "We couldn't confirm your email. Use your email and password.")

    extra = {"appleRefreshToken": sealed} if sealed else {}
    user = await db.user.find_unique(where={"email": identity.email})
    if user is None:
        user = await db.user.create(
            data={"email": identity.email, "timezone": body.timezone, **extra}
        )
        await link_identity(user.id, identity.issuer, identity.subject)
        await confirm_email(db, user)
        return await _issue_tokens(user.id)

    if user.emailVerifiedAt is not None and identity.authoritative:
        if extra:
            await db.user.update(where={"id": user.id}, data=extra)
        await link_identity(user.id, identity.issuer, identity.subject)
        return await _issue_tokens(user.id)

    # The same email on an account that never proved it, or that the provider can't
    # vouch for: the code proves it now.
    return await begin_email_challenge(
        user, "link_identity", identity.issuer, identity.subject, sealed
    )
