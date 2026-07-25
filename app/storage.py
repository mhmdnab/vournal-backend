"""Audio storage — Supabase Storage (private bucket) behind a thin seam.

The seam is unchanged in spirit from the local-disk version: `save_audio` returns
a reference stored on `Entry.audioUrl` (now an object *key*, not a path), and the
other functions act on that reference. What changed (Step 11):

- Storage is a **private** Supabase bucket. Nothing is publicly reachable; every
  read goes through a short-lived **signed URL** minted by an authenticated route.
- The backend talks to Storage over its **REST API** with the `service_role` key
  (async httpx — the app is async; the sync `supabase` client would block the
  loop). The key bypasses all access control, so it lives only here, server-side.

Functions (all async now — they do network I/O):
  save_audio(entry_id, ext, data) -> object key
  create_signed_url(key, ttl)     -> a time-limited GET URL (or None)
  resolve_audio_path(key)         -> a local TEMP file path (caller deletes it)
  delete_audio(key)               -> True if an object was removed

Verified against the Supabase Storage REST API (2026): object upload is
`POST /storage/v1/object/{bucket}/{key}`; a signed URL is
`POST /storage/v1/object/sign/{bucket}/{key}` with `{"expiresIn": secs}` returning
a relative `signedURL`; authenticated download is `GET /storage/v1/object/{bucket}/{key}`;
delete is `DELETE /storage/v1/object/{bucket}/{key}`.
"""

from __future__ import annotations

import os
import tempfile
from typing import Optional

import httpx

# Signed URLs are short-lived by design: long enough to start playback, short
# enough that a leaked URL is near-useless.
DEFAULT_SIGNED_TTL = 300  # seconds (5 min)

# A recording never changes once created, so it can be cached for a long time.
# This sets the object's stored `cache_control` metadata (Step 12) — the TTL the
# browser/CDN may cache the bytes of a REUSED signed URL for. It does NOT speed
# the first fetch of a fresh token; the client-side prefetch covers that.
AUDIO_CACHE_MAX_AGE = 31_536_000  # seconds (1 year)

# Content types for upload, so Storage serves a real audio/* type back.
_MIME = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "mpga": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
}


class StorageNotConfigured(RuntimeError):
    """Raised when the Supabase Storage env vars are missing."""


def _config() -> tuple[str, str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    bucket = os.environ.get("SUPABASE_AUDIO_BUCKET") or ""
    if not (url and key and bucket):
        raise StorageNotConfigured(
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and SUPABASE_AUDIO_BUCKET must be set."
        )
    return f"{url}/storage/v1", key, bucket


def _headers(key: str) -> dict[str, str]:
    # Both headers are required by the Storage API.
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _ext_of(key: str) -> str:
    return os.path.splitext(key)[1].lstrip(".").lower()


async def save_audio(entry_id: str, ext: str, data: bytes) -> str:
    """Upload audio bytes to the bucket; return the object key (stored on the
    entry). Keyed by entry id so there are no collisions and it's easy to find."""
    base, api_key, bucket = _config()
    ext = ext.lstrip(".").lower()
    object_key = f"{entry_id}.{ext}"
    content_type = _MIME.get(ext, "application/octet-stream")
    headers = {
        **_headers(api_key),
        "x-upsert": "true",  # a retried upload for the same entry overwrites
    }
    # Multipart upload with a `cacheControl` field — the Storage API IGNORES a raw
    # Cache-Control request header, but persists this form field into the object's
    # `cache_control` metadata (this is exactly what storage-js does). The empty
    # field name for the file part matches the SDK. Recordings are immutable, so a
    # long TTL lets the browser/CDN cache the bytes of a reused signed URL.
    files = {"": (object_key, data, content_type)}
    form = {"cacheControl": str(AUDIO_CACHE_MAX_AGE)}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base}/object/{bucket}/{object_key}",
            headers=headers,
            data=form,
            files=files,
        )
    resp.raise_for_status()
    return object_key


async def create_signed_url(
    key: str, ttl: int = DEFAULT_SIGNED_TTL
) -> Optional[str]:
    """Mint a short-lived GET URL for a private object, or None if it can't be
    signed (missing object / bad key). Never raises."""
    if not key:
        return None
    try:
        base, api_key, bucket = _config()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{base}/object/sign/{bucket}/{key}",
                headers=_headers(api_key),
                json={"expiresIn": ttl},
            )
        if resp.status_code != 200:
            return None
        signed = resp.json().get("signedURL")
        # signedURL is relative to /storage/v1, e.g. "/object/sign/bucket/key?token=…"
        return f"{base}{signed}" if signed else None
    except (httpx.HTTPError, StorageNotConfigured, ValueError):
        return None


async def resolve_audio_path(key: str) -> str:
    """Download an object to a local TEMP file and return its path — the
    transcription step opens a file. **The caller must delete the temp file.**"""
    base, api_key, bucket = _config()
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            f"{base}/object/{bucket}/{key}", headers=_headers(api_key)
        )
    resp.raise_for_status()
    suffix = f".{_ext_of(key)}" if _ext_of(key) else ""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="vournal-audio-")
    with os.fdopen(fd, "wb") as f:
        f.write(resp.content)
    return path


async def delete_audio(key: str) -> bool:
    """Remove an object from the bucket. Tolerates an already-missing object
    (returns False). Never raises."""
    if not key:
        return False
    try:
        base, api_key, bucket = _config()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.delete(
                f"{base}/object/{bucket}/{key}", headers=_headers(api_key)
            )
        return resp.status_code == 200
    except (httpx.HTTPError, StorageNotConfigured):
        return False
