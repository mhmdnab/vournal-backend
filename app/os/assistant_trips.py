"""The assistant's trip, itinerary and packing tools (design 2026-11-18, the assistant in every
section). Trips have no workspaces, so they are the user's own by construction (ruling R3)."""

import re

from app.db import db
from app.os import assistant_sections, trips
from app.os import assistant_tools as t
from app.os.assistant_tools import Kind, ToolError
from app.os.calendar_utils import local_date_time
from app.os.common.http import nest_error
from app.os.common.serialize import to_json

TRIP_TEXT = (("name", "name"), ("destination", "destination"), ("transport", "transportation"), ("accommodation", "accommodation"),
             ("notes", "notes"))
ITINERARY_ONLY = ("type", "start", "location", "cost", "currency", "confirmation_code")
PACKING_ONLY = ("quantity", "group")
LOCAL = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:T([01][0-9]|2[0-3]):([0-5][0-9]))?")
NOON = "12:00"  # a day without a time: midday local, so its local day is right in any zone


def _day(v) -> str | None:
    return to_json(v)[:10] if v else None


def _trip_link(id: str) -> str:
    return f"/travel?id={id}"


def _trip_plain(i: dict) -> dict:
    plain: dict = {}
    for field, key in TRIP_TEXT:
        if t._text(i, field):
            plain[key] = t._text(i, field)
    for field, key in (("start_date", "startDate"), ("end_date", "endDate")):
        if t.opt_date(i.get(field)):
            plain[key] = t.opt_date(i[field])
    if t._number(i.get("budget")):
        plain["budget"] = i["budget"]
    if t._text(i, "currency"):
        plain["currency"] = t._text(i, "currency").upper()
    return plain


def _trip_result(row) -> dict:
    return {"id": row.id, "name": row.name, "destination": row.destination, "startDate": _day(row.startDate), "endDate": _day(row.endDate),
            "status": row.status}


async def _create_trip(i: dict, ctx) -> dict:
    plain = {**_trip_plain(i), "name": t._req_str(i.get("name"), "name")}
    v = await trips.create_trip(t._check(trips.TripBody, plain), user=ctx.user)
    row = await trips.find_trip(ctx.user_id, v["id"])
    return t._done({"kind": "create", "entityType": "trip", "entityId": row.id, "label": row.name, "link": _trip_link(row.id),
                    "undo": {"op": "delete", "type": "trip", "id": row.id}}, _trip_result(row))


async def _update_trip(i: dict, ctx) -> dict:
    id = t._req_str(i.get("id"), "id")
    plain = _trip_plain(i)
    if i.get("status") in trips.TRIP_STATUSES:
        plain["status"] = i["status"]
    if not plain:
        raise ToolError("Nothing to change")
    before = await t.fields_now("trip", id, plain, ctx)
    await trips.update_trip(id, t._check(trips.TripUpdateBody, plain), user=ctx.user)
    row = await trips.find_trip(ctx.user_id, id)
    return t._done({"kind": "complete" if plain.get("status") == "COMPLETED" else "update", "entityType": "trip", "entityId": id,
                    "label": row.name, "link": _trip_link(id), "undo": await t._fields_undo("trip", id, before, ctx)}, _trip_result(row))


def _list(i: dict) -> str:
    kind = i.get("list")
    if kind not in ("itinerary", "packing"):
        raise ToolError('"list" must be itinerary or packing')
    wrong = PACKING_ONLY if kind == "itinerary" else ITINERARY_ONLY
    for field in wrong:
        if i.get(field) is not None:
            raise ToolError(f'"{field}" is not a field of a{"n itinerary" if kind == "itinerary" else " packing"} item')
    return kind


def _start(v, ctx) -> str | None:
    m = LOCAL.fullmatch(v) if isinstance(v, str) else None
    if not m or not t.opt_date(m.group(1)):
        if v is not None:
            raise ToolError('"start" must be YYYY-MM-DD or YYYY-MM-DDTHH:MM')
        return None
    return t._iso(local_date_time(m.group(1), f"{m.group(2)}:{m.group(3)}" if m.group(2) else NOON, ctx.tz))


def _item_plain(i: dict, kind: str, ctx) -> dict:
    plain: dict = {}
    title = t._text(i, "title")
    if kind == "itinerary":
        if title:
            plain["title"] = title
        if i.get("type") in trips.ITEM_TYPES:
            plain["type"] = i["type"]
        if i.get("start") is not None:
            plain["startAt"] = _start(i["start"], ctx)
        for field, key in (("location", "location"), ("confirmation_code", "confirmationCode")):
            if t._text(i, field):
                plain[key] = t._text(i, field)
        if t._number(i.get("cost")):
            plain["cost"] = i["cost"]
        if t._text(i, "currency"):
            plain["currency"] = t._text(i, "currency").upper()
    else:
        if title:
            plain["name"] = title
        if t._number(i.get("quantity")) and float(i["quantity"]).is_integer():
            plain["quantity"] = int(i["quantity"])
        if t._text(i, "group"):
            plain["group"] = t._text(i, "group")
    return plain


