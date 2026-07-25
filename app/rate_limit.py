"""Rate limiting (Step 11).

Two layers, both in-memory (fine while single-instance — note they reset on every
deploy; a Redis backend is the multi-instance upgrade):

- `limiter` — slowapi, keyed by client IP. A global per-IP ceiling on everything
  (via SlowAPIMiddleware) plus tighter per-route limits on the auth endpoints.
- `rate_limited(...)` — a tiny sliding-window limiter for keys slowapi can't reach
  from the request line, namely the EMAIL in a JSON body (login-by-email, and
  forgot-password-by-email in Phase 5).
"""

from __future__ import annotations

import time

from slowapi import Limiter
from slowapi.util import get_remote_address

# Per-IP. The default_limits are the "sane global ceiling on the rest".
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])

# Per-key sliding window for body-derived keys (e.g. email).
_hits: dict[str, list[float]] = {}


def rate_limited(bucket: str, key: str, limit: int, window_sec: int) -> bool:
    """True if `key` has already hit `limit` within the last `window_sec`. Records
    this attempt when it doesn't. Keyed within a named `bucket` (e.g. "login")."""
    now = time.monotonic()
    k = f"{bucket}:{(key or '').strip().lower()}"
    hits = [t for t in _hits.get(k, ()) if now - t < window_sec]
    if len(hits) >= limit:
        _hits[k] = hits
        return True
    hits.append(now)
    _hits[k] = hits
    return False
