"""RFC 6238 TOTP and purpose-bound challenge digests; no network or storage."""

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet


def digest(value: str, purpose: str) -> str:
    from app.security import _secret

    return hmac.new(
        _secret().encode(), f"{purpose}:{value}".encode(), hashlib.sha256
    ).hexdigest()


def _fernet() -> Fernet:
    # Explicit key required. The isolated runner persists a local-only key.
    return Fernet(os.environ["VOURNAL_MFA_ENCRYPTION_KEY"].encode())


def encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt(secret: str) -> str:
    return _fernet().decrypt(secret.encode()).decode()


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def provisioning_uri(secret: str, email: str) -> str:
    return f"otpauth://totp/{quote('Vournal Local:' + email, safe='')}?secret={secret}&issuer=Vournal%20Local&algorithm=SHA1&digits=6&period=30"


def code_at(secret: str, step: int) -> str:
    padded = secret + "=" * (-len(secret) % 8)
    mac = hmac.new(
        base64.b32decode(padded), struct.pack(">Q", step), hashlib.sha1
    ).digest()
    offset = mac[-1] & 15
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify(
    secret: str, code: str, last_step: int = -1, now: float | None = None
) -> int | None:
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    step = int((time.time() if now is None else now) // 30)
    for candidate in (step, step - 1, step + 1):
        if candidate > last_step and hmac.compare_digest(
            code_at(secret, candidate), code
        ):
            return candidate
    return None
