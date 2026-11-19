"""The assistant's rituals tool (design 2026-11-18, the assistant in every section): plan and review
the day, set the week's priorities and review it. Writing is always allowed; the reflective text
(the day's intention, win, mood and note; the week's "went well" and "improve") comes back to the
model only with the journal permission (ruling R5). Undo puts the row back exactly as it was
(the routes stamp the time of a plan or review, so they can't restore an old one)."""

from datetime import datetime

from app.db import db
from app.os import reports, rituals
from app.os import assistant_tools as t
from app.os.assistant_tools import ToolError
from app.os.common.serialize import to_json
from app.os.insight_time import key_to_date

DAY_FIELDS = {"plan", "review", "intention", "win", "mood", "note"}
WEEK_FIELDS = {"priorities", "went_well", "improve", "done"}
DAY_COLUMNS = ("plannedAt", "reviewedAt", "intention", "win", "mood", "note")
WEEK_COLUMNS = ("reviewedAt", "priorities", "wentWell", "improve", "score")
TIMES = {"plannedAt", "reviewedAt"}


def _columns(row, columns) -> dict | None:
    if row is None:
        return None
    return {c: (to_json(getattr(row, c)) if c in TIMES and getattr(row, c) else getattr(row, c)) for c in columns}


async def _row(user_id: str, kind: str, key: str):
    if kind == "day":
        return await db.daylog.find_unique(where={"userId_date": {"userId": user_id, "date": key_to_date(key)}})
    return await db.weekreview.find_unique(where={"userId_weekStart": {"userId": user_id, "weekStart": key_to_date(key)}})


async def _key(user, kind: str, day: str | None) -> str:
    """The day or week the route will write, worked out the way it does."""
    ctx = await rituals._context(user)
    if kind == "day":
        return reports._roll_key(day) if day else ctx["today"]
    return rituals._week_of(day, ctx["weekStartsOn"]) if day else (await rituals._review_target(user, ctx))["weekStart"]


async def _save_ritual(i: dict, ctx) -> dict:
    kind = i.get("kind")
    if kind not in ("day", "week"):
        raise ToolError('"kind" must be day or week')
    other = WEEK_FIELDS if kind == "day" else DAY_FIELDS
    for field in other:
        if i.get(field) is not None:
            raise ToolError(f'"{field}" is not a field of a {kind}')
    day = t.opt_date(i.get("date"))
    plain: dict = {"date": day} if day else {}
    if kind == "day":
        for field in ("plan", "review"):
            if isinstance(i.get(field), bool):
                plain[field] = i[field]
        for field in ("intention", "win", "note"):
            if isinstance(i.get(field), str):
                plain[field] = i[field]
        if i.get("mood") is not None:
            plain["mood"] = i["mood"]
    else:
        if i.get("priorities") is not None:
            plain["priorities"] = i["priorities"]
        for field, key in (("went_well", "wentWell"), ("improve", "improve")):
            if isinstance(i.get(field), str):
                plain[key] = i[field]
        if isinstance(i.get("done"), bool):
            plain["done"] = i["done"]
    if not set(plain) - {"date"}:
        raise ToolError("Nothing to save")
    body = t._check(rituals.DayBody if kind == "day" else rituals.WeekBody, plain)
    key = await _key(ctx.user, kind, day)
    columns = DAY_COLUMNS if kind == "day" else WEEK_COLUMNS
    before = _columns(await _row(ctx.user_id, kind, key), columns)
    if kind == "day":
        v = await rituals.update_day(body, user=ctx.user)
        result = {"date": v["date"], "planned": v["planned"], "reviewed": v["reviewed"]}
        reflections = {f: v.get(f) for f in ("intention", "win", "mood", "note")}
    else:
        v = await rituals.update_week(body, user=ctx.user)
        result = {"weekStart": v["weekStart"], "priorities": v["priorities"], "reviewed": v["reviewed"], "lifeScore": v.get("score")}
        reflections = {"wentWell": v.get("wentWell"), "improve": v.get("improve")}
    if ctx.journal:  # the reflective text: only with the journal permission (ruling R5)
        result.update(reflections)
    after = _columns(await _row(ctx.user_id, kind, key), columns)
    label = f"{'Day' if kind == 'day' else 'Week'} of {key}"
    return t._done({"kind": "update", "entityType": "ritual", "entityId": key, "label": label, "link": "/dashboard",
                    "undo": {"op": "restore_ritual", "kind": kind, "key": key, "before": before, "after": after}}, result)


def _stored(values: dict) -> dict:
    return {c: (datetime.fromisoformat(v.replace("Z", "+00:00")) if c in TIMES and v else v) for c, v in values.items()}


async def restore(user, undo: dict) -> None:
    kind, key = undo["kind"], undo["key"]
    columns = DAY_COLUMNS if kind == "day" else WEEK_COLUMNS
    row = await _row(user.id, kind, key)
    if _columns(row, columns) != undo["after"]:
        raise ToolError("It was changed since — not undone")
    table = db.daylog if kind == "day" else db.weekreview
    if undo["before"] is None:
        await table.delete(where={"id": row.id})
    else:
        await table.update(where={"id": row.id}, data=_stored(undo["before"]))


t.register({"save_ritual": _save_ritual}, {})
