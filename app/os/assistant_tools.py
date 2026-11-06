"""The chatbot's tools (port of vournalOS assistant.executor.ts). Each tool runs as the
signed-in user through the same Python functions the /api routes use, so it gets the same
validation and ownership checks; its answer is JSON (or a sentence) for the model.

`run()` never raises: a failure becomes an error result the model can read and recover from,
worded as the route would answer ("Error: Task not found")."""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from starlette.requests import Request

from app.db import db
from app import todo_rules
from app.os import accounts, app_todos, calendar, debts, events, notes, reminders, search, shopping, spending_plan, tasks, transactions, wellness
from app.os.ai import _round
from app.os.calendar_utils import local_date_time
from app.os.common import prayer
from app.os.common.http import _validation_messages
from app.os.common.jsparse import js_date, js_slice, js_to_string
from app.os.common.serialize import to_json
from app.os.household import household_id_of, list_access
from app.schemas import TaskDetailsRequest

MAX_RESULT_CHARS = 12_000
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
PRIORITIES = ("LOW", "MEDIUM", "HIGH", "URGENT")
MEALS = ("BREAKFAST", "LUNCH", "DINNER", "SNACK")
BYDAY = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")
FREQ = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}
PRAYERS = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")


class ToolError(Exception):
    """A BadRequestException in the executor: its message goes to the model."""


class Ctx:
    def __init__(self, user, tz: str, today: str, mode: str = "auto"):
        self.user, self.user_id, self.tz, self.today = user, user.id, tz, today
        self.twins_named: set[str] = set()  # titles create_task already pointed to an app to-do this message
        self.mode = mode  # "manual": writes wait for the user's approval; "auto": they run at once
        self.pending: dict[str, dict] = {}  # this message's waiting cards, by action id
        self.deletes_this_message = 0
        self.round_deletes = 0  # delete_item calls in the round being run


# ----- results ---------------------------------------------------------------------------


def js_json(value) -> str:
    """JSON.stringify: no spaces, raw Unicode, numbers and dates as JavaScript writes them."""
    return json.dumps(to_json(value), ensure_ascii=False, separators=(",", ":"))


def _json(value) -> dict:
    text = js_json(value)
    if len(text.encode("utf-16-le")) // 2 > MAX_RESULT_CHARS:
        text = f"{js_slice(text, MAX_RESULT_CHARS)}… (truncated — narrow the request)"
    return {"content": text}


def _done(action: dict, result: dict) -> dict:
    return {**_json({"ok": True, **result}), "action": {**action, "status": "done"}}


def error_text(exc: Exception) -> str:
    if isinstance(exc, ToolError):
        return f"Error: {exc}"
    if isinstance(exc, HTTPException):
        detail = exc.detail
        message = detail.get("message") if isinstance(detail, dict) else detail
        return f"Error: {'; '.join(message) if isinstance(message, list) else message}"
    return "Error: something went wrong while doing that"


def _check(model: type[BaseModel], plain: dict) -> BaseModel:
    """Validates like the route does; the messages joined with "; " (class-validator's check())."""
    try:
        return model.model_validate(plain)
    except ValidationError as exc:
        messages = [m for e in exc.errors() for m in _validation_messages({**e, "loc": ("body", *e["loc"])}, model)]
        raise ToolError("; ".join(messages) or "Invalid input") from exc


def _req(params: dict) -> Request:
    """A GET request carrying `params` as its query, for the list routes."""
    flat = {k: ("true" if v is True else v) for k, v in params.items() if v is not None}
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": urlencode(flat, doseq=True).encode()})


# ----- reading the model's input ----------------------------------------------------------


def _req_str(v, field: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f'"{field}" is required')
    return v.strip()


def opt_date(v) -> str | None:
    """"YYYY-MM-DD" that Date.parse accepts (V8 rolls 30 February over; it refuses day 32)."""
    return v if isinstance(v, str) and DATE_RE.fullmatch(v) and js_date(f"{v}T00:00:00Z") is not None else None


def _req_date(v, field: str) -> str:
    d = opt_date(v)
    if not d:
        raise ToolError(f'"{field}" must be YYYY-MM-DD')
    return d


def opt_time(v) -> str | None:
    return v if isinstance(v, str) and TIME_RE.fullmatch(v) else None


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _positive(v, field: str) -> float:
    if not _number(v) or v != v or v in (float("inf"), float("-inf")) or v <= 0:
        raise ToolError(f'"{field}" must be a positive number')
    return _round(v, 2)


