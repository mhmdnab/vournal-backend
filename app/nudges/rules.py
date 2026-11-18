"""Facts for nudges and the rule-based text (design 2026-11-18).

app_facts reads the app's own data (to-dos due today, today's entry, the journal streak) and is
all a free user's nudges use. life_facts reads vournal life (Plus) as the dashboard shows it,
keeping names and counts only: never an amount of money. The rule-based text is what free users
get, and what Plus users get when the AI can't write."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from app.db import db
from app.os.dashboard import get_dashboard
from app.os.insights import insights_for
from app.review import IN_JOURNAL

STREAK_LOOKBACK = 120  # days; a longer streak still reads as "120-day"
SOON_DAYS = 3
MAX_BODY = 140


@dataclass
class Nudge:
    title: str
    body: str
    opens: Literal["life", "record"]
    prompt: str | None = None


def _utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=timezone.utc)


async def app_facts(user, tz: ZoneInfo, today: date) -> dict:
    """{"todosDue", "recordedToday", "streak"} in the phone's local day."""
    todos_due = await db.todo.count(where={"userId": user.id, "done": False,
                                           "dueDate": {"gte": _utc_midnight(today), "lt": _utc_midnight(today + timedelta(days=1))}})
    since = datetime.combine(today - timedelta(days=STREAK_LOOKBACK), time(0), tzinfo=tz)
    rows = await db.entry.find_many(where={"userId": user.id, "createdAt": {"gte": since}, "AND": [IN_JOURNAL]})
    days = {r.createdAt.astimezone(tz).date() for r in rows}
    recorded_today = today in days
    day, streak = (today if recorded_today else today - timedelta(days=1)), 0
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return {"todosDue": todos_due, "recordedToday": recorded_today, "streak": streak}


def _items(section) -> list:
    return (section or {}).get("items") or []


def _recently_overdue(d: dict) -> list[dict]:
    try:
        today = date.fromisoformat(d["today"])
        zone = ZoneInfo(d.get("timezone") or "UTC")
    except (KeyError, TypeError, ValueError):
        return []
    out = []
    for t in _items(d.get("overdueTasks")):
        if t.get("source") == "app" or not t.get("dueAt"):
            continue
        due = datetime.fromisoformat(t["dueAt"].replace("Z", "+00:00")).astimezone(zone).date()
        if due >= today - timedelta(days=1):
            out.append(t)
    return out


async def life_facts(user) -> dict:
    """vournal life as names and counts (no amounts), plus the midday triggers."""
    d = await get_dashboard(user)
    counts = d.get("counts") or {}
    bills = [b for b in _items(d.get("upcomingBills")) if b.get("isNext", True) and 0 <= b["daysUntil"] <= SOON_DAYS]
    habits = [h for h in _items(d.get("habits")) if h.get("frequency") != "WEEKLY"]
    birthdays = [b for b in _items(d.get("birthdays")) if 0 <= b["daysUntil"] <= SOON_DAYS]
    triggers: list[str] = []
    try:
        insights = await insights_for(user)
    except Exception:  # noqa: BLE001 — no insights means fewer triggers, not a failed nudge
        insights = []
    for ins in insights:
        iid = ins.get("id", "")
        if iid.startswith("habit-risk:") or iid.startswith("goal-behind:"):
            triggers.append(ins.get("title") or iid)
    # a follow-up: a vournal life task that became overdue yesterday or today, not one overdue for weeks
    triggers += [f"{t['title']} is overdue" for t in _recently_overdue(d)]
    for b in _items(d.get("budgets")):
        if b.get("isOver"):
            triggers.append(f"The {b['name']} budget is over its limit")
        elif (b.get("percentUsed") or 0) >= 90:
            triggers.append(f"The {b['name']} budget is almost used up")
    triggers += [f"{b['name']} is due tomorrow" for b in bills if b["daysUntil"] == 1]
    triggers += [f"{b['title']} is tomorrow" for b in birthdays if b["daysUntil"] == 1]
    return {
        "tasksToday": [t["title"] for t in _items(d.get("todayTasks")) if t.get("status") != "COMPLETED"],
        "overdue": (d.get("overdueTasks") or {}).get("count", 0),
        "events": counts.get("eventsToday", 0),
        "reminders": counts.get("remindersToday", 0),
        "billsSoon": [{"name": b["name"], "inDays": b["daysUntil"]} for b in bills],
        "habits": [{"name": h["name"], "streak": h["currentStreak"], "doneToday": h["doneToday"]} for h in habits],
        "goals": [{"name": g["name"], "progress": g["progress"]} for g in _items(d.get("goals"))],
        "birthdays": [{"name": b["title"], "inDays": b["daysUntil"]} for b in birthdays],
        "triggers": triggers,
    }


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _when(days: int) -> str:
    return "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"


def free_text(moment: str, facts: dict) -> Nudge | None:
    if moment == "morning" and facts["todosDue"] >= 1:
        return Nudge("Today", f"{_plural(facts['todosDue'], 'to-do', 'to-dos')} due today.", "life")
    if moment == "evening" and not facts["recordedToday"]:
        if facts["streak"] >= 2:
            return Nudge("Evening check-in", f"No entry yet today. Keep your {facts['streak']}-day streak going.", "record")
        return Nudge("Evening check-in", "No entry yet today. How was your day?", "record")
    return None


def plus_fallback_text(moment: str, app: dict, life: dict) -> Nudge | None:
    if moment == "evening":
        return free_text("evening", app)
    if moment != "morning":
        return None
    parts: list[str] = []
    if life["tasksToday"]:
        parts.append(f"{_plural(len(life['tasksToday']), 'task', 'tasks')} today")
    if app.get("todosDue"):
        parts.append(f"{_plural(app['todosDue'], 'to-do', 'to-dos')} today")
    if not parts and life["events"]:
        parts.append(f"{_plural(life['events'], 'event', 'events')} today")
    for b in life["billsSoon"][:1]:
        parts.append(f"{b['name']} due {_when(b['inDays'])}")
    streaks = sorted((h for h in life["habits"] if h["streak"] >= 2 and not h["doneToday"]), key=lambda h: -h["streak"])
    for h in streaks[:1]:
        parts.append(f"day {h['streak'] + 1} of your {h['name']} streak")
    for b in life["birthdays"][:1]:
        parts.append(f"{b['name']} {_when(b['inDays'])}")
    if not parts:
        return None
    body = " · ".join(parts[:3])
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 1].rstrip() + "…"
    return Nudge("Your day", body[0].upper() + body[1:], "life")
