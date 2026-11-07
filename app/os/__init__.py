"""vournal OS: the web dashboard's API, ported from the NestJS vournalOS-backend.

Every route lives under /api with the paths, bodies and answers NestJS had, so
the dashboard (vournalOS-frontend) only changes its BACKEND_URL. The app's own
routes stay at the root, untouched.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.os.common.deps import csrf_check
from app.os.common.http import nest_router
from app.os.common.serialize import to_json

api_router: APIRouter = nest_router("/api", dependencies=[Depends(csrf_check)])
_STARTED = time.monotonic()


@api_router.get("/health")
async def health():
    """Public liveness + database check (NestJS's HealthController)."""
    from app.db import db

    started = time.monotonic()
    try:
        await db.query_raw("SELECT 1")
    except Exception:  # noqa: BLE001 — any failure means the database is down
        return JSONResponse(status_code=503, content={"status": "error", "db": "down"})
    return {
        "status": "ok",
        "db": "up",
        "dbLatencyMs": round((time.monotonic() - started) * 1000),
        "uptimeSeconds": round(time.monotonic() - _STARTED),
        "time": to_json(datetime.now(timezone.utc)),
    }


_included = False


def include_os_routers() -> APIRouter:
    """Attach every section's router (once) and return /api for app.include_router."""
    global _included
    if not _included:
        from app.os import (
            accounts,
            ai,
            app_todos,
            assistant,
            assistant_voice,
            attachments,
            auth,
            budgets,
            calendar,
            categories,
            challenges,
            dashboard,
            debts,
            documents,
            events,
            exchange_rates,
            finance,
            goals,
            meal_plan,
            habits,
            household,
            insights,
            journal,
            maintenance,
            important_dates,
            notes,
            notifications,
            onboarding,
            people,
            prayer_times,
            projects,
            push,
            recipes,
            recurring_payments,
            reports,
            rituals,
            reminders,
            search,
            settings,
            shopping,
            study,
            tags,
            tasks,
            transactions,
            trips,
            users,
            wellness,
            work,
            workouts,
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
        api_router.include_router(notes.router)
        api_router.include_router(projects.router)
        api_router.include_router(documents.router)
        api_router.include_router(habits.router)
        api_router.include_router(challenges.router)
        api_router.include_router(goals.router)
        api_router.include_router(shopping.router)
        api_router.include_router(recipes.router)
        api_router.include_router(meal_plan.router)
        api_router.include_router(wellness.router)
        api_router.include_router(workouts.router)
        api_router.include_router(prayer_times.router)
        api_router.include_router(household.router)
        api_router.include_router(people.router)
        api_router.include_router(important_dates.router)
        api_router.include_router(study.router)
        api_router.include_router(trips.router)
        api_router.include_router(trips.public_router)
        api_router.include_router(maintenance.router)
        api_router.include_router(insights.router)
        api_router.include_router(reports.router)
        api_router.include_router(rituals.router)
        api_router.include_router(dashboard.router)
        api_router.include_router(search.router)
        api_router.include_router(onboarding.router)
        api_router.include_router(app_todos.router)
        api_router.include_router(journal.router)
        api_router.include_router(push.router)
        api_router.include_router(ai.router)
        api_router.include_router(assistant.router)
        api_router.include_router(assistant_voice.router)
        api_router.include_router(work.router)
        _included = True
    return api_router
