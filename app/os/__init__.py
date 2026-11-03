"""vournal OS: the web dashboard's API, ported from the NestJS vournalOS-backend.

Every route lives under /api with the paths, bodies and answers NestJS had, so
the dashboard (vournalOS-frontend) only changes its BACKEND_URL. The app's own
routes stay at the root, untouched.
"""

from fastapi import APIRouter

from app.os.common.http import NestJSONResponse

api_router = APIRouter(prefix="/api", default_response_class=NestJSONResponse)


@api_router.get("/health")
async def health():
    return {"status": "ok"}


_included = False


def include_os_routers() -> APIRouter:
    """Attach every section's router (once) and return /api for app.include_router."""
    global _included
    if not _included:
        _included = True
    return api_router
