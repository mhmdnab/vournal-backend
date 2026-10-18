"""Opt-in feature boundary. The experimental server must never use remote DBs."""

import os
from urllib.parse import urlparse


def enabled() -> bool:
    if os.environ.get("VOURNAL_ENHANCEMENTS") != "1":
        return False
    for key in ("DATABASE_URL", "DIRECT_URL"):
        url = urlparse(os.environ.get(key, ""))
        if (
            url.scheme not in {"postgres", "postgresql"}
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not url.path.lstrip("/").startswith("vournal_enhancements")
        ):
            raise RuntimeError(f"Enhancements require a dedicated local {key}.")
    return True


def require_enabled() -> None:
    from fastapi import HTTPException

    if not enabled():
        raise HTTPException(404, "Not found")
