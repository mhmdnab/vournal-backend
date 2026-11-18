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
from app.review import LISTED_SQL

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
    # the distinct local days only, worked out in the database (not every entry of four months)
    sql = (f'SELECT DISTINCT ((e."createdAt" AT TIME ZONE \'UTC\') AT TIME ZONE $3)::date::text AS day FROM "Entry" e '
           f'WHERE e."userId" = $1 AND e."createdAt" >= $2::timestamp AND {LISTED_SQL}')
    start = since.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    try:
        rows = await db.query_raw(sql, user.id, start, tz.key)
    except Exception:  # noqa: BLE001 — a zone Python knows and the database doesn't: count in UTC rather than skip the nudge
        rows = await db.query_raw(sql, user.id, start, "UTC")
    days = {date.fromisoformat(str(r["day"])[:10]) for r in rows}
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


# Arabic (an account whose vournal life language is Arabic): Modern Standard Arabic, counted the
# Arabic way (one, two, 3–10, 11+). An app to-do and a vournal life task are both "مهمة", so the
# morning adds them up into one count.
def _ar_count(n: int, one: str, two: str, few: str, many: str) -> str:
    return one if n == 1 else two if n == 2 else f"{n} {few}" if 3 <= n <= 10 else f"{n} {many}"


def _ar_tasks(n: int) -> str:
    return _ar_count(n, "مهمة واحدة", "مهمتان", "مهام", "مهمة")


def _ar_when(days: int) -> str:
    return "اليوم" if days <= 0 else "غدًا" if days == 1 else f"بعد {days} أيام" if days <= 10 else f"بعد {days} يومًا"


def free_text(moment: str, facts: dict, lang: str = "en") -> Nudge | None:
    ar = lang == "ar"
    if moment == "morning" and facts["todosDue"] >= 1:
        if ar:
            return Nudge("اليوم", f"{_ar_tasks(facts['todosDue'])} مستحقة اليوم.", "life")
        return Nudge("Today", f"{_plural(facts['todosDue'], 'to-do', 'to-dos')} due today.", "life")
    if moment == "evening" and not facts["recordedToday"]:
        if facts["streak"] >= 2:
            if ar:
                return Nudge("مراجعة المساء", f"لا يوجد تسجيل اليوم بعد. حافظ على سلسلتك المستمرة منذ {facts['streak']} أيام.", "record")
            return Nudge("Evening check-in", f"No entry yet today. Keep your {facts['streak']}-day streak going.", "record")
        if ar:
            return Nudge("مراجعة المساء", "لا يوجد تسجيل اليوم بعد. كيف كان يومك؟", "record")
        return Nudge("Evening check-in", "No entry yet today. How was your day?", "record")
    return None


def plus_fallback_text(moment: str, app: dict, life: dict, lang: str = "en") -> Nudge | None:
    if moment == "evening":
        return free_text("evening", app, lang)
    if moment != "morning":
        return None
    ar = lang == "ar"
    parts: list[str] = []
    if ar:
        tasks = len(life["tasksToday"]) + (app.get("todosDue") or 0)
        if tasks:
            parts.append(f"{_ar_tasks(tasks)} اليوم")
        elif life["events"]:
            parts.append(f"{_ar_count(life['events'], 'حدث واحد', 'حدثان', 'أحداث', 'حدثًا')} اليوم")
    else:
        if life["tasksToday"]:
            parts.append(f"{_plural(len(life['tasksToday']), 'task', 'tasks')} today")
        if app.get("todosDue"):
            parts.append(f"{_plural(app['todosDue'], 'to-do', 'to-dos')} today")
        if not parts and life["events"]:
            parts.append(f"{_plural(life['events'], 'event', 'events')} today")
    for b in life["billsSoon"][:1]:
        parts.append(f"{b['name']} مستحق {_ar_when(b['inDays'])}" if ar else f"{b['name']} due {_when(b['inDays'])}")
    streaks = sorted((h for h in life["habits"] if h["streak"] >= 2 and not h["doneToday"]), key=lambda h: -h["streak"])
    for h in streaks[:1]:
        parts.append(f"اليوم {h['streak'] + 1} من سلسلة {h['name']}" if ar else f"day {h['streak'] + 1} of your {h['name']} streak")
    for b in life["birthdays"][:1]:
        parts.append(f"{b['name']} {_ar_when(b['inDays'])}" if ar else f"{b['name']} {_when(b['inDays'])}")
    if not parts:
        return None
    body = " · ".join(parts[:3])
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 1].rstrip() + "…"
    if ar:
        return Nudge("يومك", body, "life")
    return Nudge("Your day", body[0].upper() + body[1:], "life")
