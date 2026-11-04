"""The app's to-dos on the web (spec "Shared to-dos"): one store, the app's Todo table, edited
through the app's own rules (app/todo_rules.py) so ticking a repeat, the details and deleting
behave the same on both sides. They keep the app's shape, marked `source: "app"` with
their journal entry, and show in the calendar, search, the home screen, insights and the
life score next to the web's own tasks."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request

from app import todo_rules
from app.db import db
from app.os.common import dates
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.query import _query_bool, unknown_params
from app.schemas import TaskDetailsRequest, UpdateTodoRequest

router = nest_router("/app-todos")

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")  # Python's weekday() order
# the app's urgency (none, !, !!, !!!) as the web's task priority
URGENCY_PRIORITY = {0: "MEDIUM", 1: "MEDIUM", 2: "HIGH", 3: "URGENT"}


def zone(tz: str):
    return ZoneInfo(dates.valid_zone(tz))


def occurs_on_day(todo, day: date, tz) -> bool:
    """Whether a repeat falls on `day`, after the to-do's own day (the app's occursOnDay:
    weeks count from the Monday of the to-do's week; a monthly repeat on the 31st skips
    shorter months)."""
    anchor = todo_rules.local_day(todo, tz)
    if not anchor or todo.done or not todo.recurrence:
        return False
    diff = (day - anchor).days
    if diff <= 0:
        return False
    if todo.recurrenceUntil and day > todo.recurrenceUntil.astimezone(tz).date():
        return False
    interval = todo.recurrenceInterval if isinstance(todo.recurrenceInterval, int) and todo.recurrenceInterval >= 1 else 1
    months = (day.year - anchor.year) * 12 + day.month - anchor.month
    if todo.recurrence == "daily":
        return diff % interval == 0
    if todo.recurrence == "weekly":
        days = todo.recurrenceWeekdays or [WEEKDAY_CODES[anchor.weekday()]]
        week = (day - (anchor - timedelta(days=anchor.weekday()))).days // 7
        return week % interval == 0 and WEEKDAY_CODES[day.weekday()] in days
    if todo.recurrence == "monthly":
        return day.day == anchor.day and months % interval == 0
    if todo.recurrence == "yearly":
        return day.day == anchor.day and months % (interval * 12) == 0
    return False


def view(todo) -> dict:
    """The app's TodoListItem, marked as the app's and linked to its journal entry."""
    return {**todo_rules.todo_fields(todo), "entryId": todo.entryId, "createdAt": todo.createdAt, "source": "app",
            "link": f"/journal/{todo.entryId}"}


def _sort_key(todo) -> tuple:
    """The app's order: dated ones by when, then undated ones by when they were captured."""
    when = todo.startAt or todo.dueDate
    return (0, when, todo.createdAt) if when is not None else (1, todo.createdAt, todo.createdAt)


async def for_user(user_id: str, **where) -> list:
    return await db.todo.find_many(where={"userId": user_id, **where})


def is_overdue(todo, today: date, now: datetime, tz) -> bool:
    """Not done, and its day has passed (a timed one: its time has)."""
    if todo.done:
        return False
    if todo.startAt:
        return todo.startAt < now
    day = todo_rules.local_day(todo, tz)
    return day is not None and day < today


def due_instant(todo, tz: str) -> datetime | None:
    """When it's due, as the web's tasks store it: the time, or local midnight of its day."""
    if todo.startAt:
        return todo.startAt
    day = todo_rules.local_day(todo, zone(tz))
    return dates.local_midnight(day.isoformat(), tz) if day else None


def as_task(todo, tz: str, today: date, now: datetime) -> dict:
    """The home screen's task shape, so app to-dos list with the web's own tasks."""
    due = due_instant(todo, tz)
    return {
        "id": todo.id, "title": todo.text, "status": "COMPLETED" if todo.done else "TODO", "priority": URGENCY_PRIORITY.get(todo.urgency, "MEDIUM"),
        "dueAt": _iso(due) if due else None, "hasDueTime": bool(todo.startAt), "completedAt": None, "focusDate": None, "lifeArea": None,
        "estimatedMinutes": None, "isRecurring": bool(todo.recurrence), "isSubtask": False,
        "isOverdue": is_overdue(todo, today, now, zone(tz)), "category": None, "project": None, "link": f"/journal/{todo.entryId}",
        "source": "app", "entryId": todo.entryId,
    }


