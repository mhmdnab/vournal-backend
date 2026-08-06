"""Audio storage — Cloudflare R2 (S3-compatible, private) behind a thin seam.

The seam is unchanged in spirit: `save_audio` returns a reference stored on
`Entry.audioUrl` (an object *key*, `{entry_id}.{ext}`), and the other functions act
on that reference. Migrated off Supabase Storage to **Cloudflare R2**:

- The bucket is **PRIVATE**. Nothing is publicly reachable; every read goes through
  a short-lived **presigned GET URL** minted by an authenticated route. (R2 also
  offers a public `r2.dev` URL — deliberately NOT used, so journal audio never
  becomes publicly accessible; keep public access disabled on the bucket.)
- R2 speaks the **S3 API**, so this uses `boto3` with SigV4. The access key/secret
  bypass all access control, so they live only here, server-side. Presigning is an
  offline (no-network) operation — instant — so the prefetch speed win is preserved;
  the network calls (put/get/delete) run off the event loop via `asyncio.to_thread`.

Functions (all async — the network ones do I/O off the loop):
  save_audio(entry_id, ext, data) -> object key
  create_signed_url(key, ttl)     -> a time-limited GET URL (or None)
  resolve_audio_path(key)         -> a local TEMP file path (caller deletes it)
  delete_audio(key)               -> True if an object was removed
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

# Signed URLs are short-lived by design: long enough to start playback, short
# enough that a leaked URL is near-useless.
DEFAULT_SIGNED_TTL = 300  # seconds (5 min)

# A recording never changes once created, so it can be cached for a long time. Set
# on the object's Cache-Control at upload — the TTL the browser may cache the bytes
# of a REUSED signed URL for. Doesn't speed the first fetch; the prefetch covers that.
AUDIO_CACHE_MAX_AGE = 31_536_000  # seconds (1 year)

# Content types for upload, so playback gets a real audio/* type back.
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
    """Raised when the R2 env vars are missing."""


_client_cache = None  # (boto3 s3 client, bucket) — built once per process


def _client_and_bucket():
    """A cached S3 client pointed at this account's R2 endpoint, plus the bucket."""
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    account = os.environ.get("R2_ACCOUNT_ID") or ""
    key_id = os.environ.get("R2_ACCESS_KEY_ID") or ""
    secret = os.environ.get("R2_SECRET_ACCESS_KEY") or ""
    bucket = os.environ.get("R2_BUCKET") or ""
    if not (account and key_id and secret and bucket):
        raise StorageNotConfigured(
            "R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET must be set."
        )

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
        # Path-style + SigV4 is the reliable form for R2 presigned URLs.
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    _client_cache = (client, bucket)
    return _client_cache


def _ext_of(key: str) -> str:
    return os.path.splitext(key)[1].lstrip(".").lower()


async def save_audio(entry_id: str, ext: str, data: bytes) -> str:
    """Upload audio bytes to the bucket; return the object key (stored on the
    entry). Keyed by entry id so there are no collisions and it's easy to find."""
    client, bucket = _client_and_bucket()
    ext = ext.lstrip(".").lower()
    object_key = f"{entry_id}.{ext}"
    content_type = _MIME.get(ext, "application/octet-stream")
    await asyncio.to_thread(
        client.put_object,
        Bucket=bucket,
        Key=object_key,
        Body=data,
        ContentType=content_type,
        CacheControl=f"max-age={AUDIO_CACHE_MAX_AGE}",
    )
    return object_key


async def create_signed_url(key: str, ttl: int = DEFAULT_SIGNED_TTL) -> Optional[str]:
    """Mint a short-lived presigned GET URL for a private object, or None if it
    can't be signed. Presigning is offline (no network), so this is instant. Never
    raises."""
    if not key:
        return None
    try:
        client, bucket = _client_and_bucket()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl,
        )
    except (BotoCoreError, ClientError, StorageNotConfigured):
        return None


async def resolve_audio_path(key: str) -> str:
    """Download an object to a local TEMP file and return its path — the
    transcription step opens a file. **The caller must delete the temp file.**"""
    client, bucket = _client_and_bucket()

    def _download() -> bytes:
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    data = await asyncio.to_thread(_download)
    suffix = f".{_ext_of(key)}" if _ext_of(key) else ""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="vournal-audio-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


async def delete_audio(key: str) -> bool:
    """Remove an object from the bucket. Returns True only if an object was actually
    there to remove (S3 delete is idempotent, so HEAD first). Never raises."""
    if not key:
        return False
    try:
        client, bucket = _client_and_bucket()

        def _delete() -> bool:
            try:
                client.head_object(Bucket=bucket, Key=key)
            except ClientError:
                return False  # already gone
            client.delete_object(Bucket=bucket, Key=key)
            return True

        return await asyncio.to_thread(_delete)
    except (BotoCoreError, ClientError, StorageNotConfigured):
        return False
