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
        from app.os import (
            accounts,
            attachments,
            auth,
            budgets,
            calendar,
            categories,
            debts,
            events,
            exchange_rates,
            finance,
            notifications,
            recurring_payments,
            reminders,
            settings,
            tags,
            tasks,
            transactions,
            users,
        )

        api_router.include_router(auth.router)
        api_router.include_router(categories.router)
        api_router.include_router(settings.router)
        api_router.include_router(users.router)
        api_router.include_router(tags.router)
        api_router.include_router(exchange_rates.router)
        api_router.include_router(tasks.router)
        api_router.include_router(events.router)
        api_router.include_router(reminders.router)
        api_router.include_router(notifications.router)
        api_router.include_router(calendar.router)
        api_router.include_router(accounts.router)
        api_router.include_router(transactions.router)
        api_router.include_router(budgets.router)
        api_router.include_router(recurring_payments.router)
        api_router.include_router(debts.router)
        api_router.include_router(finance.router)
        api_router.include_router(attachments.router)
        _included = True
    return api_router
