"""Manual mode (design 2026-11-06): a change the model asks for waits as a pending action that
holds the tool call, and runs through the same tool code when the user approves it. The input is
checked against the tool's own schema first, so obvious mistakes go straight back to the model;
the deeper checks run on approval, and a failure there marks the card failed with the reason.

A later step can refer to a waiting item by its placeholder id, "pending:<actionId>"; approving
swaps in the real id, in the order the changes were made."""

import json
import re
import uuid
from pathlib import Path

from app.db import db
from app.os import assistant_tools as tools
from app.os.assistant_tools import Ctx, ToolError

PLACEHOLDER = "pending:"
SCHEMAS = {t["name"]: t["input_schema"] for t in json.loads((Path(__file__).parent / "data" / "assistant.json").read_text())["tools"]}

_JSON_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}

# what each write tool changes, for the card (delete_item says it in its input)
ENTITY = {
    "create_task": "task", "update_task": "task", "create_event": "event", "update_event": "event",
    "create_reminder": "reminder", "add_transaction": "transaction", "update_transaction": "transaction",
    "create_debt": "debt", "add_debt_payment": "debt_payment", "add_note": "note", "add_shopping_item": "shopping_item",
    "log_food": "food_log", "log_water": "water", "log_weight": "weight", "update_health_profile": "health_profile",
}
# the item an edit or payment points at: (input field, kind for describe)
_TARGET = {"update_task": ("id", "task"), "update_event": ("id", "event"), "update_transaction": ("id", "transaction"),
           "add_debt_payment": ("debt_id", "debt")}


def check_input(schema: dict, value: dict) -> str | None:
    """The first way `value` breaks the tool's input schema (required fields, JSON types, enums,
    unknown fields), or None."""
    props = schema.get("properties", {})
    for field in schema.get("required", []):
        if value.get(field) in (None, ""):
            return f"{field} is required"
    for field, v in value.items():
        spec = props.get(field)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"{field} is not a field of this tool"
            continue
        kind = spec.get("type")
        if kind in ("number", "integer"):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and (kind == "number" or float(v).is_integer())
        elif kind in _JSON_TYPES:
            ok = isinstance(v, _JSON_TYPES[kind])
        else:
            ok = True
        if not ok:
            return f"{field} must be a {kind}"
        if "enum" in spec and v not in spec["enum"]:
            return f"{field} must be one of {', '.join(map(str, spec['enum']))}"
    return None


def pending_kind(name: str, i: dict) -> str:
    if name == "delete_item":
        return "delete"
    if name == "update_task" and i.get("status") == "done":
        return "complete"
    return "update" if name.startswith("update_") else "create"


def pending_label(name: str, i: dict) -> str:
    for field in ("title", "name", "text", "who"):
        if isinstance(i.get(field), str) and i[field].strip():
            return i[field].strip()
    if isinstance(i.get("amount"), (int, float)):
        return " ".join(str(x) for x in (i["amount"], i.get("currency")) if x)
    if isinstance(i.get("ml"), (int, float)):
        return f"{i['ml']} ml"
    if isinstance(i.get("kg"), (int, float)):
        return f"{i['kg']} kg"
    return name.replace("_", " ")


async def _waiting_label(ctx: Ctx, placeholder: str) -> str:
    aid = placeholder[len(PLACEHOLDER):]
    if aid in ctx.pending:
        return ctx.pending[aid]["label"]
    a = await db.assistantaction.find_first(where={"id": aid, "userId": ctx.user_id})
    if a is None:
        raise ToolError(f"Unknown id {placeholder}")
    return a.label


async def _describe_target(name: str, i: dict, ctx: Ctx) -> dict:
    """Label and link for the card: the item an edit points at, or what a new one is called."""
    if name == "update_health_profile":
        return {"label": "Health plan", "link": "/health"}
    if name in _TARGET:
        field, kind = _TARGET[name]
        target = i.get(field)
        if isinstance(target, str) and target.startswith(PLACEHOLDER):
            return {"label": await _waiting_label(ctx, target), "link": None}
        if name == "update_task":
            todo = await tools._app_todo(ctx, target) if isinstance(target, str) else None
            if todo is not None:
                return {"label": todo.text, "link": f"/tasks?id={todo.id}"}
        found = await tools.describe(ctx, kind, target)
        if name == "add_debt_payment":
            return {"label": f"{pending_label(name, i)} → {found['label']}", "link": found["link"]}
        return found
    return {"label": pending_label(name, i), "link": None}


async def defer(name: str, i: dict, ctx: Ctx) -> dict:
    """A write in Manual mode: checked, described and saved as a waiting card, not run."""
    problem = check_input(SCHEMAS[name], i)
    if problem:
        return {"content": f"Error: {problem}", "isError": True}
    found = await _describe_target(name, i, ctx)
    aid = uuid.uuid4().hex
    target = _TARGET.get(name, ("id", None))[0]
    entity_id = i.get(target) if name in _TARGET and isinstance(i.get(target), str) else ""
    draft = {"id": aid, "kind": pending_kind(name, i), "entityType": ENTITY[name], "entityId": entity_id, "label": found["label"],
             "link": found["link"], "undo": None, "status": "pending", "call": {"name": name, "input": i}}
    ctx.pending[aid] = draft
    placeholder = f"{PLACEHOLDER}{aid}"
    result = {"ok": False, "pending": True, "id": placeholder,
              "message": f'Waiting for the user to approve: "{found["label"]}". Not done yet. If a later step needs this item, use the id "{placeholder}".'}
    return {"content": tools.js_json(result), "action": draft}


async def resolve_placeholders(value, user_id: str):
    """`value` with every "pending:<actionId>" replaced by that action's real id, once it's done."""
    if isinstance(value, dict):
        return {k: await resolve_placeholders(v, user_id) for k, v in value.items()}
    if isinstance(value, list):
        return [await resolve_placeholders(v, user_id) for v in value]
    if isinstance(value, str) and value.startswith(PLACEHOLDER):
        a = await db.assistantaction.find_first(where={"id": value[len(PLACEHOLDER):], "userId": user_id})
        if a is None:
            raise ToolError(f"Unknown id {value}")
        if a.status != "done" or not a.entityId:
            raise ToolError(f'Depends on "{a.label}", which wasn\'t done')
        return a.entityId
    return value


async def run_approved(action, ctx: Ctx) -> dict:
    """Runs an approved card. {"action": result action} when done, {"error": text} when not."""
    try:
        if action.kind == "delete" and not action.call:
            entity_id = await resolve_placeholders(action.entityId, ctx.user_id)
            await tools.describe(ctx, action.entityType, entity_id)  # still there, and still theirs
            undo = await tools.remove(ctx.user, action.entityType, entity_id)
            return {"action": {"entityId": entity_id, "label": action.label, "link": action.link, "undo": undo}}
        call = action.call if isinstance(action.call, dict) else json.loads(action.call)
        i = await resolve_placeholders(call["input"], ctx.user_id)
    except Exception as exc:  # noqa: BLE001 — the card shows why
        return {"error": tools.error_text(exc).removeprefix("Error: ")}
    out = await tools.run(call["name"], i, ctx)
    if out.get("isError") or not out.get("action"):
        # an error, or a tool that changed nothing (e.g. the app already has that to-do)
        return {"error": re.sub(r" \(id [^)]*\)", "", str(out["content"]).removeprefix("Error: ")).split(" Use ")[0]}
    return {"action": out["action"]}
