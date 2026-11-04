"""The weekly life score (0–100) and the ritual streak — pure maths (port of vournalOS
rituals/life-score.ts), tested by tests/vournal_os/test_life_score.py.

  tasks    30  share of this week's due tasks that got done
  habits   30  habit check-ins done / expected
  money    20  spending within income this month (1 when expenses ≤ income, else income / expenses)
  rituals  10  days planned + days reviewed, out of the week's days so far
  backlog  10  nothing overdue = full, up to 3 overdue = half, more = none

A part with nothing to measure is left out and the rest rescaled to 100, so an unused
section never lowers the score. The rituals routes come with the reports section.
"""

import math
from datetime import date, timedelta

from app.os.common.jsparse import js_sum

SCORE_WEIGHTS = {"tasks": 30, "habits": 30, "money": 20, "rituals": 10, "backlog": 10}


def _clamp01(x: float) -> float:
    return max(0, min(1, x))


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def level_of(total: float) -> str:
    return "great" if total >= 85 else "good" if total >= 70 else "okay" if total >= 50 else "low"


def compute_life_score(p: dict) -> dict:
    ratios: dict[str, float | None] = dict.fromkeys(SCORE_WEIGHTS)
    if p["tasks"]:
        due = p["tasks"]["completed"] + p["tasks"]["open"]
        if due > 0:
            ratios["tasks"] = _clamp01(p["tasks"]["completed"] / due)
    if p["habits"] and p["habits"]["expected"] > 0:
        ratios["habits"] = _clamp01(p["habits"]["done"] / p["habits"]["expected"])
    if p["money"] and p["money"]["income"] > 0:
        money = p["money"]
        ratios["money"] = 1 if money["expenses"] <= money["income"] else _clamp01(money["income"] / money["expenses"])
    days = max(1, p["rituals"]["days"])
    ratios["rituals"] = _clamp01((p["rituals"]["planned"] + p["rituals"]["reviewed"]) / (2 * days))
    if p["overdueNow"] is not None:
        ratios["backlog"] = 1 if p["overdueNow"] == 0 else 0.5 if p["overdueNow"] <= 3 else 0
    measurable = [k for k in SCORE_WEIGHTS if ratios[k] is not None]
    # rituals alone say nothing about a life: at least one real signal is needed
    has_signal = any(k != "rituals" for k in measurable)
    weight_sum = sum(SCORE_WEIGHTS[k] for k in measurable)
    if not has_signal or weight_sum == 0:
        return {"total": None, "level": None, "parts": [{"key": k, "ratio": ratios[k], "points": 0, "max": 0} for k in SCORE_WEIGHTS]}
    scale = 100 / weight_sum
    parts = []
    for key in SCORE_WEIGHTS:
        ratio = ratios[key]
        if ratio is None:
            parts.append({"key": key, "ratio": ratio, "points": 0, "max": 0})
            continue
        top = SCORE_WEIGHTS[key] * scale
        parts.append({"key": key, "ratio": ratio, "points": _js_round(ratio * top * 10) / 10, "max": _js_round(top * 10) / 10})
    total = _js_round(js_sum(part["points"] for part in parts))
    return {"total": total, "level": level_of(total), "parts": parts}


def ritual_streak(active_days: set[str], today: str) -> int:
    """Consecutive days with a plan or a review, ending today (or yesterday when today
    has nothing yet)."""
    def shift(key: str, n: int) -> str:
        return (date.fromisoformat(key) + timedelta(days=n)).isoformat()

    cursor = today if today in active_days else shift(today, -1)
    streak = 0
    while cursor in active_days:
        streak += 1
        cursor = shift(cursor, -1)
    return streak
