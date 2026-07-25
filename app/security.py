"""Password hashing and token primitives — the crypto seam for auth.

Two token types, on purpose:
  - **Access token**: a short-lived stateless JWT (HS256). Fast to verify, no DB
    hit, expires quickly.
  - **Refresh token**: a long-lived *opaque random* string. We store only its
    SHA-256 hash in the DB, so it can be revoked (logout) and rotated (refresh).
    Never a JWT — statelessness is the wrong property for something revocable.

Password hashing uses bcrypt directly. passlib was the obvious pick but it's
unmaintained (last release 2020) and logs a spurious error against bcrypt 4.1+.
A SHA-256 pre-hash lifts bcrypt's 72-byte password truncation so long
passphrases hash their full length.

Config is read LAZILY (inside functions), because app.main calls load_dotenv()
*after* its imports — reading env at import time here would see an empty secret.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from dotenv import load_dotenv

# Safe to call repeatedly; guards against import-order surprises.
load_dotenv()

_JWT_ALG = "HS256"
_MIN_SECRET_LEN = 32  # PyJWT warns below this for HS256; enforce it.


# ---- Config (lazy) -----------------------------------------------------------


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < _MIN_SECRET_LEN:
        raise RuntimeError(
            "JWT_SECRET is missing or too short. Set it to at least "
            f"{_MIN_SECRET_LEN} characters (see .env.example)."
        )
    return secret


def _access_ttl() -> timedelta:
    return timedelta(minutes=int(os.environ.get("ACCESS_TOKEN_TTL_MIN", "30")))


def _refresh_ttl() -> timedelta:
    return timedelta(days=int(os.environ.get("REFRESH_TOKEN_TTL_DAYS", "60")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- Passwords ---------------------------------------------------------------


def _prehash(password: str) -> bytes:
    """SHA-256 → base64 (44 bytes) so bcrypt never silently truncates at 72."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed stored hash — treat as a failed verification, never crash.
        return False


# ---- Access tokens (JWT) -----------------------------------------------------


def create_access_token(user_id: str) -> str:
    now = _now()
    payload = {
        "sub": user_id,
        "type": "access",
        "iat": now,
        "exp": now + _access_ttl(),
    }
    return jwt.encode(payload, _secret(), algorithm=_JWT_ALG)


def decode_access_token(token: str) -> Optional[str]:
    """Return the user id from a valid access token, or None if it's no good."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


# ---- Refresh tokens (opaque, stored hashed) ----------------------------------


def generate_refresh_token() -> str:
    """A high-entropy opaque token. The raw value is returned to the client
    exactly once; only its hash is persisted."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex. Refresh tokens are already high-entropy random, so a plain
    fast hash is the right tool — bcrypt would be pointless here."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return _now() + _refresh_ttl()