def _limit(v) -> int:
    return min(50, int(v // 1)) if _number(v) and v > 0 else 20


def _duration(v) -> int:
    from app.os.insight_text import js_round

    return js_round(v) if _number(v) and 5 <= v <= 24 * 60 else 60


def _is_priority(v) -> bool:
    return v in PRIORITIES


def _extra(i: dict) -> dict:
    out = {}
    if isinstance(i.get("location"), str) and i["location"].strip():
        out["location"] = i["location"].strip()
    if isinstance(i.get("notes"), str) and i["notes"].strip():
        out["description"] = i["notes"].strip()
    return out


def _add_day(day: str) -> str:
    return (js_date(f"{day}T00:00:00Z") + timedelta(days=1)).date().isoformat()


def _days_between(a: str, b: str) -> int:
    from app.os.insight_text import js_round

    return js_round((js_date(f"{b}T00:00:00Z") - js_date(f"{a}T00:00:00Z")) / timedelta(days=1))


def _weekday(day: str) -> str:
    return BYDAY[(js_date(f"{day}T12:00:00Z").weekday() + 1) % 7]


def align_to_weekdays(day: str, weekdays: list[str]) -> str:
    """The day itself if it is one of `weekdays` (or none given), else the next one that is."""
    if not weekdays:
        return day
    for _ in range(7):
        if _weekday(day) in weekdays:
            break
        day = _add_day(day)
    return day


def read_repeat(v) -> dict | None:
    """The tool's `repeat` → an RRULE (+ last day)."""
    if not v or not isinstance(v, dict):
        return None
    freq = FREQ.get(_js_str(v.get("every")))
    if not freq:
        raise ToolError("repeat.every must be day, week, month or year")
    weekdays = []
    if freq == "WEEKLY" and isinstance(v.get("weekdays"), list):
        for d in map(_js_str, v["weekdays"]):
            if d in BYDAY and d not in weekdays:
                weekdays.append(d)
    parts = [f"FREQ={freq}"]
    interval, count = v.get("interval"), v.get("count")
    if _number(interval) and float(interval).is_integer() and 1 < interval <= 52:
        parts.append(f"INTERVAL={int(interval)}")
    if weekdays:
        parts.append(f"BYDAY={','.join(weekdays)}")
    if _number(count) and float(count).is_integer() and 0 < count <= 1000:
        parts.append(f"COUNT={int(count)}")
    return {"rrule": ";".join(parts), "until": opt_date(v.get("until")), "weekdays": weekdays}


def read_prayer(v, between: bool) -> dict | None:
    """The tool's `prayer` → a stored time anchor."""
    from app.os.insight_text import js_round

    if not v or not isinstance(v, dict):
        return None
    if _js_str(v.get("prayer")) not in PRAYERS:
        raise ToolError("prayer.prayer must be fajr, sunrise, dhuhr, asr, maghrib or isha")
    offset = max(-240, min(240, js_round(v["offset_minutes"]))) if _number(v.get("offset_minutes")) else 0
    anchor = {"start": {"prayer": v["prayer"], "offset": offset}}
    if between and _js_str(v.get("until_prayer")) in PRAYERS:
        anchor["end"] = {"prayer": v["until_prayer"], "offset": 0}
    return anchor


def _js_str(v) -> str:
    """String(v) for what JSON can hold."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if _number(v):
        return js_to_string(v)
    return v if isinstance(v, str) else ""


# ----- local days and times (the user's time zone) ---------------------------------------


def _instant(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return js_date(value)


def local_day(value, tz: str) -> str:
    from zoneinfo import ZoneInfo

    return _instant(value).astimezone(ZoneInfo(tz)).date().isoformat()


def local_time(value, tz: str) -> str:
    from zoneinfo import ZoneInfo

    return _instant(value).astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _local_date_time(value, tz: str) -> str:
    return f"{local_day(value, tz)} {local_time(value, tz)}"


def _iso(moment: datetime) -> str:
    return to_json(moment)


def _due_text(t: dict, tz: str) -> str | None:
    if not t.get("dueAt"):
        return None
    return _local_date_time(t["dueAt"], tz) if t.get("hasDueTime") else local_day(t["dueAt"], tz)


def _due(date: str | None, at: str | None, ctx: Ctx) -> dict:
    if not date and not at:
        return {}
    d = date or ctx.today
    return {"dueAt": _iso(local_date_time(d, at, ctx.tz)), "hasDueTime": True} if at else {"dueAt": d, "hasDueTime": False}


def _decimal(value) -> str:
    """A Prisma Decimal as JSON.stringify wrote it inside a tool result: a string ("12.35")."""
    return js_to_string(float(value))


def _money_label(t: dict) -> str:
    return " · ".join(x for x in (f"{js_to_string(float(t['amount']))} {t['currency']}", t.get("title")) if x)


# ----- the tools -------------------------------------------------------------------------


async def run(name: str, raw_input, ctx: Ctx) -> dict:
    """{"content", "isError"?, "action"?}; never raises."""
    i = raw_input if isinstance(raw_input, dict) else {}
    try:
        tool = _TOOLS.get(name)
        if tool is None:
            return {"content": f"Unknown tool {name}", "isError": True}
        if ctx.mode == "manual" and name in WRITE_TOOLS and name != "delete_item":
            from app.os.assistant_pending import defer

            return await defer(name, i, ctx)
        return await tool(i, ctx)
    except Exception as exc:  # noqa: BLE001 — the model reads the error and recovers
        return {"content": error_text(exc), "isError": True}


async def _get_options(i: dict, ctx: Ctx) -> dict:
    uid = ctx.user_id
    household = await household_id_of(uid)
    categories = await db.category.find_many(where={"userId": uid, "scope": {"in": ["EXPENSE", "INCOME"]}}, order={"sortOrder": "asc"}, take=120)
    account_rows = await db.account.find_many(where={"userId": uid, "isArchived": False}, take=40)
    lists = await db.shoppinglist.find_many(where={**list_access(uid, household), "isArchived": False}, take=40)
    people = await db.person.find_many(where={"userId": uid}, order={"name": "asc"}, take=300)
    return _json({
        "expenseCategories": [{"id": c.id, "name": c.name} for c in categories if c.scope == "EXPENSE"],
        "incomeCategories": [{"id": c.id, "name": c.name} for c in categories if c.scope == "INCOME"],
        "accounts": [{"id": a.id, "name": a.name, "currency": a.currency} for a in account_rows],
        "shoppingLists": [{"id": s.id, "name": s.name} for s in lists],
        "people": [{"id": p.id, "name": p.name} for p in people],
    })


async def _search(i: dict, ctx: Ctx) -> dict:
    types = [t for t in map(_js_str, i["types"]) if t in search.SEARCH_TYPES] if isinstance(i.get("types"), list) else None
    found = await search.search(ctx.user, _req_str(i.get("query"), "query"), types or None, 15)
    return _json([{k: v for k, v in (("type", r["type"]), ("id", r["id"]), ("title", r["title"]), ("subtitle", r.get("subtitle")),
                                     ("date", r.get("date"))) if not (k == "date" and v is None)} for r in found["results"]])


async def _get_agenda(i: dict, ctx: Ctx) -> dict:
    start, end = _req_date(i.get("from"), "from"), _req_date(i.get("to"), "to")
    if end < start:
        raise ToolError('"to" is before "from"')
    if _days_between(start, end) > 62:
        raise ToolError("At most 62 days at a time")
    feed = await calendar.feed_for(ctx.user, _req({"from": start, "to": end}))
    out = []
    for x in feed["items"][:150]:
        item = {"type": x["sourceType"], "id": x["sourceId"], "title": x["title"],
                "start": local_day(x["start"], ctx.tz) if x["allDay"] else _local_date_time(x["start"], ctx.tz)}
        item.update({"allDay": True} if x["allDay"] else {"end": _local_date_time(x["end"], ctx.tz)})
        meta = x.get("meta") or {}
        if "amount" in meta:
            item.update({"amount": meta["amount"], "currency": meta.get("currency")})
        if meta.get("completed"):
            item["done"] = True
        out.append(item)
    return _json(out)


async def _list_tasks(i: dict, ctx: Ctx) -> dict:
    status = ["COMPLETED"] if i.get("status") == "done" else None if i.get("status") == "all" else ["TODO", "IN_PROGRESS"]
    query = i["query"].strip() if isinstance(i.get("query"), str) and i["query"].strip() else None
    res = await tasks.list_tasks(_req({"status": status, "dueFrom": opt_date(i.get("due_from")), "dueTo": opt_date(i.get("due_to")),
                                       "overdue": True if i.get("overdue") is True else None, "q": query,
                                       "sort": "dueAt", "order": "asc", "page": 1, "pageSize": _limit(i.get("limit"))}), user=ctx.user)
    items = [{"id": t["id"], "title": t["title"], "status": t["status"], "priority": t["priority"], "due": _due_text(t, ctx.tz)} for t in res["items"]]
    found = await _app_todos(i, ctx, query)
    out = {"total": res["total"] + len(found), "items": items + [_app_todo_item(t, ctx.tz) for t in found[:_limit(i.get("limit"))]]}
    if found:
        out["appTodos"] = APP_TODOS_NOTE
    return _json(out)


# ----- the app's to-dos (vournal OS had none) ----------------------------------------------

APP_TODOS_NOTE = ('Items with "source": "app" are to-dos from the user\'s voice journal in the vournal app. update_task can tick or untick '
                  "them and change their day, time and priority; they can't be renamed or deleted here.")
APP_ONLY = "This to-do comes from the vournal app: here only its day, time, priority and done can change."
_PRIORITY_URGENCY = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "URGENT": 3}


async def _app_todos(i: dict, ctx: Ctx, query: str | None) -> list:
    """The app's to-dos for list_tasks, filtered as the tasks were (status, days, overdue, words)."""
    status = i.get("status")
    todos = await app_todos.for_user(ctx.user_id, **({"done": True} if status == "done" else {} if status == "all" else {"done": False}))
    zone, now = app_todos.zone(ctx.tz), datetime.now(timezone.utc)
    today = datetime.fromisoformat(ctx.today).date()
    start, end = opt_date(i.get("due_from")), opt_date(i.get("due_to"))

    def keep(t) -> bool:
        day = todo_rules.local_day(t, zone)
        if (start or end) and day is None:
            return False
        if start and day.isoformat() < start or end and day.isoformat() > end:
            return False
        if i.get("overdue") is True and not app_todos.is_overdue(t, today, now, zone):
            return False
        return not query or query.casefold() in t.text.casefold()

    return sorted((t for t in todos if keep(t)), key=app_todos._sort_key)


def _app_todo_item(t, tz: str) -> dict:
    due = app_todos.due_instant(t, tz)
    return {"id": t.id, "title": t.text, "status": "COMPLETED" if t.done else "TODO", "priority": app_todos.URGENCY_PRIORITY.get(t.urgency, "MEDIUM"),
            "due": (_local_date_time(due, tz) if t.startAt else local_day(due, tz)) if due else None, "source": "app"}


def _app_when(todo, date: str | None, at: str | None, ctx: Ctx) -> dict:
    """The day and time the model gave (the web's time zone) as the app's rules read them (the
    app account's own zone, which can differ): the same moment."""
    if at is None and date is not None and not todo.startAt:
        return {"date": date}  # a day alone is a calendar day, in any zone
    if at is None and date is not None:
        at = local_time(todo.startAt, ctx.tz)  # moving to another day keeps the time it had
    if at is not None and date is None:
        date = local_day(todo.startAt, ctx.tz) if todo.startAt else todo.dueDate.date().isoformat() if todo.dueDate else None
        if date is None:
            return {"time": at}  # the app's rules answer "Choose a date before adding a time."
    if at is None:
        return {}
    moment = local_date_time(date, at, ctx.tz).astimezone(todo_rules._user_tz(ctx.user.timezone))
    return {"date": moment.date().isoformat(), "time": moment.strftime("%H:%M")}


async def _app_todo(ctx: Ctx, id: str):
    return await db.todo.find_first(where={"id": id, "userId": ctx.user_id})


async def _update_app_todo(i: dict, ctx: Ctx, todo) -> dict:
    """update_task on an app to-do, through the app's own rules (todo_rules)."""
    if any(k in i for k in ("title", "repeat", "stop_repeating", "clear_due")) or i.get("status") == "in_progress":
        raise ToolError(APP_ONLY)
    link = f"/journal/{todo.entryId}"
    if i.get("status") == "done":
        ticked = await todo_rules.set_done(ctx.user, todo.id, True, advance=True)
        advanced = not ticked.done  # a repeat moves to its next day instead of being ticked
        return _done({"kind": "complete", "entityType": "app_todo", "entityId": todo.id, "label": todo.text, "link": link,
                      "undo": None if advanced else {"op": "reopen_app_todo", "id": todo.id}},
                     {**_app_todo_item(ticked, ctx.tz), "status": "COMPLETED"})
    if i.get("status") == "todo" and todo.done:
        todo = await todo_rules.set_done(ctx.user, todo.id, False)
    if "date" in i and not opt_date(i.get("date")):
        raise ToolError('"date" must be YYYY-MM-DD')
    fields = _app_when(todo, opt_date(i.get("date")), opt_time(i.get("time")), ctx)
    if _is_priority(i.get("priority")):
        fields["urgency"] = _PRIORITY_URGENCY[i["priority"]]
    if fields:
        try:
            todo = await todo_rules.update_details(ctx.user, todo.id, TaskDetailsRequest(**fields))
        except HTTPException as exc:
            raise ToolError(exc.detail if isinstance(exc.detail, str) else "That change didn't work") from exc
    return _done({"kind": "update", "entityType": "app_todo", "entityId": todo.id, "label": todo.text, "link": link, "undo": None},
                 _app_todo_item(todo, ctx.tz))


async def _get_money(i: dict, ctx: Ctx) -> dict:
    p = await spending_plan.plan(ctx.user)
    acc = await accounts.list_accounts(_req({}), user=ctx.user)
    return _json({
        "currency": p["currency"],
        "accounts": [{"id": a["id"], "name": a["name"], "balance": a["balance"], "currency": a["currency"]} for a in acc["items"] if not a["isArchived"]],
        "period": p["period"],
        "daysLeft": p["daysLeft"],
        "leftToSpendToday": p["todayLeft"],
        "dailyAllowance": p["dailyAllowance"],
        "spentToday": p["spentToday"],
        "leftForPeriod": p["leftToSpend"],
        "income": {"total": p["incomeTotal"], "received": p["incomeReceived"], "extra": p["extraIncome"]},
        "commitments": {"total": p["commitmentsTotal"], "paid": p["commitmentsPaid"]},
        "savingsTarget": p["savingsTarget"],
        "debtPayoff": [{"title": d["title"], "left": d["left"], "payBy": d["target"], "perDay": d["perDay"], "thisPeriodStillToPutAside": d["remaining"]}
                       for d in p["debtPlan"]],
        "advice": p["advice"],
        "extraIncomeSplit": p["extra"],
        "comingUp": [{"kind": c["kind"], "title": c["title"], "amount": c["amount"], "date": c["date"]} for c in p["commitments"] if not c["done"]][:12],
        "status": p["status"],
    })


async def _list_transactions(i: dict, ctx: Ctx) -> dict:
    kind = {"expense": "EXPENSE", "income": "INCOME", "transfer": "TRANSFER"}.get(i.get("type"))
    query = i["query"].strip() if isinstance(i.get("query"), str) and i["query"].strip() else None
    res = await transactions.list_transactions(_req({"type": [kind] if kind else None, "from": opt_date(i.get("from")), "to": opt_date(i.get("to")),
                                                     "q": query, "categoryId": i["category_id"] if isinstance(i.get("category_id"), str) else None,
                                                     "page": 1, "pageSize": _limit(i.get("limit"))}), user=ctx.user)
    return _json({"total": res["total"], "items": [{
        "id": t["id"], "type": t["type"], "amount": float(t["amount"]), "currency": t["currency"], "date": local_day(t["date"], ctx.tz),
        "title": t["title"], "category": (t.get("category") or {}).get("name"), "account": (t.get("account") or {}).get("name"),
    } for t in res["items"]]})


async def _list_debts(i: dict, ctx: Ctx) -> dict:
    status = i["status"] if i.get("status") in ("closed", "all") else "open"
    res = await debts.list_debts(_req({"status": status}), user=ctx.user)
    return _json({"summary": res["summary"], "items": [{
        "id": d["id"], "direction": "i_owe" if d["direction"] == "I_OWE" else "owed_to_me", "who": d["who"], "title": d["title"],
        "amount": d["principal"], "paid": d["paid"], "left": d["remaining"], "currency": d["currency"],
        "due": to_json(d["dueDate"])[:10] if d.get("dueDate") else None, "status": d["status"],
    } for d in res["items"]]})


async def _get_health(i: dict, ctx: Ctx) -> dict:
    d = await wellness._day(ctx.user, opt_date(i.get("date")) or ctx.today)
    w = await wellness._weights(ctx.user, 120)
    p = d["profile"]
    profile = {
        "sex": p["profile"]["sex"], "age": p["age"], "heightCm": p["profile"]["heightCm"], "currentWeightKg": p["currentWeightKg"],
        "weighedOn": p["weightDate"], "bmi": p["bmi"], "goal": p["profile"]["goal"], "targetWeightKg": p["profile"]["targetWeightKg"],
        "weeklyRateKg": p["profile"]["weeklyRateKg"], "activityLevel": p["profile"]["activityLevel"], "targets": p["targets"], "weeksToGoal": p["weeksToGoal"],
    } if p["exists"] else "Not set up yet (Health → plan)."
    return _json({
        "profile": profile,
        "weight": {**w["stats"], "recent": [{"date": x["date"], "kg": x["weightKg"]} for x in w["items"][-30:]]},
        "day": {
            "date": d["date"], "calorieTarget": d["target"], "allowedWithExercise": d["allowed"], "eaten": d["eaten"], "remaining": d["remaining"],
            "totals": d["totals"],
            "meals": [{"meal": m["meal"], "calories": m["calories"], "items": [{"id": x["id"], "name": x["name"], "calories": x["calories"]} for x in m["items"]]}
                      for m in d["meals"]],
            "exercise": d["exerciseLogs"], "water": d["water"],
        },
    })


async def _update_health_profile(i: dict, ctx: Ctx) -> dict:
    patch = {}
    if i.get("goal") in ("LOSE", "MAINTAIN", "GAIN"):
        patch["goal"] = i["goal"]
    for src, dst in (("target_weight_kg", "targetWeightKg"), ("weekly_rate_kg", "weeklyRateKg"), ("height_cm", "heightCm")):
        if _number(i.get(src)):
            patch[dst] = i[src]
    if isinstance(i.get("activity_level"), str):
        patch["activityLevel"] = i["activity_level"]
    for src, dst in (("calorie_target", "calorieTargetOverride"), ("water_goal_ml", "waterGoalMl")):
        if src in i and (i[src] is None or _number(i[src])):
            patch[dst] = i[src]
    if not patch:
        raise ToolError("Nothing to change")
    before = await fields_now("health_profile", ctx.user_id, patch, ctx)
    p = await wellness.update_profile(_check(wellness.ProfileBody, patch), user=ctx.user)
    pr = p["profile"]
    undo = await _fields_undo("health_profile", ctx.user_id, before, ctx)
    return _done({"kind": "update", "entityType": "health_profile", "entityId": ctx.user_id, "label": "Health plan", "link": "/health", "undo": undo},
                 {"goal": pr["goal"], "targetWeightKg": pr["targetWeightKg"], "weeklyRateKg": pr["weeklyRateKg"], "activityLevel": pr["activityLevel"],
                  "heightCm": pr["heightCm"], "targets": p["targets"], "weeksToGoal": p["weeksToGoal"]})


async def _get_prayer_times(i: dict, ctx: Ctx) -> dict:
    date = opt_date(i.get("date")) or ctx.today
    settings = await prayer.settings(ctx.user_id)
    if settings is None:
        return {"content": "The user has not chosen a city for prayer times yet (Settings → Preferences)."}
    times = prayer.prayer_times(settings, date)
    return _json({"date": date, "times": {k: local_time(v, ctx.tz) if isinstance(v, datetime) else v for k, v in times.items()}})


def _same_title(text: str) -> str:
    return " ".join(text.casefold().split())


async def _create_task(i: dict, ctx: Ctx) -> dict:
    title = _req_str(i.get("title"), "title")
    # the spec: before adding, look at both lists, so "add X" doesn't duplicate an app to-do;
    # asked again in the same message, it's added (the user wants a separate web task)
    key = _same_title(title)
    if key not in ctx.twins_named:
        twin = next((t for t in await app_todos.for_user(ctx.user_id, done=False) if _same_title(t.text) == key), None)
        if twin:
            ctx.twins_named.add(key)
            return {"content": f'An open to-do "{twin.text}" already exists in the vournal app (id {twin.id}); nothing was added. '
                               "Use update_task on it, or call create_task again with the same title if the user wants a separate task."}
    repeat = read_repeat(i.get("repeat"))
    anchor = read_prayer(i.get("prayer"), False)
    # a recurring or prayer-timed task needs a first day
    first = opt_date(i.get("date")) or (ctx.today if repeat or anchor else None)
    date = align_to_weekdays(first, repeat["weekdays"]) if first and repeat else first
    plain = {"title": title, **({"dueAt": date} if anchor else _due(date, opt_time(i.get("time")), ctx))}
    if anchor:
        plain["timeAnchor"] = anchor
    if repeat:
        plain["rrule"] = repeat["rrule"]
        if repeat["until"]:
            plain["recurrenceEndAt"] = repeat["until"]
    if _is_priority(i.get("priority")):
        plain["priority"] = i["priority"]
    if isinstance(i.get("notes"), str) and i["notes"].strip():
        plain["description"] = i["notes"].strip()
    t = await tasks.create(_check(tasks.CreateBody, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "task", "entityId": t["id"], "label": t["title"], "link": f"/tasks?id={t['id']}",
                  "undo": {"op": "delete", "type": "task", "id": t["id"]}},
                 {"id": t["id"], "title": t["title"], "due": _due_text(t, ctx.tz), "repeats": t.get("rrule")})


async def _update_task(i: dict, ctx: Ctx) -> dict:
    id = _req_str(i.get("id"), "id")
    try:
        current = await tasks.get(id, user=ctx.user)
    except HTTPException as exc:
        todo = await _app_todo(ctx, id) if exc.status_code == 404 else None
        if todo is None:
            raise
        return await _update_app_todo(i, ctx, todo)
    link = f"/tasks?id={id}"
    if i.get("status") == "done":
        t = await tasks.complete(id, user=ctx.user)
        nxt = t.get("nextOccurrence")
        undo = {"op": "reopen_task", "id": id, **({"nextId": nxt["id"]} if nxt else {})}  # undo also removes the next copy
        return _done({"kind": "complete", "entityType": "task", "entityId": id, "label": t["title"], "link": link, "undo": undo},
                     {"id": id, "title": t["title"], "status": "COMPLETED", "next": {"id": nxt["id"]} if nxt else None})
    if i.get("status") == "todo" and current["status"] == "COMPLETED":
        await tasks.reopen(id, user=ctx.user)
    patch = {}
    if isinstance(i.get("title"), str) and i["title"].strip():
        patch["title"] = i["title"].strip()
    if _is_priority(i.get("priority")):
        patch["priority"] = i["priority"]
    if i.get("status") == "in_progress":
        patch["status"] = "IN_PROGRESS"
    if i.get("status") == "todo" and current["status"] != "COMPLETED":
        patch["status"] = "TODO"
    repeat = read_repeat(i.get("repeat"))
    if repeat:
        patch.update({"rrule": repeat["rrule"], "recurrenceEndAt": repeat["until"]})
    if i.get("stop_repeating") is True:
        patch.update({"rrule": None, "recurrenceEndAt": None})
    # repeating needs a due day: keep it, or start today
    due_day = opt_date(i.get("date")) or (align_to_weekdays(ctx.today, repeat["weekdays"]) if repeat and not current.get("dueAt") else None)
    at = opt_time(i.get("time"))
    if i.get("clear_due") is True:
        patch.update({"dueAt": None, "hasDueTime": False})
    elif due_day or at:
        base = due_day or (local_day(current["dueAt"], ctx.tz) if current.get("dueAt") else ctx.today)
        # moving to another day keeps the time it had
        keep = local_time(current["dueAt"], ctx.tz) if current.get("dueAt") and current.get("hasDueTime") else None
        patch.update(_due(base, at or keep, ctx))
    before = await fields_now("task", id, patch, ctx) if patch else None
    t = await tasks.update(id, _check(tasks.UpdateBody, patch), user=ctx.user) if patch else await tasks.get(id, user=ctx.user)
    undo = await _fields_undo("task", id, before, ctx) if before else None
    return _done({"kind": "update", "entityType": "task", "entityId": id, "label": t["title"], "link": link, "undo": undo},
                 {"id": id, "title": t["title"], "status": t["status"], "due": _due_text(t, ctx.tz), "repeats": t.get("rrule")})


def _event_link(id: str, start, tz: str) -> str:
    return f"/calendar?id={id}&date={local_day(start, tz)}"


def _event_start(e: dict, tz: str) -> str:
    return local_day(e["startAt"], tz) if e["allDay"] else _local_date_time(e["startAt"], tz)


async def _create_event(i: dict, ctx: Ctx) -> dict:
    title = _req_str(i.get("title"), "title")
    repeat = read_repeat(i.get("repeat"))
    anchor = read_prayer(i.get("prayer"), True)
    # "every Thursday" starts on the first Thursday
    date = align_to_weekdays(_req_date(i.get("date"), "date"), repeat["weekdays"]) if repeat else _req_date(i.get("date"), "date")
    # with a prayer the time is worked out from it; 12:00 only carries the day
    at = "12:00" if anchor else opt_time(i.get("time"))
    minutes = _duration(i.get("duration_minutes"))
    start = local_date_time(date, at, ctx.tz) if at else None
    series = {"rrule": repeat["rrule"], **({"recurrenceEndAt": repeat["until"]} if repeat["until"] else {})} if repeat else {}
    if start:
        plain = {"title": title, "startAt": _iso(start), **({} if anchor and anchor.get("end") else {"endAt": _iso(start + timedelta(minutes=minutes))}),
                 "allDay": False, **({"timeAnchor": anchor} if anchor else {}), **series, **_extra(i)}
    else:
        plain = {"title": title, "date": date, "allDay": True, **series, **_extra(i)}
    e = await events.create(_check(events.CreateBody, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "event", "entityId": e["id"], "label": e["title"], "link": f"/calendar?id={e['id']}&date={date}",
                  "undo": {"op": "delete", "type": "event", "id": e["id"]}},
                 {"id": e["id"], "title": e["title"], "start": _event_start(e, ctx.tz),
                  "end": _local_date_time(e["endAt"], ctx.tz) if e.get("endAt") else None, "repeats": e.get("rrule")})


async def _update_event(i: dict, ctx: Ctx) -> dict:
    from app.os.insight_text import js_round

    id = _req_str(i.get("id"), "id")
    cur = await events.get(id, user=ctx.user)
    patch = {}
    if isinstance(i.get("title"), str) and i["title"].strip():
        patch["title"] = i["title"].strip()
    if isinstance(i.get("location"), str):
        patch["location"] = i["location"].strip() or None
    repeat = read_repeat(i.get("repeat"))
    if repeat:
        patch.update({"rrule": repeat["rrule"], "recurrenceEndAt": repeat["until"]})
    if i.get("stop_repeating") is True:
        patch.update({"rrule": None, "recurrenceEndAt": None})
    # "make it every Thursday": move a non-Thursday start to the next Thursday
    cur_day = local_day(cur["startAt"], ctx.tz)
    aligned = align_to_weekdays(cur_day, repeat["weekdays"]) if repeat else cur_day
    move_to = opt_date(i.get("date")) or (aligned if repeat and aligned != cur_day else None)
    at_in = opt_time(i.get("time"))
    if move_to or at_in or "duration_minutes" in i:
        start_at = _instant(cur["startAt"])
        old_minutes = js_round((_instant(cur["endAt"]) - start_at) / timedelta(minutes=1)) if cur.get("endAt") else 60
        date = move_to or cur_day
        at = at_in or (None if cur["allDay"] else local_time(start_at, ctx.tz))
        if at:
            start = local_date_time(date, at, ctx.tz)
            minutes = _duration(i["duration_minutes"]) if "duration_minutes" in i else 60 if cur["allDay"] else old_minutes
            patch.update({"startAt": _iso(start), "endAt": _iso(start + timedelta(minutes=minutes)), "allDay": False})
        else:
            patch.update({"date": date, "allDay": True})
    before = await fields_now("event", id, patch, ctx) if patch else None
    e = await events.update(id, _check(events.UpdateBody, patch), user=ctx.user) if patch else cur
    undo = await _fields_undo("event", id, before, ctx) if before else None
    return _done({"kind": "update", "entityType": "event", "entityId": id, "label": e["title"], "link": _event_link(id, e["startAt"], ctx.tz), "undo": undo},
                 {"id": id, "title": e["title"], "start": _event_start(e, ctx.tz), "repeats": e.get("rrule")})


async def _create_reminder(i: dict, ctx: Ctx) -> dict:
    title = _req_str(i.get("title"), "title")
    repeat = read_repeat(i.get("repeat"))
    anchor = read_prayer(i.get("prayer"), False)
    at = "12:00" if anchor else opt_time(i.get("time"))
    # a time with no day means the next time that hour comes round
    date = opt_date(i.get("date"))
    if not date and anchor:
        date = ctx.today
    if not date and at:
        date = ctx.today if local_date_time(ctx.today, at, ctx.tz) > datetime.now(timezone.utc) else _add_day(ctx.today)
    if not date and repeat:
        date = ctx.today
    if date and repeat:
        date = align_to_weekdays(date, repeat["weekdays"])
    plain = {"title": title}
    if date:
        plain["date"] = date
        if at:
            plain["time"] = at
    if anchor:
        plain["timeAnchor"] = anchor
    if repeat:  # reminders carry the end inside the rule
        plain["rrule"] = f"{repeat['rrule']};UNTIL={repeat['until'].replace('-', '')}T235959Z" if repeat["until"] else repeat["rrule"]
    if isinstance(i.get("notes"), str) and i["notes"].strip():
        plain["notes"] = i["notes"].strip()
    r = await reminders.create(_check(reminders.CreateBody, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "reminder", "entityId": r["id"], "label": r["title"], "link": f"/reminders?id={r['id']}",
                  "undo": {"op": "delete", "type": "reminder", "id": r["id"]}},
                 {"id": r["id"], "title": r["title"], "at": _local_date_time(r["remindAt"], ctx.tz), "repeats": r.get("rrule")})


async def _add_transaction(i: dict, ctx: Ctx) -> dict:
    date = opt_date(i.get("date"))
    plain = {"type": "INCOME" if i.get("type") == "income" else "EXPENSE", "amount": _positive(i.get("amount"), "amount")}
    if isinstance(i.get("currency"), str) and i["currency"]:
        plain["currency"] = i["currency"].upper()
    if isinstance(i.get("title"), str) and i["title"].strip():
        plain["title"] = i["title"].strip()
    for src, dst in (("category_id", "categoryId"), ("account_id", "accountId")):
        if isinstance(i.get(src), str):
            plain[dst] = i[src]
    if date and date != ctx.today:
        plain["date"] = date
    t = await transactions.create(_check(transactions.CreateBody, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "transaction", "entityId": t["id"], "label": _money_label(t), "link": f"/finance/transactions?id={t['id']}",
                  "undo": {"op": "delete", "type": "transaction", "id": t["id"]}},
                 {"id": t["id"], "type": t["type"], "amount": _decimal(t["amount"]), "currency": t["currency"], "date": local_day(t["date"], ctx.tz),
                  "category": (t.get("category") or {}).get("name")})


async def _update_transaction(i: dict, ctx: Ctx) -> dict:
    id = _req_str(i.get("id"), "id")
    date = opt_date(i.get("date"))
    plain = {}
    if "amount" in i:
        plain["amount"] = _positive(i["amount"], "amount")
    if isinstance(i.get("title"), str):
        plain["title"] = i["title"].strip() or None
    for src, dst in (("category_id", "categoryId"), ("account_id", "accountId")):
        if isinstance(i.get(src), str):
            plain[dst] = i[src]
    if date:
        plain["date"] = date
    before = await fields_now("transaction", id, plain, ctx) if plain else None
    t = await transactions.update(id, _check(transactions.UpdateBody, plain), user=ctx.user)
    undo = await _fields_undo("transaction", id, before, ctx) if before else None
    return _done({"kind": "update", "entityType": "transaction", "entityId": id, "label": _money_label(t), "link": f"/finance/transactions?id={id}", "undo": undo},
                 {"id": id, "amount": _decimal(t["amount"]), "currency": t["currency"], "date": local_day(t["date"], ctx.tz), "title": t["title"],
                  "category": (t.get("category") or {}).get("name")})


async def _create_debt(i: dict, ctx: Ctx) -> dict:
    who = _req_str(i.get("who"), "who")
    due = opt_date(i.get("due_date"))
    plain = {"direction": "OWED_TO_ME" if i.get("direction") == "owed_to_me" else "I_OWE",
             "title": i["title"].strip() if isinstance(i.get("title"), str) and i["title"].strip() else who,
             **({"personId": i["person_id"]} if isinstance(i.get("person_id"), str) else {"counterparty": who}),
             "principal": _positive(i.get("amount"), "amount")}
    if isinstance(i.get("currency"), str) and i["currency"]:
        plain["currency"] = i["currency"].upper()
    if due and due > ctx.today:
        plain["dueDate"] = due
    d = await debts.create(_check(debts.CreateBody, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "debt", "entityId": d["id"], "label": f"{d.get('who') or who} · {js_to_string(float(d['principal']))} {d['currency']}",
                  "link": f"/finance/debts?id={d['id']}", "undo": {"op": "delete", "type": "debt", "id": d["id"]}},
                 {"id": d["id"], "who": d.get("who"), "amount": d["principal"], "currency": d["currency"],
                  "due": to_json(d["dueDate"])[:10] if d.get("dueDate") else None})


async def _add_debt_payment(i: dict, ctx: Ctx) -> dict:
    debt_id = _req_str(i.get("debt_id"), "debt_id")
    date = opt_date(i.get("date"))
    body = _check(debts.PaymentBody, {"amount": _positive(i.get("amount"), "amount"), **({"date": date} if date else {})})
    before = await db.debtpayment.find_many(where={"debtId": debt_id, "userId": ctx.user_id})
    d = await debts.add_payment(debt_id, body, user=ctx.user)
    added = await db.debtpayment.find_first(where={"debtId": debt_id, "userId": ctx.user_id, "id": {"not_in": [b.id for b in before]}})
    return _done({"kind": "create", "entityType": "debt_payment", "entityId": added.id if added else debt_id,
                  "label": f"{js_to_string(float(body.amount))} {d['currency']} → {d.get('who') or d['title']}", "link": f"/finance/debts?id={debt_id}",
                  "undo": {"op": "remove_payment", "debtId": debt_id, "paymentId": added.id} if added else None},
                 {"debtId": debt_id, "paid": body.amount, "left": d["remaining"], "currency": d["currency"]})


async def _add_note(i: dict, ctx: Ctx) -> dict:
    title = _req_str(i.get("title"), "title")
    plain = {"title": js_slice(title, 200), **({"content": i["content"].strip()} if isinstance(i.get("content"), str) and i["content"].strip() else {})}
    n = await notes.create(_check(notes.CreateBody, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "note", "entityId": n["id"], "label": n["title"], "link": f"/notes?id={n['id']}",
                  "undo": {"op": "delete", "type": "note", "id": n["id"]}}, {"id": n["id"], "title": n["title"]})


async def _add_shopping_item(i: dict, ctx: Ctx) -> dict:
    plain = {"name": _req_str(i.get("name"), "name")}
    if _number(i.get("quantity")) and i["quantity"] > 0:
        plain["quantity"] = i["quantity"]
    if isinstance(i.get("list_id"), str):
        plain["listId"] = i["list_id"]
    item = await shopping.create_item(_check(shopping.ItemCreate, plain), user=ctx.user)
    return _done({"kind": "create", "entityType": "shopping_item", "entityId": item["id"], "label": item["name"],
                  "link": f"/shopping?list={item['listId']}&id={item['id']}", "undo": {"op": "delete", "type": "shopping_item", "id": item["id"]}},
                 {"id": item["id"], "name": item["name"], "listId": item["listId"]})


async def _log_food(i: dict, ctx: Ctx) -> dict:
    from app.os.insight_text import js_round

    date = opt_date(i.get("date"))

    def macro(v):
        return _round(v, 1) if _number(v) and v >= 0 else None

    plain = {"name": js_slice(_req_str(i.get("name"), "name"), 120), "calories": js_round(_positive(i.get("calories"), "calories"))}
    for key in ("protein", "carbs", "fat"):
        if macro(i.get(key)) is not None:
            plain[key] = macro(i[key])
    if i.get("meal") in MEALS:
        plain["meal"] = i["meal"]
    if date:
        plain["date"] = date
    plain["source"] = "ai"
    await wellness.add_food(_check(wellness.FoodLogBody, plain), user=ctx.user)
    f = await db.foodlog.find_first(where={"userId": ctx.user_id}, order={"createdAt": "desc"})
    return _done({"kind": "create", "entityType": "food_log", "entityId": f.id, "label": f"{f.name or plain['name']} · {f.calories} kcal",
                  "link": f"/health?date={date or ctx.today}", "undo": {"op": "delete", "type": "food_log", "id": f.id}},
                 {"id": f.id, "name": f.name, "calories": f.calories})


async def _log_water(i: dict, ctx: Ctx) -> dict:
    from app.os.insight_text import js_round

    ml = js_round(_positive(i.get("ml"), "ml"))
    date = opt_date(i.get("date"))
    await wellness.add_water(_check(wellness.WaterBody, {"addMl": ml, **({"date": date} if date else {})}), user=ctx.user)
    d = await wellness._day(ctx.user, date or ctx.today)
    return _done({"kind": "create", "entityType": "water", "entityId": d["date"], "label": f"{ml} ml", "link": f"/health?date={d['date']}",
                  "undo": {"op": "remove_water", "date": d["date"], "ml": ml}},
                 {"addedMl": ml, "totalMl": d["water"]["ml"], "goalMl": d["water"]["goalMl"]})


async def _log_weight(i: dict, ctx: Ctx) -> dict:
    from app.os.calendar_utils import date_only_value

    date = opt_date(i.get("date"))
    body = _check(wellness.WeightBody, {"weightKg": _positive(i.get("kg"), "kg"), **({"date": date} if date else {})})
    day = date_only_value(date or ctx.today)
    before = await db.weightlog.find_first(where={"userId": ctx.user_id, "date": day})
    await wellness.log_weight(body, user=ctx.user)
    w = await db.weightlog.find_first(where={"userId": ctx.user_id, "date": day})  # by its day (the route lists only the last year)
    # a day holds one weight: undo puts back the one this replaced, or removes the new one
    undo = {"op": "restore_weight", "id": w.id, "previousKg": float(before.weightKg)} if before else {"op": "delete", "type": "weight", "id": w.id}
    return _done({"kind": "create", "entityType": "weight", "entityId": w.id, "label": f"{js_to_string(float(w.weightKg))} kg", "link": "/health/progress",
                  "undo": undo}, {"id": w.id, "kg": w.weightKg})


async def _delete_item(i: dict, ctx: Ctx) -> dict:
    kind = i.get("type")
    id = _req_str(i.get("id"), "id")
    found = await describe(ctx, kind, id)
    ctx.deletes_this_message += 1
    if ctx.mode == "auto" and ctx.round_deletes == 1 and ctx.deletes_this_message == 1:
        # Auto deletes one plain item at once (with Undo); anything bigger still asks
        from app.os import assistant_snapshot

        copy = await assistant_snapshot.take(assistant_snapshot.MODELS[kind], id, ctx.user_id)
        if copy["count"] == 1 and not copy["has_file"]:
            undo = await remove(ctx.user, kind, id)
            return {"content": f'Deleted "{found["label"]}". The user can undo it.',
                    "action": {"kind": "delete", "entityType": kind, "entityId": id, "label": found["label"], "link": None, "undo": undo, "status": "done"}}
    return {"content": f'Waiting for the user to confirm deleting "{found["label"]}". It is not deleted yet — ask them to confirm with the button below your reply.',
            "action": {"kind": "delete", "entityType": kind, "entityId": id, "label": found["label"], "link": found["link"], "undo": None, "status": "pending"}}


_TOOLS = {
    "get_options": _get_options, "search": _search, "get_agenda": _get_agenda, "list_tasks": _list_tasks, "get_money": _get_money,
    "list_transactions": _list_transactions, "list_debts": _list_debts, "get_health": _get_health, "update_health_profile": _update_health_profile,
    "get_prayer_times": _get_prayer_times, "create_task": _create_task, "update_task": _update_task, "create_event": _create_event,
    "update_event": _update_event, "create_reminder": _create_reminder, "add_transaction": _add_transaction, "update_transaction": _update_transaction,
    "create_debt": _create_debt, "add_debt_payment": _add_debt_payment, "add_note": _add_note, "add_shopping_item": _add_shopping_item,
    "log_food": _log_food, "log_water": _log_water, "log_weight": _log_weight, "delete_item": _delete_item,
}


# ----- undoing an edit: the changed fields' old values, unless the item changed since ----------

_EVENT_TIME = ("startAt", "endAt", "allDay", "date")
_TASK_DUE = ("dueAt", "hasDueTime")


def _norm(value):
    """One form for comparing a field now with what the edit left."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return value


async def fields_now(kind: str, id: str, keys, ctx: Ctx) -> dict:
    """The item's current values for `keys`, in the route's update body's own form. A time is
    taken as a whole (an event's start/end/all-day, a task's due day and time)."""
    keys = set(keys)
    out: dict = {}
    if kind == "task":
        t = await tasks.get(id, user=ctx.user)
        if keys & set(_TASK_DUE):
            keys -= set(_TASK_DUE)
            if not t.get("dueAt"):
                out.update(dueAt=None, hasDueTime=False)
            elif t.get("hasDueTime"):
                out.update(dueAt=_iso(_instant(t["dueAt"])), hasDueTime=True)
            else:
                out.update(dueAt=local_day(t["dueAt"], ctx.tz), hasDueTime=False)
        for k in keys:
            out[k] = to_json(t.get(k))[:10] if k == "recurrenceEndAt" and t.get(k) else t.get(k)
    elif kind == "event":
        e = await events.get(id, user=ctx.user)
        if keys & set(_EVENT_TIME):
            keys -= set(_EVENT_TIME)
            if e["allDay"]:
                out.update(date=local_day(e["startAt"], ctx.tz), allDay=True)
            else:
                out.update(startAt=_iso(_instant(e["startAt"])), endAt=_iso(_instant(e["endAt"])) if e.get("endAt") else None, allDay=False)
        for k in keys:
            out[k] = to_json(e.get(k))[:10] if k == "recurrenceEndAt" and e.get(k) else e.get(k)
    elif kind == "transaction":
        t = await transactions.get(id, user=ctx.user)
        for k in keys:
            out[k] = local_day(t["date"], ctx.tz) if k == "date" else float(t["amount"]) if k == "amount" else t.get(k)
    elif kind == "health_profile":
        p = (await wellness._profile(ctx.user))["profile"]
        out = {k: p.get(k) for k in keys}
    return out


async def _fields_undo(kind: str, id: str, before: dict, ctx: Ctx) -> dict:
    return {"op": "restore_fields", "type": kind, "id": id, "before": before, "after": await fields_now(kind, id, before, ctx)}


async def _restore_fields(undo: dict, ctx: Ctx) -> None:
    kind, id = undo["type"], undo["id"]
    try:
        now = await fields_now(kind, id, undo["after"], ctx)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ToolError("It was deleted since — nothing to undo") from exc
        raise
    if any(_norm(now.get(k)) != _norm(v) for k, v in undo["after"].items()):
        raise ToolError("It was changed since — not undone")
    before = undo["before"]
    if kind == "task":
        await tasks.update(id, _check(tasks.UpdateBody, before), user=ctx.user)
    elif kind == "event":
        await events.update(id, _check(events.UpdateBody, before), user=ctx.user)
    elif kind == "transaction":
        await transactions.update(id, _check(transactions.UpdateBody, before), user=ctx.user)
    elif kind == "health_profile":
        await wellness.update_profile(_check(wellness.ProfileBody, before), user=ctx.user)


READ_TOOLS = frozenset({"get_options", "search", "get_agenda", "list_tasks", "get_money", "list_transactions", "list_debts", "get_health",
                        "get_prayer_times"})
WRITE_TOOLS = frozenset(_TOOLS) - READ_TOOLS


# ----- deleting (after the user confirms) and undoing ------------------------------------


async def remove(user, kind: str, id: str) -> dict | None:
    """Deletes a confirmed item (only ids that belong to the user). Returns how to undo it: a
    copy of everything the delete removes, taken just before."""
    from app.os import assistant_snapshot

    handlers = {"task": tasks.remove, "event": events.remove, "reminder": reminders.remove, "transaction": transactions.remove, "debt": debts.remove,
                "note": notes.remove, "shopping_item": shopping.remove_item, "food_log": wellness.remove_food, "weight": wellness.remove_weight}
    if kind not in handlers:
        return None
    snapshot = await assistant_snapshot.take(assistant_snapshot.MODELS[kind], id, user.id)
    await handlers[kind](id, user=user)
    # attached files are deleted from storage with it and can't come back: no Undo then
    return None if snapshot["has_file"] else {"op": "restore_snapshot", "snapshot": snapshot}


async def reverse(user, undo: dict, ctx: Ctx | None = None) -> None:
    if undo["op"] == "restore_fields":
        await _restore_fields(undo, ctx)
    elif undo["op"] == "restore_snapshot":
        from app.os import assistant_snapshot

        await assistant_snapshot.restore(undo["snapshot"])
    elif undo["op"] == "remove_water":
        await wellness.add_water(_check(wellness.WaterBody, {"addMl": -undo["ml"], "date": undo["date"]}), user=user)
    elif undo["op"] == "delete":
        await remove(user, undo["type"], undo["id"])
    elif undo["op"] == "reopen_task":
        await tasks.reopen(undo["id"], user=user)
        if undo.get("nextId"):
            await tasks.remove(undo["nextId"], user=user)
    elif undo["op"] == "restore_weight":
        await db.weightlog.update_many(where={"id": undo["id"], "userId": user.id}, data={"weightKg": undo["previousKg"]})
    elif undo["op"] == "reopen_app_todo":
        await todo_rules.set_done(user, undo["id"], False)
    else:
        await debts.remove_payment(undo["debtId"], undo["paymentId"], user=user)


async def describe(ctx: Ctx, kind, id: str) -> dict:
    """Label and link for an item the user owns; the route's 404 otherwise."""
    tz, user = ctx.tz, ctx.user
    if kind == "task":
        try:
            t = await tasks.get(id, user=user)
        except HTTPException as exc:
            if exc.status_code == 404 and await _app_todo(ctx, id):
                raise ToolError("This to-do comes from the vournal app: it can't be deleted here.") from exc
            raise
        return {"label": t["title"], "link": f"/tasks?id={id}"}
    if kind == "event":
        e = await events.get(id, user=user)
        return {"label": f"{e['title']} · {local_day(e['startAt'], tz)}", "link": _event_link(id, e["startAt"], tz)}
    if kind == "reminder":
        r = await reminders.get(id, user=user)
        return {"label": r["title"], "link": f"/reminders?id={id}"}
    if kind == "transaction":
        t = await transactions.get(id, user=user)
        label = " · ".join(x for x in (f"{js_to_string(float(t['amount']))} {t['currency']}", t.get("title"), local_day(t["date"], tz)) if x)
        return {"label": label, "link": f"/finance/transactions?id={id}"}
    if kind == "debt":
        d = await debts.get(id, user=user)
        return {"label": f"{d['title']} · {d['who']}" if d.get("who") and d["who"] != d["title"] else d["title"], "link": f"/finance/debts?id={id}"}
    if kind == "note":
        n = await notes.get(id, user=user)
        return {"label": n["title"], "link": f"/notes?id={id}"}
    if kind == "shopping_item":
        s = await shopping.get_item(id, user=user)
        return {"label": s["name"], "link": f"/shopping?list={s['listId']}&id={id}"}
    if kind == "food_log":
        f = await db.foodlog.find_first(where={"id": id, "userId": ctx.user_id})
        if f is None:
            raise ToolError("Food entry not found")
        return {"label": f"{f.name or '—'} · {f.calories} kcal", "link": f"/health?date={to_json(f.date)[:10]}"}
    raise ToolError(f'Cannot delete a "{_js_str(kind)}"')
