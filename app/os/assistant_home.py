"""The assistant's document and home maintenance tools (design 2026-11-18, the assistant in every
section). A document's number and notes never pass through here: the tools take names, types and
dates only, and every result is scrubbed (owner decision 2026-11-18). Maintenance marked done
with a cost also adds the expense to Finance; its Undo removes the log entry and that expense, and
puts the item's dates back when no later "done" has moved them since."""

from app.db import db
from app.os import assistant_sections, documents, maintenance
from app.os import assistant_tools as t
from app.os.assistant_sections import scrub_document
from app.os.assistant_tools import Kind, ToolError
from app.os.common import dates
from app.os.common.serialize import to_json

DOCUMENT_TEXT = (("name", "name"), ("issuer", "issuer"), ("holder_name", "holderName"))
DOCUMENT_DATES = (("issue_date", "issueDate"), ("expiry_date", "expiryDate"))
DONE_STATE = ("lastDoneAt", "nextDueAt", "isActive")


def _int(v) -> bool:
    return t._number(v) and float(v).is_integer()


def _day(v) -> str | None:
    return to_json(v)[:10] if v else None


def _document_plain(i: dict) -> dict:
    """Only these fields: anything else the model sends (a number, notes) is dropped here."""
    plain: dict = {}
    for field, key in DOCUMENT_TEXT:
        if isinstance(i.get(field), str):
            plain[key] = i[field].strip() or None
    for field, key in DOCUMENT_DATES:
        if t.opt_date(i.get(field)):
            plain[key] = t.opt_date(i[field])
    if i.get("type") in documents.TYPES:
        plain["type"] = i["type"]
    if _int(i.get("remind_days_before")):
        plain["reminderDaysBefore"] = int(i["remind_days_before"])
    return plain


def _document_result(d: dict) -> dict:
    d = scrub_document(d)
    return {"id": d["id"], "name": d["name"], "type": d["type"], "expiryDate": _day(d.get("expiryDate")), "status": d.get("status")}


async def _add_document(i: dict, ctx) -> dict:
    plain = {**_document_plain(i), "name": t._req_str(i.get("name"), "name")}
    d = await documents.create(t._check(documents.CreateBody, plain), user=ctx.user)
    return t._done({"kind": "create", "entityType": "document", "entityId": d["id"], "label": d["name"], "link": f"/documents?id={d['id']}",
                    "undo": {"op": "delete", "type": "document", "id": d["id"]}}, _document_result(d))


