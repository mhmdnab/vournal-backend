"""vournal OS: the web dashboard's API, ported from the NestJS vournalOS-backend.

Every route lives under /api with the paths, bodies and answers NestJS had, so
the dashboard (vournalOS-frontend) only changes its BACKEND_URL. The app's own
routes stay at the root, untouched.
"""

from fastapi import APIRouter, Depends

from app.os.common.deps import csrf_check
from app.os.common.http import nest_router

api_router: APIRouter = nest_router("/api", dependencies=[Depends(csrf_check)])


@api_router.get("/health")
async def health():
    return {"status": "ok"}


_included = False


def include_os_routers() -> APIRouter:
    """Attach every section's router (once) and return /api for app.include_router."""
    global _included
    if not _included:
        from app.os import auth, categories, settings, users

        api_router.include_router(auth.router)
        api_router.include_router(categories.router)
        api_router.include_router(settings.router)
        api_router.include_router(users.router)
        _included = True
    return api_router
