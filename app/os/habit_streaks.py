"""Habit maths — pure (port of vournalOS habits/habit-streaks.ts), tested by
tests/vournal_os/test_habit_streaks.py (its spec's cases).

Days are integer "day numbers" (days since 1970-01-01 on the UTC calendar), so no
timezone enters: the caller turns the user's local today into "YYYY-MM-DD" once.

- A day is done when its count reaches targetPerDay.
- DAILY: every day is scheduled. CUSTOM: only daysOfWeek (0 = Sun … 6 = Sat; empty =
  every day). WEEKLY: timesPerWeek done days per week (from the user's weekStartsOn).
- Streaks count consecutive scheduled days (WEEKLY: weeks meeting the quota); today
  (or this week) not done yet is pending, not a break.
- Completion = achieved / expected over a range; a pending today never counts as missed.
- The effective start is the earlier of startDate and the first logged day.
"""

import math
import re
from datetime import date, timedelta

from app.os.common.jsparse import roll_date

_EPOCH = date(1970, 1, 1)
_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})")


def day_num(text: str) -> int:
    m = _DATE.match(text)
    if not m:
        raise ValueError(f"Invalid date: {text}")
    return (roll_date(int(m[1]), int(m[2]), int(m[3])) - _EPOCH).days


def day_str(n: int) -> str:
    return (_EPOCH + timedelta(days=n)).isoformat()


def weekday_of(n: int) -> int:
    """0 = Sunday … 6 = Saturday (1970-01-01 was a Thursday)."""
    return (n + 4) % 7


def week_start_of(n: int, week_starts_on: int) -> int:
    return n - ((weekday_of(n) - week_starts_on + 7) % 7)


def times_per_week_of(h: dict) -> int:
    t = h.get("timesPerWeek") or 1
    return min(7, max(1, math.trunc(t)))


def is_scheduled_weekday(h: dict, n: int) -> bool:
    if h["frequency"] != "CUSTOM" or not h["daysOfWeek"]:
        return True
    return weekday_of(n) in h["daysOfWeek"]


