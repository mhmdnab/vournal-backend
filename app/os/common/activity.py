"""The append-only activity stream (port of vournalOS ActivityService).

NestJS logged every domain event (`<entity>.<verb>`) through a wildcard listener;
here each section calls log_activity() where NestJS emitted the event. Best-effort:
it never raises, so a failed log never fails the request that caused it.
"""

import json
import logging

from prisma import Json

from app.db import db
from app.os.common.serialize import to_json

log = logging.getLogger("vournal.os")

# Event-name prefix → EntityType (constants/events.ts).
EVENT_PREFIX_ENTITY: dict[str, str] = {
    "task": "TASK",
    "event": "EVENT",
    "project": "PROJECT",
    "transaction": "TRANSACTION",
    "recurring": "RECURRING_PAYMENT",
    "recurringPayment": "RECURRING_PAYMENT",
    "subscription": "RECURRING_PAYMENT",
    "budget": "BUDGET",
    "goal": "GOAL",
    "milestone": "GOAL",
    "habit": "HABIT",
    "maintenance": "MAINTENANCE",
    "document": "DOCUMENT",
    "shoppingList": "SHOPPING_LIST",
    "shoppingItem": "SHOPPING_ITEM",
    "shopping": "SHOPPING_ITEM",
    "meal": "MEAL",
    "mealPlan": "MEAL",
    "recipe": "RECIPE",
    "note": "NOTE",
    "course": "COURSE",
    "assessment": "ASSESSMENT",
    "studySession": "COURSE",
    "trip": "TRIP",
    "person": "PERSON",
    "importantDate": "IMPORTANT_DATE",
    "reminder": "REMINDER",
    "debt": "DEBT",
}
_ENTITY_TYPES = set(EVENT_PREFIX_ENTITY.values())
_MAX_METADATA = 8000


def _metadata(fields: dict):
    if not fields:
        return None
    safe = to_json(fields)
    return {"truncated": True} if len(json.dumps(safe, ensure_ascii=False)) > _MAX_METADATA else safe


async def log_activity(
    user_id: str,
    action: str,
    *,
    entity_id: str | None = None,
    entity_type: str | None = None,
    **metadata,
) -> None:
    if not user_id or not action or action.startswith("system."):
        return
    kind = entity_type if entity_type in _ENTITY_TYPES else EVENT_PREFIX_ENTITY.get(action.split(".")[0])
    data = {"userId": user_id, "action": action[:100], "entityType": kind, "entityId": entity_id}
    meta = _metadata(metadata)
    if meta is not None:
        data["metadata"] = Json(meta)
    try:
        await db.activitylog.create(data=data)
    except Exception as exc:  # noqa: BLE001 — e.g. the user was deleted meanwhile
        log.debug("activity log skipped (%s): %s", action, exc)