async def _add_trip_item(i: dict, ctx) -> dict:
    trip_id = t._req_str(i.get("trip_id"), "trip_id")
    kind = _list(i)
    t._req_str(i.get("title"), "title")
    plain = _item_plain(i, kind, ctx)
    if kind == "itinerary":
        item = await trips.create_item(trip_id, t._check(trips.ItemBody, plain), user=ctx.user)
        entity, label = "trip_item", item["title"]
    else:
        item = await trips.create_packing(trip_id, t._check(trips.PackingBody, plain), user=ctx.user)
        entity, label = "packing_item", item["name"]
    return t._done({"kind": "create", "entityType": entity, "entityId": item["id"], "label": label, "link": f"/travel/{trip_id}",
                    "undo": {"op": "delete", "type": entity, "id": item["id"]}}, {"id": item["id"], "tripId": trip_id, "title": label})


async def _update_trip_item(i: dict, ctx) -> dict:
    trip_id = t._req_str(i.get("trip_id"), "trip_id")
    id = t._req_str(i.get("id"), "id")
    kind = _list(i)
    plain = _item_plain(i, kind, ctx)
    done = i.get("done") if isinstance(i.get("done"), bool) else None
    entity = "trip_item" if kind == "itinerary" else "packing_item"
    if done is not None:
        plain["isDone" if kind == "itinerary" else "isPacked"] = done
    if not plain:
        raise ToolError("Nothing to change")
    row = await KINDS[entity].find(ctx.user_id, id)
    if row.tripId != trip_id:
        raise nest_error(404, "Trip item not found")
    before = await t.fields_now(entity, id, plain, ctx)
    if kind == "itinerary":
        item = await trips.update_item(trip_id, id, t._check(trips.ItemUpdateBody, plain), user=ctx.user)
        label = item["title"]
    else:
        item = await trips.update_packing(trip_id, id, t._check(trips.PackingUpdateBody, plain), user=ctx.user)
        label = item["name"]
    return t._done({"kind": "complete" if done else "update", "entityType": entity, "entityId": id, "label": label, "link": f"/travel/{trip_id}",
                    "undo": await t._fields_undo(entity, id, before, ctx)},
                   {"id": id, "tripId": trip_id, "title": label, "done": item.get("isDone", item.get("isPacked"))})


# ----- item types --------------------------------------------------------------------------


async def _item_row(user_id: str, id: str):
    row = await db.tripitem.find_first(where={"id": id, "trip": {"is": {"userId": user_id}}})
    if row is None:
        raise nest_error(404, "Trip item not found")
    return row


async def _packing_row(user_id: str, id: str):
    row = await db.packingitem.find_first(where={"id": id, "trip": {"is": {"userId": user_id}}})
    if row is None:
        raise nest_error(404, "Packing item not found")
    return row


async def _restore_trip(ctx, id: str, before: dict) -> None:
    await trips.update_trip(id, t._check(trips.TripUpdateBody, before), user=ctx.user)


async def _restore_item(ctx, id: str, before: dict) -> None:
    row = await _item_row(ctx.user_id, id)
    await trips.update_item(row.tripId, id, t._check(trips.ItemUpdateBody, before), user=ctx.user)


async def _restore_packing(ctx, id: str, before: dict) -> None:
    row = await _packing_row(ctx.user_id, id)
    await trips.update_packing(row.tripId, id, t._check(trips.PackingUpdateBody, before), user=ctx.user)


async def _delete_item(id: str, user):
    row = await _item_row(user.id, id)
    return await trips.remove_item(row.tripId, id, user=user)


async def _delete_packing(id: str, user):
    row = await _packing_row(user.id, id)
    return await trips.remove_packing(row.tripId, id, user=user)


KINDS = {
    "trip": Kind(model="Trip", find=trips.find_trip, label=lambda r: r.name, link=lambda r: _trip_link(r.id), delete=trips.remove_trip,
                 restore=_restore_trip, date_only=frozenset({"startDate", "endDate"})),
    "trip_item": Kind(model="TripItem", find=_item_row, label=lambda r: r.title, link=lambda r: f"/travel/{r.tripId}", delete=_delete_item,
                      restore=_restore_item),
    "packing_item": Kind(model="PackingItem", find=_packing_row, label=lambda r: r.name, link=lambda r: f"/travel/{r.tripId}",
                         delete=_delete_packing, restore=_restore_packing),
}
TOOLS = {"create_trip": _create_trip, "update_trip": _update_trip, "add_trip_item": _add_trip_item, "update_trip_item": _update_trip_item}

t.register(TOOLS, KINDS)
assistant_sections.SECTION_OF.update({name: "travel" for name in TOOLS})
