"""Who is calling /api, and whether they may.

The web keeps the app's access token in the `life_at` cookie (or sends it as a
bearer token). A cookie-authenticated write must also carry
`X-Requested-With: life-os`: a cross-site form can't set that header, and a
cross-site fetch that sets it needs a CORS preflight only the dashboard passes.
"""

from fastapi import Depends, Request

from app.auth import user_for_access_token
from app.entitlements import effective_plan
from app.os.common.http import nest_error

ACCESS_COOKIE = "life_at"
REFRESH_COOKIE = "life_rt"
REFRESH_COOKIE_PATH = "/api/auth"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "life-os"
PLUS_REQUIRED = "vournal life is part of vournal Plus."

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[len("bearer ") :].strip() if header.lower().startswith("bearer ") else None


async def csrf_check(request: Request) -> None:
    """On every /api route: cookie-authenticated writes need the header."""
    if request.method.upper() in _SAFE_METHODS or _bearer(request):
        return
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise nest_error(403, "Missing or invalid X-Requested-With header")


async def web_user(request: Request):
    token = _bearer(request) or request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise nest_error(401, "Not authenticated")
    user = await user_for_access_token(token)
    if user is None:
        raise nest_error(401, "Session expired")
    return user


async def plus_user(user=Depends(web_user)):
    """Checked on every request, so a lapsed subscription stops at the next call."""
    if effective_plan(user) == "free":
        raise nest_error(403, PLUS_REQUIRED, code="plus_required")
    return user