async def _update_document(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    plain = _document_plain(i)
    if not plain:
        raise ToolError("Nothing to change")
    before = await t.fields_now("document", id, plain, ctx)
    d = await documents.update(id, t._check(documents.UpdateBody, plain), user=ctx.user)
    return t._done({"kind": "update", "entityType": "document", "entityId": id, "label": d["name"], "link": f"/documents?id={id}",
                    "undo": await t._fields_undo("document", id, before, ctx)}, _document_result(d))


def _item_plain(i: dict) -> dict:
    plain: dict = {}
    if t._text(i, "title"):
        plain["title"] = t._text(i, "title")
    if i.get("area") in maintenance.AREAS:
        plain["area"] = i["area"]
    repeat = t.read_repeat(i.get("repeat"))
    if repeat:
        plain["rrule"] = repeat["rrule"]
    if t.opt_date(i.get("next_due")):
        plain["nextDueAt"] = t.opt_date(i["next_due"])
    if t._number(i.get("estimated_cost")):
        plain["estimatedCost"] = i["estimated_cost"]
    return plain


def _item_result(m: dict) -> dict:
    return {"id": m["id"], "title": m["title"], "area": m["area"], "nextDueAt": _day(m["nextDueAt"]), "lastDoneAt": _day(m.get("lastDoneAt"))}


async def _add_maintenance_item(i: dict, ctx) -> dict:
    plain = {**_item_plain(i), "title": t._req_str(i.get("title"), "title")}
    if "rrule" not in plain:
        raise ToolError('"repeat" is required: how often it is done, e.g. { every: "year" }')
    if t._text(i, "currency"):
        plain["currency"] = t._text(i, "currency").upper()
    if _int(i.get("remind_days_before")):
        plain["reminderDaysBefore"] = int(i["remind_days_before"])
    if t._text(i, "notes"):
        plain["notes"] = t._text(i, "notes")
    m = await maintenance.create(t._check(maintenance.ItemBody, plain), user=ctx.user)
    return t._done({"kind": "create", "entityType": "maintenance_item", "entityId": m["id"], "label": m["title"], "link": f"/maintenance?id={m['id']}",
                    "undo": {"op": "delete", "type": "maintenance_item", "id": m["id"]}}, _item_result(m))


async def _update_maintenance_item(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    plain = _item_plain(i)
    if isinstance(i.get("active"), bool):
        plain["isActive"] = i["active"]
    if i.get("done") is True:
        if plain:
            raise ToolError("Mark it done alone, or change fields — not both")
        return await _done_item(id, i, ctx)
    if not plain:
        raise ToolError("Nothing to change")
    before = await t.fields_now("maintenance_item", id, plain, ctx)
    m = await maintenance.update(id, t._check(maintenance.ItemUpdateBody, plain), user=ctx.user)
    return t._done({"kind": "update", "entityType": "maintenance_item", "entityId": id, "label": m["title"], "link": f"/maintenance?id={id}",
                    "undo": await t._fields_undo("maintenance_item", id, before, ctx)}, _item_result(m))


async def _done_item(id: str, i: dict, ctx) -> dict:
    plain: dict = {}
    if t.opt_date(i.get("date")):
        plain["date"] = t.opt_date(i["date"])
    if t._number(i.get("cost")):
        plain["cost"] = i["cost"]
        plain["logExpense"] = i["cost"] > 0  # the expense goes to Finance too (owner 2026-11-18)
    if t._text(i, "notes"):
        plain["notes"] = t._text(i, "notes")
    before = await t.fields_now("maintenance_item", id, DONE_STATE, ctx)
    out = await maintenance.mark_done(id, t._check(maintenance.DoneBody, plain), user=ctx.user)
    m, log = out["item"], out["log"]
    expense = log["transaction"]
    undo = {"op": "undo_maintenance_done", "itemId": id, "logId": log["id"], "transactionId": expense["id"] if expense else None,
            "before": before, "after": await t.fields_now("maintenance_item", id, DONE_STATE, ctx)}
    result = {**_item_result(m), "doneOn": _day(log["doneAt"])}
    if expense:
        result["expenseAdded"] = {"amount": float(expense["amount"]), "currency": expense["currency"],
                                  "note": "Already in Finance: don't add it again."}
    return t._done({"kind": "complete", "entityType": "maintenance_item", "entityId": id, "label": m["title"], "link": f"/maintenance?id={id}",
                    "undo": undo}, result)


async def undo_done(user, undo: dict) -> None:
    """Removes that log entry and its expense; the item's dates go back only if they're still the
    ones that "done" left (a later one moved them on otherwise)."""
    log = await db.maintenancelog.find_first(where={"id": undo["logId"], "userId": user.id})
    item = await db.maintenanceitem.find_first(where={"id": undo["itemId"], "userId": user.id})
    if log is None or item is None:
        raise ToolError("It was deleted since — nothing to undo")
    now = {"lastDoneAt": _day(item.lastDoneAt), "nextDueAt": _day(item.nextDueAt), "isActive": item.isActive}
    before = undo["before"]
    async with db.tx() as tx:
        await tx.maintenancelog.delete(where={"id": log.id})
        if undo.get("transactionId"):
            await tx.transaction.delete_many(where={"id": undo["transactionId"], "userId": user.id})
        if now == undo["after"]:
            await tx.maintenanceitem.update(where={"id": item.id}, data={
                "lastDoneAt": dates.parse_date_only(before["lastDoneAt"]), "nextDueAt": dates.parse_date_only(before["nextDueAt"]),
                "isActive": before["isActive"]})


# ----- item types --------------------------------------------------------------------------


async def _restore_document(ctx, id: str, before: dict) -> None:
    await documents.update(id, t._check(documents.UpdateBody, before), user=ctx.user)


async def _restore_item(ctx, id: str, before: dict) -> None:
    await maintenance.update(id, t._check(maintenance.ItemUpdateBody, before), user=ctx.user)


KINDS = {
    "document": Kind(model="Document", find=documents._owned, label=lambda r: r.name, link=lambda r: f"/documents?id={r.id}",
                     delete=documents.remove, restore=_restore_document, date_only=frozenset({"issueDate", "expiryDate"})),
    "maintenance_item": Kind(model="MaintenanceItem", find=maintenance._find, label=lambda r: r.title, link=lambda r: f"/maintenance?id={r.id}",
                             delete=maintenance.remove, restore=_restore_item, date_only=frozenset({"nextDueAt", "lastDoneAt"})),
}
TOOLS = {"add_document": _add_document, "update_document": _update_document, "add_maintenance_item": _add_maintenance_item,
         "update_maintenance_item": _update_maintenance_item}

t.register(TOOLS, KINDS)
assistant_sections.SECTION_OF.update({"add_document": "documents", "update_document": "documents",
                                      "add_maintenance_item": "maintenance", "update_maintenance_item": "maintenance"})