def count_map(logs: list[dict]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for log in logs:
        n = day_num(log["date"])
        counts[n] = counts.get(n, 0) + log["count"]
    return counts


def effective_start(h: dict, counts: dict[int, int]) -> int:
    start = day_num(h["startDate"])
    for n, c in counts.items():
        if c > 0 and n < start:
            start = n
    return start


def _target(h: dict) -> int:
    return max(1, h.get("targetPerDay") or 1)


def _is_done(h: dict, counts: dict[int, int], n: int) -> bool:
    return counts.get(n, 0) >= _target(h)


def _done_days_in_week(h: dict, counts: dict[int, int], ws: int, today: int) -> int:
    return sum(1 for n in range(ws, min(ws + 6, today) + 1) if _is_done(h, counts, n))


def _week_quota(h: dict, ws: int, start: int) -> int:
    """The weekly quota, smaller for a first week that starts mid-week."""
    available = min(7, ws + 7 - max(ws, start))
    return min(times_per_week_of(h), max(0, available))


def _round1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def _completion(achieved: int, expected: int) -> dict:
    return {"achieved": achieved, "expected": expected, "rate": _round1(min(100, achieved / expected * 100)) if expected > 0 else None}


def compute_streaks(h: dict, counts: dict[int, int], today: int, week_starts_on: int) -> dict:
    start = effective_start(h, counts)
    if start > today:
        return {"current": 0, "best": 0}
    if h["frequency"] == "WEEKLY":
        first_week, cur_week = week_start_of(start, week_starts_on), week_start_of(today, week_starts_on)

        def met(ws: int) -> bool:
            quota = _week_quota(h, ws, start)
            return quota > 0 and _done_days_in_week(h, counts, ws, today) >= quota

        current = 0
        ws = cur_week if met(cur_week) else cur_week - 7  # this week is pending
        while ws >= first_week and met(ws):
            current += 1
            ws -= 7
        best = run = 0
        for w in range(first_week, cur_week + 1, 7):
            if met(w):
                run += 1
                best = max(best, run)
            elif w != cur_week:
                run = 0
        return {"current": current, "best": max(best, current)}

    current = 0
    for n in range(today, start - 1, -1):
        if not is_scheduled_weekday(h, n):
            continue
        if _is_done(h, counts, n):
            current += 1
        elif n == today:
            continue  # today pending
        else:
            break
    best = run = 0
    for n in range(start, today + 1):
        if not is_scheduled_weekday(h, n):
            continue
        if _is_done(h, counts, n):
            run += 1
            best = max(best, run)
        elif n != today:
            run = 0
    return {"current": current, "best": max(best, current)}


def completion_between(h: dict, counts: dict[int, int], from_n: int, to_n: int, today: int, week_starts_on: int) -> dict:
    start = effective_start(h, counts)
    lo, hi = max(from_n, start), min(to_n, today)
    if lo > hi:
        return _completion(0, 0)
    achieved = expected = 0
    if h["frequency"] == "WEEKLY":
        cur_week = week_start_of(today, week_starts_on)
        for ws in range(week_start_of(lo, week_starts_on), week_start_of(hi, week_starts_on) + 1, 7):
            quota = _week_quota(h, ws, start)
            if quota == 0:
                continue
            a = min(_done_days_in_week(h, counts, ws, today), quota)
            if ws == cur_week:
                # only what can no longer be reached counts as missed
                days_left = ws + 6 - today + (0 if _is_done(h, counts, today) else 1)
                missed = max(0, quota - a - days_left)
                achieved += a
                expected += a + missed
            else:
                achieved += a
                expected += quota
        return _completion(achieved, expected)
    for n in range(lo, hi + 1):
        if not is_scheduled_weekday(h, n):
            continue
        done = _is_done(h, counts, n)
        if n == today and not done:
            continue  # pending
        expected += 1
        achieved += done
    return _completion(achieved, expected)


def day_status(h: dict, counts: dict[int, int], n: int) -> dict:
    count = counts.get(n, 0)
    return {"date": day_str(n), "count": count, "done": count >= _target(h), "scheduled": n >= effective_start(h, counts) and is_scheduled_weekday(h, n)}


def is_scheduled_on_day(h: dict, counts: dict[int, int], n: int, week_starts_on: int) -> bool:
    if h.get("isArchived"):
        return False
    if n < day_num(h["startDate"]):
        return False
    if h["frequency"] == "WEEKLY":
        ws = week_start_of(n, week_starts_on)
        return _done_days_in_week(h, counts, ws, n - 1) < times_per_week_of(h) or _is_done(h, counts, n)
    return is_scheduled_weekday(h, n)


def compute_habit(h: dict, logs: list[dict], today_str: str, week_starts_on: int) -> dict:
    """Everything the habit list and detail show; today_str is the user's local date."""
    counts = count_map(logs)
    today = day_num(today_str)
    streaks = compute_streaks(h, counts, today, week_starts_on)
    cur_week = week_start_of(today, week_starts_on)
    month_start = day_num(f"{today_str[:7]}-01")
    start = effective_start(h, counts)
    done_days = [n for n in counts if _is_done(h, counts, n)]
    today_count = counts.get(today, 0)
    return {
        "todayCount": today_count,
        "doneToday": today_count >= _target(h),
        "isScheduledToday": is_scheduled_on_day(h, counts, today, week_starts_on),
        "currentStreak": streaks["current"],
        "bestStreak": streaks["best"],
        "streakUnit": "week" if h["frequency"] == "WEEKLY" else "day",
        "weekProgress": (
            {"done": _done_days_in_week(h, counts, cur_week, today), "target": times_per_week_of(h)} if h["frequency"] == "WEEKLY" else None
        ),
        "weeklyCompletion": completion_between(h, counts, cur_week, cur_week + 6, today, week_starts_on)["rate"],
        "monthlyCompletion": completion_between(h, counts, month_start, today, today, week_starts_on)["rate"],
        "overallCompletion": completion_between(h, counts, start, today, today, week_starts_on)["rate"],
        "totalDoneDays": len(done_days),
        "lastDoneDate": day_str(max(done_days)) if done_days else None,
        "last7Days": [day_status(h, counts, n) for n in range(today - 6, today + 1)],
    }
