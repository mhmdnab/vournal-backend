"""A copy of what deleting an item removes, so the assistant's Undo can bring it back exactly
(design 2026-11-06): the row, every row the database deletes with it (ON DELETE CASCADE, followed
down), and the links it clears on other rows (ON DELETE SET NULL). The rules come from the
database's own foreign keys (pg_constraint), so the copy matches what the delete really does.

Rows are copied with row_to_json and restored with json_populate_record under their own ids, so
links keep working and encrypted columns travel exactly as stored, never decrypted. Restoring is
one transaction: if something now clashes (an id or a unique value reused), nothing changes."""

import json
import re
from datetime import timezone

from prisma.errors import PrismaError

from app.db import db
from app.os.assistant_tools import ToolError

# deletable kinds (assistant_tools.remove) → table
MODELS = {"task": "Task", "event": "Event", "reminder": "Reminder", "transaction": "Transaction", "debt": "Debt", "note": "Note",
          "shopping_item": "ShoppingItem", "food_log": "FoodLog", "weight": "WeightLog"}
# Attachment.entityType for a table, where it differs from the table name in SCREAMING_SNAKE
_ENTITY_TYPE = {"MaintenanceItem": "MAINTENANCE"}

_fks: dict[str, list[tuple[str, str, str]]] | None = None  # parent → [(child, column, "c" | "n")]
_columns: dict[str, set[str]] = {}


async def _graph() -> dict[str, list[tuple[str, str, str]]]:
    global _fks
    if _fks is None:
        rows = await db.query_raw(
            "SELECT c.conrelid::regclass::text AS child, a.attname AS col, c.confrelid::regclass::text AS parent, c.confdeltype AS rule "
            "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.contype = 'f' AND c.confdeltype IN ('c', 'n')")
        cols = await db.query_raw("SELECT table_name AS t, column_name AS c FROM information_schema.columns WHERE table_schema = current_schema()")
        for r in cols:
            _columns.setdefault(r["t"], set()).add(r["c"])
        graph: dict[str, list[tuple[str, str, str]]] = {}
        for r in rows:
            graph.setdefault(r["parent"].strip('"'), []).append((r["child"].strip('"'), r["col"], r["rule"]))
        _fks = graph
    return _fks


def _row(value) -> dict:
    return value if isinstance(value, dict) else json.loads(value)


async def _select(table: str, column: str, ids: list[str]) -> list[dict]:
    rows = await db.query_raw(
        f'SELECT row_to_json(t) AS r FROM "{table}" t WHERE t."{column}" IN (SELECT json_array_elements_text($1::json))', json.dumps(ids))
    return [_row(r["r"]) for r in rows]


def _entity_type(table: str) -> str:
    return _ENTITY_TYPE.get(table) or re.sub(r"(?<!^)(?=[A-Z])", "_", table).upper()


async def take(model: str, id: str, user_id: str) -> dict:
    """{"rows": [{"model", "row"}] parent first, "relinks": [{"model", "id", "column", "value"}],
    "count": items the user would lose (join-table rows aside), "has_file": an attachment is among them}."""
    graph = await _graph()
    owned = "userId" in _columns.get(model, set())
    root = await db.query_raw(f'SELECT row_to_json(t) AS r FROM "{model}" t WHERE t."id" = $1' + (' AND t."userId" = $2' if owned else ""),
                              id, *([user_id] if owned else []))
    if not root:
        raise ToolError("Not found")
    rows = [{"model": model, "row": _row(root[0]["r"])}]
    seen = {(model, id)}
    relinks: list[dict] = []
    frontier = [(model, [id])]
    while frontier:
        upcoming = []
        for parent, ids in frontier:
            for child, column, rule in graph.get(parent, []):
                has_id = "id" in _columns.get(child, set())
                found = await _select(child, column, ids)
                if rule == "n":
                    relinks += [{"model": child, "id": r["id"], "column": column, "value": r[column]} for r in found if has_id]
                    continue
                new_ids = []
                for r in found:
                    if has_id:
                        if (child, r["id"]) in seen:
                            continue
                        seen.add((child, r["id"]))
                        new_ids.append(r["id"])
                    rows.append({"model": child, "row": r})
                if new_ids:
                    upcoming.append((child, new_ids))
        frontier = upcoming
    relinks = [x for x in relinks if (x["model"], x["id"]) not in seen]  # deleted with it: restored as rows
    by_type: dict[str, list[str]] = {}
    for r in rows:
        if "id" in r["row"]:
            by_type.setdefault(_entity_type(r["model"]), []).append(r["row"]["id"])
    has_file = False
    for entity_type, ids in by_type.items():
        found = await db.query_raw(
            'SELECT 1 AS ok FROM "Attachment" WHERE "userId" = $1 AND "entityType"::text = $2 '
            'AND "entityId" IN (SELECT json_array_elements_text($3::json)) LIMIT 1', user_id, entity_type, json.dumps(ids))
        has_file = has_file or bool(found)
    return {"rows": rows, "relinks": relinks, "count": sum(1 for r in rows if not r["model"].startswith("_")), "has_file": has_file}


async def restore(snapshot: dict) -> None:
    """Puts every copied row back under its own id, then the cleared links where they're still
    empty. All or nothing: a clash raises ToolError and changes nothing."""
    try:
        async with db.tx() as tx:
            for r in snapshot["rows"]:
                await tx.execute_raw(f'INSERT INTO "{r["model"]}" SELECT * FROM json_populate_record(NULL::"{r["model"]}", $1::json)',
                                     json.dumps(r["row"]))
            for x in snapshot["relinks"]:
                await tx.execute_raw(f'UPDATE "{x["model"]}" SET "{x["column"]}" = $1 WHERE "id" = $2 AND "{x["column"]}" IS NULL',
                                     x["value"], x["id"])
    except PrismaError as exc:
        raise ToolError("Something with the same details exists now — not undone") from exc


async def clear_old(older_than) -> int:
    """Drops undo copies past their 24 hours (they can hold whole deleted items)."""
    return await db.execute_raw('UPDATE "AssistantAction" SET "undo" = NULL WHERE "undo" IS NOT NULL AND "createdAt" < $1::timestamp',
                                older_than.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f"))