def _iso(moment: datetime) -> str:
    return dates._utc(moment).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def calendar_items(todos, start_day: str, end_day: str, tz: str, include_completed: bool, now: datetime) -> list[dict]:
    """Feed items: each to-do on its own day, and its repeats on later days in range."""
    tzinfo = zone(tz)
    first, last = date.fromisoformat(start_day), date.fromisoformat(end_day)
    today = now.astimezone(tzinfo).date()
    out = []
    for t in todos:
        if t.done and not include_completed:
            continue
        own = todo_rules.local_day(t, tzinfo)
        if own is None:
            continue
        days = [own] if first <= own <= last else []
        if t.recurrence and not t.done:
            day = max(first, own + timedelta(days=1))
            while day <= last:
                if occurs_on_day(t, day, tzinfo):
                    days.append(day)
                day += timedelta(days=1)
        for day in days:
            copy = day != own
            if t.startAt:
                clock = t.startAt.astimezone(tzinfo)
                start = _iso(datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=tzinfo))
            else:
                start = day.isoformat()
            out.append({
                "id": f"appTodo:{t.id}:{day.isoformat()}", "sourceType": "appTodo", "sourceId": t.id, "title": t.text, "start": start,
                "end": start, "allDay": not t.startAt, "color": None, "link": f"/journal/{t.entryId}",
                "priority": URGENCY_PRIORITY.get(t.urgency, "MEDIUM"),
                "meta": {"done": t.done, "completed": t.done, "entryId": t.entryId, "kind": t.kind, "urgency": t.urgency, "category": t.category,
                         "recurrence": t.recurrence, "repeatCopy": copy, "overdue": not copy and is_overdue(t, today, now, tzinfo)},
            })
    return out


async def task_counts(user_id: str, tz: str, now: datetime, start_day: date | None = None, end_day: date | None = None) -> dict:
    """Overdue and due-today counts (insights), and done / open in a day range (life score)."""
    tzinfo = zone(tz)
    today = now.astimezone(tzinfo).date()
    todos = await for_user(user_id)
    overdue = [t for t in todos if is_overdue(t, today, now, tzinfo)]
    oldest = min((todo_rules.local_day(t, tzinfo) for t in overdue), default=None)
    in_range = [t for t in todos if start_day and (d := todo_rules.local_day(t, tzinfo)) and start_day <= d <= end_day]
    return {
        "overdue": len(overdue),
        "oldestOverdueDay": oldest,
        "dueToday": sum(1 for t in todos if not t.done and todo_rules.local_day(t, tzinfo) == today),
        "done": sum(1 for t in in_range if t.done),
        "open": sum(1 for t in in_range if not t.done),
    }


# ----- routes --------------------------------------------------------------------------


def _web_error(exc: HTTPException) -> HTTPException:
    """The app's error in the web's format (same status and words)."""
    return nest_error(exc.status_code, exc.detail if isinstance(exc.detail, str) else "Request failed")


@router.get("")
async def list_todos(request: Request, user=Depends(plus_user)):
    """The account's app to-dos, soonest first (`?done=true|false`)."""
    errors = unknown_params(request, ("done",))
    done = _query_bool(request, "done", errors)
    if errors:
        raise nest_error(400, errors)
    todos = await for_user(user.id, **({} if done is None else {"done": done}))
    todos.sort(key=_sort_key)
    return {"items": [view(t) for t in todos], "total": len(todos)}


@router.patch("/{todo_id}")
async def set_done(todo_id: str, body: UpdateTodoRequest, user=Depends(plus_user)):
    try:
        return view(await todo_rules.set_done(user, todo_id, body.done, body.advance))
    except HTTPException as exc:
        raise _web_error(exc) from None


@router.patch("/{todo_id}/details")
async def update_details(todo_id: str, body: TaskDetailsRequest, user=Depends(plus_user)):
    try:
        return view(await todo_rules.update_details(user, todo_id, body))
    except HTTPException as exc:
        raise _web_error(exc) from None


@router.delete("/{todo_id}")
async def delete(todo_id: str, user=Depends(plus_user)):
    try:
        await todo_rules.delete(user, todo_id)
    except HTTPException as exc:
        raise _web_error(exc) from None
    return {"ok": True}
