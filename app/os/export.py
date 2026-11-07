"""GET /api/users/me/export (port of vournalOS UsersService.export): everything the account
owns as JSON, rows as stored. Encrypted document fields are exported decrypted (it is the
user's own data). The account's journal from the app is included too: entries, their
to-dos, topics and places. Secrets (password hash, tokens, keys, storage paths) never are."""

from datetime import datetime, timezone

from app.db import db
from app.os.auth import web_user_out
from app.os.documents import _try_decrypt
from app.os.profile import ensure_os_profile
from app.os.settings import settings_view

# A storage path is how the server finds a file, not the user's data.
_PRIVATE_COLUMNS = {"storageKey", "audioUrl"}


def _relation(annotation) -> bool:
    return "prisma.models." in str(annotation)


def row(value, **nested) -> dict:
    """A Prisma row's columns (no relations unless asked for, as in a findMany with include)."""
    out = {}
    for name, field in type(value).model_fields.items():
        if name in nested:
            child = getattr(value, name)
            out[name] = [nested[name](c) for c in child] if isinstance(child, list) else None if child is None else nested[name](child)
        elif not _relation(field.annotation) and name not in _PRIVATE_COLUMNS:
            out[name] = getattr(value, name)
    return out


def _brief(fields):
    return lambda value: {f: getattr(value, f) for f in fields}


async def export_account(user) -> dict:
    where = {"userId": user.id}

    def rows(found, **nested) -> list[dict]:
        return [row(i, **nested) for i in found]

    tagged = {"tags": _brief(("id", "name"))}
    documents = []
    for d in await db.document.find_many(where=where):
        doc = row(d)
        doc.pop("documentNumberEnc", None)
        doc.pop("notesEnc", None)
        documents.append({**doc, "documentNumber": _try_decrypt(d.documentNumberEnc), "notes": _try_decrypt(d.notesEnc)})
    health = await db.healthprofile.find_unique(where={"userId": user.id})
    settings = await ensure_os_profile(user)
    data = {
        "categories": rows(await db.category.find_many(where=where, order=[{"scope": "asc"}, {"sortOrder": "asc"}])),
        "tags": rows(await db.tag.find_many(where=where, order={"name": "asc"})),
        "exchangeRates": rows(await db.exchangerate.find_many(where=where, order={"currency": "asc"})),
        "tasks": rows(await db.task.find_many(where=where, include={"tags": True}, order={"createdAt": "asc"}), **tagged),
        "events": rows(await db.event.find_many(where=where, order={"startAt": "asc"})),
        "projects": rows(await db.project.find_many(where=where, order={"createdAt": "asc"})),
        "attachments": [{f: getattr(a, f) for f in ("id", "entityType", "entityId", "fileName", "mimeType", "size", "createdAt")}
                        for a in await db.attachment.find_many(where=where, order={"createdAt": "asc"})],
        "accounts": rows(await db.account.find_many(where=where, order={"sortOrder": "asc"})),
        "transactions": rows(await db.transaction.find_many(where=where, order={"date": "asc"})),
        "budgets": rows(await db.budget.find_many(where=where)),
        "recurringPayments": rows(await db.recurringpayment.find_many(where=where, order={"nextDate": "asc"})),
        "goals": rows(await db.goal.find_many(where=where, include={"milestones": {"order_by": {"sortOrder": "asc"}}}), milestones=row),
        "habits": rows(await db.habit.find_many(where=where, order={"sortOrder": "asc"})),
        "habitLogs": rows(await db.habitlog.find_many(where=where, order={"date": "asc"})),
        "maintenanceItems": rows(await db.maintenanceitem.find_many(where=where)),
        "maintenanceLogs": rows(await db.maintenancelog.find_many(where=where, order={"doneAt": "asc"})),
        "documents": documents,
        "shoppingLists": rows(await db.shoppinglist.find_many(where=where, order={"sortOrder": "asc"})),
        "shoppingItems": rows(await db.shoppingitem.find_many(where=where, order={"sortOrder": "asc"})),
        "recipes": rows(await db.recipe.find_many(where=where, include={"ingredients": {"order_by": {"sortOrder": "asc"}}}), ingredients=row),
        "mealPlanEntries": rows(await db.mealplanentry.find_many(where=where, order={"date": "asc"})),
        "notes": rows(await db.note.find_many(where=where, include={"tags": True}, order={"createdAt": "asc"}), **tagged),
        "courses": rows(await db.course.find_many(where=where)),
        "assessments": rows(await db.assessment.find_many(where=where)),
        "studySessions": rows(await db.studysession.find_many(where=where, order={"startAt": "asc"})),
        "trips": rows(await db.trip.find_many(where=where, include={"items": {"order_by": {"sortOrder": "asc"}}, "packingItems": {"order_by": {"sortOrder": "asc"}}},
                                              order={"startDate": "asc"}), items=row, packingItems=row),
        "people": rows(await db.person.find_many(where=where, include={"giftIdeas": True}), giftIdeas=row),
        "importantDates": rows(await db.importantdate.find_many(where=where)),
        "reminders": rows(await db.reminder.find_many(where=where, order={"remindAt": "asc"})),
        "notifications": rows(await db.notification.find_many(where=where, order={"createdAt": "asc"})),
        "activityLogs": rows(await db.activitylog.find_many(where=where, order={"createdAt": "asc"})),
        "dayLogs": rows(await db.daylog.find_many(where=where, order={"date": "asc"})),
        "weekReviews": rows(await db.weekreview.find_many(where=where, order={"weekStart": "asc"})),
        "debts": rows(await db.debt.find_many(where=where, order={"createdAt": "asc"})),
        "debtPayments": rows(await db.debtpayment.find_many(where=where, order={"date": "asc"})),
        "healthProfile": row(health) if health else None,
        "foodItems": rows(await db.fooditem.find_many(where=where, order={"name": "asc"})),
        "foodLogs": rows(await db.foodlog.find_many(where=where, order={"date": "asc"})),
        "exerciseLogs": rows(await db.exerciselog.find_many(where=where, order={"date": "asc"})),
        "weightLogs": rows(await db.weightlog.find_many(where=where, order={"date": "asc"})),
        "waterLogs": rows(await db.waterlog.find_many(where=where, order={"date": "asc"})),
        "challenges": rows(await db.challenge.find_many(where=where, include={"habits": True}, order={"createdAt": "asc"}), habits=row),
        "workoutPlans": rows(await db.workoutplan.find_many(where=where, include={"days": {"include": {"exercises": True}}}, order={"createdAt": "asc"}),
                             days=lambda d: row(d, exercises=row)),
        "workoutSessions": rows(await db.workoutsession.find_many(where=where, include={"sets": True}, order={"date": "asc"}), sets=row),
        # the app's journal (vournal OS had none)
        "journalEntries": rows(await db.entry.find_many(where=where, order={"createdAt": "asc"})),
        "appTodos": rows(await db.todo.find_many(where=where, order={"createdAt": "asc"})),
        "topics": rows(await db.topic.find_many(where=where)),
        "places": rows(await db.place.find_many(where=where)),
    }
    return {
        "exportedAt": datetime.now(timezone.utc),
        "formatVersion": 1,
        "user": web_user_out(user, settings),
        "settings": settings_view(settings),
        "data": data,
    }
