"""The app's to-do rules, shared by its own routes (PATCH /todos/{id}, /details, DELETE)
and the web's /api/app-todos, so a to-do edited on either side changes the same way:
ticking a repeat moves it to its next day, details keep the day and time consistent in
the person's time zone, and a to-do belongs to one account.

Each function raises the app's own HTTPExceptions; the web turns them into its format.
"""

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from app.db import db
from app.task_intelligence import first_occurrence_day, metadata_out, next_occurrence_day


def _user_tz(tz_name: Optional[str]):
    """Resolve a stored IANA timezone name to a tzinfo, falling back to UTC for
    a missing or unrecognized value — a bad tz must never break ingestion."""
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse_due(value: Optional[str]) -> Optional[datetime]:
    """Parse an extracted dueDate string ("YYYY-MM-DD") into a datetime.

    Tolerates bad/None values by returning None rather than raising.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _compose_start_at(
    due: Optional[str], time_str: Optional[str], tz
) -> Optional[datetime]:
    """Combine an extracted dueDate ("YYYY-MM-DD") and time ("HH:MM") into a
    startAt UTC instant. Returns None unless BOTH are present and valid — an
    untimed todo (no spoken time) has no startAt.

    The extracted time is the speaker's LOCAL wall clock (the prompt is given
    their local now), so the day+time is interpreted in the user's timezone `tz`
    and converted to UTC. "2pm tomorrow" for an Asia/Beirut (UTC+3) user stores
    11:00Z. Rendering back to "2:00 PM" is the client's job.
    """
    day = _parse_due(due)
    if day is None or not time_str:
        return None
    try:
        hour, minute = (int(part) for part in time_str.split(":"))
        # dueDate only carries the calendar day; build the local wall-clock
        # datetime in the user's zone, then normalize to UTC for storage.
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        return local.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _next_occurrence_update(task, tz) -> Optional[dict]:
    """The fields that move a repeating to-do to its next day, or None when the
    series has no next day. The clock time stays the same on the person's wall."""
    today = datetime.now(tz).date()
    until = task.recurrenceUntil.astimezone(tz).date() if task.recurrenceUntil else None
    anchor = task.dueDate.date() if task.dueDate else today
    day = next_occurrence_day(
        anchor, task.recurrence, task.recurrenceInterval, task.recurrenceWeekdays, today, until
    )
    if day is None:
        return None
    clock = task.startAt.astimezone(tz).strftime("%H:%M") if task.startAt else None
    return {
        "done": False,
        "dueDate": _parse_due(day.isoformat()),
        "startAt": _compose_start_at(day.isoformat(), clock, tz),
    }


async def owned_todo(user, todo_id: str):
    todo = await db.todo.find_unique(where={"id": todo_id})
    if todo is None or todo.userId != user.id:
        raise HTTPException(status_code=404, detail="Todo not found")
    return todo


async def set_done(user, todo_id: str, done: bool, advance: bool = False):
    """Tick or untick; ticking a repeat with `advance` moves it to its next day instead."""
    existing = await owned_todo(user, todo_id)
    data = {"done": done}
    if done and advance and existing.recurrence:
        data = _next_occurrence_update(existing, _user_tz(user.timezone)) or data
    return await db.todo.update(where={"id": todo_id}, data=data)


async def update_details(user, todo_id: str, body):
    """The app's TaskDetailsRequest: only the fields sent change."""
    task = await owned_todo(user, todo_id)
    fields = body.model_fields_set
    data = {key: getattr(body, key) for key in fields - {"date", "time", "dismiss"}}
    if "urgency" in fields:
        data["urgencyUnclear"] = False
    if "recurrence" in fields:
        data["recurrenceUnclear"] = False
        if body.recurrence != "weekly" and "recurrenceWeekdays" not in fields:
            data["recurrenceWeekdays"] = []  # repeat days only mean something weekly
    tz = _user_tz(user.timezone)
    if "date" in fields or "time" in fields:
        day = (
            body.date
            if "date" in fields
            else task.dueDate.date().isoformat() if task.dueDate else None
        )
        clock = (
            body.time
            if "time" in fields
            else task.startAt.astimezone(tz).strftime("%H:%M") if task.startAt else None
        )
        if clock and not day:
            raise HTTPException(422, "Choose a date before adding a time.")
        start = _compose_start_at(day, clock, tz)
        if (
            start
            and start.astimezone(tz).strftime("%Y-%m-%d %H:%M") != f"{day} {clock}"
        ):
            raise HTTPException(
                422, "That time does not exist in your timezone. Choose another time."
            )
        if start:
            local = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=tz)
            if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
                raise HTTPException(
                    422,
                    "That time occurs twice during the clock change. Choose an unambiguous time.",
                )
        data.update(dueDate=_parse_due(day), startAt=start)
    if not data.get("dueDate", task.dueDate):
        first = first_occurrence_day(
            data.get("recurrence", task.recurrence),
            data.get("recurrenceWeekdays", task.recurrenceWeekdays),
            datetime.now(tz).date(),
        )
        if first:
            data["dueDate"] = _parse_due(first.isoformat())
    until = data.get("recurrenceUntil", task.recurrenceUntil)
    anchor = data.get("startAt", task.startAt) or data.get("dueDate", task.dueDate)
    if until and anchor and until < anchor:
        raise HTTPException(422, "The repeat end must be after the first occurrence.")
    # Explicit dismissal is persistent; no fabricated answer is written.
    data["dismissedQuestions"] = list(set(task.dismissedQuestions + body.dismiss))
    return await db.todo.update(where={"id": todo_id}, data=data)


async def delete(user, todo_id: str) -> None:
    await owned_todo(user, todo_id)
    await db.todo.delete(where={"id": todo_id})


def todo_fields(todo) -> dict:
    """The app's TodoOut fields of a row."""
    return {"id": todo.id, "text": todo.text, "dueDate": todo.dueDate, "startAt": todo.startAt, "done": todo.done, **metadata_out(todo)}


def local_day(todo, tz) -> Optional[date]:
    """The day a to-do sits on: its startAt's local day (timed), else its dueDate's."""
    if todo.startAt:
        return todo.startAt.astimezone(tz).date()
    return todo.dueDate.astimezone(timezone.utc).date() if todo.dueDate else None
