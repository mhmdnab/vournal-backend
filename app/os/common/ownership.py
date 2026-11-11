"""Ownership checks for ids the client sends (port of vournalOS OwnershipService).

A row that isn't the user's is "not found" (404), never 403, so its existence
isn't leaked. Keys are prisma-client-py delegate names (`db.<name>`).
"""

from app.db import db
from app.os.common.http import nest_error

LABELS: dict[str, str] = {
    "category": "Category",
    "tag": "Tag",
    "task": "Task",
    "event": "Event",
    "project": "Project",
    "attachment": "Attachment",
    "account": "Account",
    "transaction": "Transaction",
    "budget": "Budget",
    "recurringpayment": "Recurring payment",
    "goal": "Goal",
    "goalmilestone": "Milestone",
    "habit": "Habit",
    "habitlog": "Habit log",
    "maintenanceitem": "Maintenance item",
    "maintenancelog": "Maintenance log",
    "document": "Document",
    "shoppinglist": "Shopping list",
    "shoppingitem": "Shopping item",
    "recipe": "Recipe",
    "recipeingredient": "Ingredient",
    "mealplanentry": "Meal",
    "note": "Note",
    "course": "Course",
    "assessment": "Assessment",
    "studysession": "Study session",
    "trip": "Trip",
    "tripitem": "Trip item",
    "packingitem": "Packing item",
    "person": "Person",
    "giftidea": "Gift idea",
    "importantdate": "Important date",
    "reminder": "Reminder",
    "notification": "Notification",
    "debt": "Debt",
    "debtpayment": "Debt payment",
}

# Models with no userId column: ownership goes through their parent.
_PARENT_SCOPE = {
    "goalmilestone": "goal",
    "recipeingredient": "recipe",
    "tripitem": "trip",
    "packingitem": "trip",
    "giftidea": "person",
}


# The workspace-shared models: an id checked here is a Personal one (workspace items go
# through app.os.common.space).
_SHARED = ("task", "event", "project", "note", "attachment")


def _scope(model: str, user_id: str) -> dict:
    parent = _PARENT_SCOPE.get(model)
    if parent:
        return {parent: {"is": {"userId": user_id}}}
    return {"userId": user_id, "workspaceId": None} if model in _SHARED else {"userId": user_id}


def _not_found(model: str):
    return nest_error(404, f"{LABELS[model]} not found")


async def assert_owned(model: str, id: str | None, user_id: str) -> None:
    if id is None:
        return
    if not isinstance(id, str) or not id:
        raise _not_found(model)
    count = await getattr(db, model).count(where={"id": id, **_scope(model, user_id)})
    if count == 0:
        raise _not_found(model)


async def assert_many_owned(model: str, ids: list[str] | None, user_id: str) -> None:
    if not ids:
        return
    unique = list(dict.fromkeys(ids))
    count = await getattr(db, model).count(where={"id": {"in": unique}, **_scope(model, user_id)})
    if count != len(unique):
        raise _not_found(model)


async def owns(model: str, id: str, user_id: str) -> bool:
    return await getattr(db, model).count(where={"id": id, **_scope(model, user_id)}) > 0


# Polymorphic links (attachments, reminders, notifications): EntityType → model.
ENTITY_TYPE_MODEL: dict[str, str] = {
    "TASK": "task",
    "EVENT": "event",
    "PROJECT": "project",
    "TRANSACTION": "transaction",
    "RECURRING_PAYMENT": "recurringpayment",
    "BUDGET": "budget",
    "GOAL": "goal",
    "HABIT": "habit",
    "MAINTENANCE": "maintenanceitem",
    "DOCUMENT": "document",
    "SHOPPING_LIST": "shoppinglist",
    "SHOPPING_ITEM": "shoppingitem",
    "MEAL": "mealplanentry",
    "RECIPE": "recipe",
    "NOTE": "note",
    "COURSE": "course",
    "ASSESSMENT": "assessment",
    "TRIP": "trip",
    "PERSON": "person",
    "IMPORTANT_DATE": "importantdate",
    "REMINDER": "reminder",
    "DEBT": "debt",
}


async def assert_entity(entity_type: str | None, entity_id: str | None, user_id: str) -> None:
    """For (entityType, entityId) pairs: both empty → fine; only one → 400."""
    if not entity_type and not entity_id:
        return
    if not entity_type or not entity_id:
        raise nest_error(400, "entityType and entityId must be provided together")
    model = ENTITY_TYPE_MODEL.get(entity_type)
    if model is None:
        raise nest_error(400, "Unsupported entityType")
    await assert_owned(model, entity_id, user_id)
