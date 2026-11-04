"""Habit schedules, completion and streaks for the overview sections (port of vournalOS
insights/lib/habits.ts; the habits section's own maths is habit_streaks.py).

A habit is done on a day when its log count reaches targetPerDay. DAILY: every day;
CUSTOM: its daysOfWeek (0 = Sun … 6 = Sat; none = every day); WEEKLY: timesPerWeek days
a week, any days (streaks counted in weeks). Habits are dicts with the Prisma fields.
"""

import math

from app.os.insight_text import js_round
from app.os.insight_time import add_days_key, date_only_key, each_day_key, week_start_key, weekday_of_key


def index_habit_logs(logs) -> dict[str, dict[str, int]]:
    """habitId → (day key → count)."""
    idx: dict[str, dict[str, int]] = {}
    for log in logs:
        days = idx.setdefault(log["habitId"], {})
        key = date_only_key(log["date"])
        days[key] = days.get(key, 0) + log["count"]
    return idx


def habit_start_key(h) -> str:
    return date_only_key(h["startDate"])


def is_done_on(h, idx, key: str) -> bool:
    return idx.get(h["id"], {}).get(key, 0) >= max(1, h["targetPerDay"])


def count_on(h, idx, key: str) -> int:
    return idx.get(h["id"], {}).get(key, 0)


def is_scheduled_on(h, key: str) -> bool:
    """Scheduled on this day? (Every day is eligible for a WEEKLY habit.)"""
    if key < habit_start_key(h):
        return False
    if h["frequency"] == "CUSTOM":
        return not h["daysOfWeek"] or weekday_of_key(key) in h["daysOfWeek"]
    return True


def times_per_week(h) -> int:
    return min(7, max(1, h["timesPerWeek"] or 1))


def done_in_week(h, idx, key: str, week_starts_on: int, up_to: str | None = None) -> int:
    start = week_start_key(key, week_starts_on)
    n = 0
    for i in range(7):
        k = add_days_key(start, i)
        if up_to and k > up_to:
            break
        if is_done_on(h, idx, k):
            n += 1
    return n


def is_due_today(h, idx, today: str, week_starts_on: int) -> bool:
    """In "today's habits"? DAILY/CUSTOM: scheduled today. WEEKLY: target not met yet (or done today)."""
    if today < habit_start_key(h):
        return False
    if h["frequency"] != "WEEKLY":
        return is_scheduled_on(h, today)
    if is_done_on(h, idx, today):
        return True
    return done_in_week(h, idx, today, week_starts_on, today) < times_per_week(h)


def current_streak(h, idx, today: str, week_starts_on: int, max_days: int = 800) -> int:
    """DAILY/CUSTOM: scheduled days done in a row back from today (today not done yet
    doesn't break it). WEEKLY: weeks meeting the target (this week once it's met)."""
    start = habit_start_key(h)
    if h["frequency"] == "WEEKLY":
        target = times_per_week(h)
        wk = week_start_key(today, week_starts_on)
        streak = 1 if done_in_week(h, idx, wk, week_starts_on) >= target else 0
        wk = add_days_key(wk, -7)
        for _ in range(math.ceil(max_days / 7)):
            if add_days_key(wk, 6) < start:
                break
            if done_in_week(h, idx, wk, week_starts_on) >= target:
                streak += 1
            else:
                break
            wk = add_days_key(wk, -7)
        return streak
    streak = 0
    k = today
    if is_scheduled_on(h, k) and not is_done_on(h, idx, k):
        k = add_days_key(k, -1)
    for _ in range(max_days):
        if k < start:
            break
        if is_scheduled_on(h, k):
            if is_done_on(h, idx, k):
                streak += 1
            else:
                break
        k = add_days_key(k, -1)
    return streak


def best_streak_in_range(h, idx, start_key: str, end_key: str, week_starts_on: int) -> int:
    """Longest streak (days, or weeks for WEEKLY) inside [start, end]."""
    start = max(habit_start_key(h), start_key)
    if start > end_key:
        return 0
    best = run = 0
    if h["frequency"] == "WEEKLY":
        target = times_per_week(h)
        wk = week_start_key(start, week_starts_on)
        while wk <= end_key:
            if done_in_week(h, idx, wk, week_starts_on) >= target:
                run += 1
                best = max(best, run)
            elif add_days_key(wk, 6) <= end_key:
                run = 0  # an unfinished current week doesn't break it
            wk = add_days_key(wk, 7)
        return best
    for k in each_day_key(start, end_key):
        if not is_scheduled_on(h, k):
            continue
        if is_done_on(h, idx, k):
            run += 1
            best = max(best, run)
        elif k != end_key:
            run = 0
    return best


def completion_in_range(h, idx, start_key: str, end_key: str, today: str) -> dict:
    """{expected, done, percent} over [start, end], the end clamped to today (the future
    is never missed)."""
    start = max(habit_start_key(h), start_key)
    end = min(end_key, today)
    if start > end:
        return {"expected": 0, "done": 0, "percent": None}
    days = each_day_key(start, end)
    if h["frequency"] == "WEEKLY":
        expected = max(1, js_round((times_per_week(h) * len(days)) / 7))
        done = min(sum(1 for k in days if is_done_on(h, idx, k)), expected)
        return {"expected": expected, "done": done, "percent": js_round((done / expected) * 100)}
    expected = done = 0
    for k in days:
        if not is_scheduled_on(h, k):
            continue
        expected += 1
        if is_done_on(h, idx, k):
            done += 1
    return {"expected": expected, "done": done, "percent": js_round((done / expected) * 100) if expected else None}


def aggregate_completion(habits, idx, start_key: str, end_key: str, today: str) -> dict:
    expected = done = 0
    for h in habits:
        c = completion_in_range(h, idx, start_key, end_key, today)
        expected += c["expected"]
        done += c["done"]
    return {"expected": expected, "done": done, "percent": js_round((done / expected) * 100) if expected else None}
