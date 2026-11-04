"""/api/rituals (port of vournalOS RitualsService): the morning plan, the evening review,
the weekly review and the weekly life score. The lists the rituals show come from
/api/dashboard; this stores what the user did (DayLog, WeekReview) and works out the score."""

import re
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request

from app.db import db
from app.os import reports
from app.os.common import dates, validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, number, text
from app.os.common.currency import get_converter, round_money
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.jsparse import _JS_SPACE
from app.os.common.query import unknown_params
from app.os.insight_time import add_days_key, diff_days_key, key_to_date, week_start_key
from app.os.life_score import compute_life_score, ritual_streak
from app.os.profile import ensure_os_profile

router = nest_router("/rituals")

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_DAY_MESSAGE = "must match /^\\d{4}-\\d{2}-\\d{2}$/ regular expression"


# ----- bodies --------------------------------------------------------------------------


def trim_or_null(value):
    """@Transform: a string is trimmed, and an empty one becomes null."""
    return (value.strip(_JS_SPACE) or None) if isinstance(value, str) else value


def _day_rule(value, field):
    return [] if isinstance(value, str) and _DAY.match(value) else [f"{field} {_DAY_MESSAGE}"]


def _text_or_null(maximum: int):
    """Trimmed to null first; null then skips the rules (@IsOptional)."""
    check = text(maximum)
    return lambda v, f: [] if trim_or_null(v) is None else check(trim_or_null(v), f)


def _priorities_rule(value, field):
    """@IsArray() @ArrayMaxSize(3) @IsString({ each: true }) @MaxLength(160, { each: true })."""
    items = value if isinstance(value, list) else [value]
    problems = []
    if any(not isinstance(v, str) or len(v.encode("utf-16-le")) // 2 > 160 for v in items):
        problems.append(f"each value in {field} must be shorter than or equal to 160 characters")
    if any(not isinstance(v, str) for v in items):
        problems.append(f"each value in {field} must be a string")
    if not isinstance(value, list) or len(value) > 3:
        problems.append(f"{field} must contain no more than 3 elements")
    if not isinstance(value, list):
        problems.append(f"{field} must be an array")
    return problems


class DayBody(RuleBody):
    RULES = (
        ("date", True, False, _day_rule),
        ("plan", True, False, validators.bool_rules),
        ("review", True, False, validators.bool_rules),
        ("intention", True, False, _text_or_null(300)),
        ("win", True, False, _text_or_null(300)),
        ("mood", True, False, number(1, 5)),
        ("note", True, False, _text_or_null(2000)),
    )
    date: object = None
    plan: object = None
    review: object = None
    intention: object = None
    win: object = None
    mood: object = None
    note: object = None


class WeekBody(RuleBody):
    RULES = (
        ("date", True, False, _day_rule),
        ("priorities", True, False, _priorities_rule),
        ("wentWell", True, False, _text_or_null(1000)),
        ("improve", True, False, _text_or_null(1000)),
        ("done", True, False, validators.bool_rules),
    )
    date: object = None
    priorities: object = None
    wentWell: object = None
    improve: object = None
    done: object = None


# ----- the context, the score and the review target ------------------------------------


async def _context(user) -> dict:
    s = await ensure_os_profile(user)
    today = dates.local_date_string(datetime.now(timezone.utc), s.timezone)
    return {"tz": s.timezone, "today": today, "weekStartsOn": s.weekStartsOn, "weekStart": week_start_key(today, s.weekStartsOn),
            "sections": set(s.enabledSections)}


def _key_of(value: datetime) -> str:
    return dates._utc(value).date().isoformat()


async def _review_target(user, ctx: dict) -> dict:
    """The week to review now, and whether to nudge: the last 2 days of a week → this week;
    the first 3 days after an unreviewed week → last week (if the account existed then)."""
    day_index = diff_days_key(ctx["weekStart"], ctx["today"])
    last_week = add_days_key(ctx["weekStart"], -7)
    current = await db.weekreview.find_unique(where={"userId_weekStart": {"userId": user.id, "weekStart": key_to_date(ctx["weekStart"])}})
    previous = await db.weekreview.find_unique(where={"userId_weekStart": {"userId": user.id, "weekStart": key_to_date(last_week)}})
    has_history = await db.user.count(where={"id": user.id, "createdAt": {"lt": key_to_date(ctx["weekStart"])}})
    reviewed = bool(current and current.reviewedAt)
    if day_index >= 5:
        return {"weekStart": ctx["weekStart"], "weekEnd": add_days_key(ctx["weekStart"], 6), "due": not reviewed, "reviewed": reviewed}
    if day_index <= 2 and has_history > 0 and not (previous and previous.reviewedAt):
        return {"weekStart": last_week, "weekEnd": add_days_key(last_week, 6), "due": True, "reviewed": False}
    return {"weekStart": ctx["weekStart"], "weekEnd": add_days_key(ctx["weekStart"], 6), "due": False, "reviewed": reviewed}


async def _month_to_date(user, month_from: str, tz: str, last_day: str) -> dict:
    """Income and expenses from the 1st of the month to the end of `last_day`."""
    start = dates.start_of_day(key_to_date(month_from) + timedelta(hours=12), tz)
    end = dates.end_of_day(key_to_date(last_day) + timedelta(hours=12), tz)
    rows = await db.transaction.find_many(where={"userId": user.id, "type": {"in": ["INCOME", "EXPENSE"]}, "date": {"gte": start, "lte": end}})
    fx = await get_converter(user.id)
    income = expenses = 0.0
    for r in rows:
        v = fx.to_default(r.amount, r.currency)
        if r.type == "INCOME":
            income += v
        else:
            expenses += v
    return {"income": round_money(income), "expenses": round_money(expenses)}


async def _score_for(user, ctx: dict, week_start: str, report: dict | None = None) -> dict:
    week_end = add_days_key(week_start, 6)
    last_day = (week_start if ctx["today"] < week_start else ctx["today"]) if ctx["today"] < week_end else week_end
    days_elapsed = diff_days_key(week_start, last_day) + 1
    report = report or await reports.weekly(user, week_start)
    logs = await db.daylog.find_many(where={"userId": user.id, "date": {"gte": key_to_date(week_start), "lte": key_to_date(last_day)}})
    money = await _month_to_date(user, f"{last_day[:7]}-01", ctx["tz"], last_day) if "finance" in ctx["sections"] else None
    t, h = report["tasks"], report["habits"]
    return compute_life_score({
        "tasks": {"completed": t["completed"], "open": t["pending"] + t["overdue"]} if t else None,
        "habits": {"done": h["done"], "expected": h["expected"]} if h else None,
        "money": money,
        "rituals": {"planned": sum(1 for l in logs if l.plannedAt), "reviewed": sum(1 for l in logs if l.reviewedAt), "days": days_elapsed},
        "overdueNow": t["overdueNow"] if t else None,
    })


def _day_view(d) -> dict:
    return {"date": _key_of(d.date), "planned": bool(d.plannedAt), "reviewed": bool(d.reviewedAt), "plannedAt": d.plannedAt,
            "reviewedAt": d.reviewedAt, "intention": d.intention, "win": d.win, "mood": d.mood, "note": d.note}


def _week_view(w) -> dict:
    return {"weekStart": _key_of(w.weekStart), "reviewed": bool(w.reviewedAt), "reviewedAt": w.reviewedAt, "priorities": w.priorities,
            "wentWell": w.wentWell, "improve": w.improve, "score": w.score}


def _week_of(day: str, week_starts_on: int) -> str:
    return week_start_key(reports._roll_key(day), week_starts_on)


# ----- routes --------------------------------------------------------------------------


@router.get("")
async def overview(user=Depends(plus_user)):
    """Today's log, the streak, this week's score and whether a weekly review is due."""
    ctx = await _context(user)
    day = await db.daylog.find_unique(where={"userId_date": {"userId": user.id, "date": key_to_date(ctx["today"])}})
    recent = await db.daylog.find_many(where={"userId": user.id, "date": {"gte": key_to_date(add_days_key(ctx["today"], -120))},
                                              "OR": [{"plannedAt": {"not": None}}, {"reviewedAt": {"not": None}}]})
    return {
        "today": ctx["today"],
        "day": _day_view(day) if day else None,
        "streak": ritual_streak({_key_of(r.date) for r in recent}, ctx["today"]),
        "week": {"weekStart": ctx["weekStart"], "weekEnd": add_days_key(ctx["weekStart"], 6), "score": await _score_for(user, ctx, ctx["weekStart"])},
        "weeklyReview": await _review_target(user, ctx),
    }


@router.put("/day")
async def update_day(body: DayBody, user=Depends(plus_user)):
    ctx = await _context(user)
    day = key_to_date(reports._roll_key(body.date) if body.date else ctx["today"])
    now = datetime.now(timezone.utc)
    data: dict = {}
    if body.plan is not None:
        data["plannedAt"] = now if body.plan else None
    if body.review is not None:
        data["reviewedAt"] = now if body.review else None
    for field in ("intention", "win", "note"):
        if body.sent(field):
            data[field] = trim_or_null(getattr(body, field))
    if body.sent("mood"):
        data["mood"] = body.mood
    row = await db.daylog.upsert(where={"userId_date": {"userId": user.id, "date": day}},
                                 data={"create": {"userId": user.id, "date": day, **data}, "update": data})
    if body.plan:
        await log_activity(user.id, "ritual.planned", entity_id=row.id, date=_key_of(day))
    if body.review:
        await log_activity(user.id, "ritual.reviewed", entity_id=row.id, date=_key_of(day), mood=row.mood)
    return _day_view(row)


@router.get("/week")
async def week(request: Request, user=Depends(plus_user)):
    """The week for the review screen: numbers, score, the stored review, last week's priorities."""
    errors = unknown_params(request, ("date",))
    date = request.query_params.get("date")
    if date is not None:
        errors += _day_rule(date, "date")
    if errors:
        raise nest_error(400, errors)
    ctx = await _context(user)
    week_start = _week_of(date, ctx["weekStartsOn"]) if date else (await _review_target(user, ctx))["weekStart"]
    report = await reports.weekly(user, week_start)
    score = await _score_for(user, ctx, week_start)
    review = await db.weekreview.find_unique(where={"userId_weekStart": {"userId": user.id, "weekStart": key_to_date(week_start)}})
    previous = await db.weekreview.find_unique(where={"userId_weekStart": {"userId": user.id, "weekStart": key_to_date(add_days_key(week_start, -7))}})
    logs = await db.daylog.find_many(where={"userId": user.id, "date": {"gte": key_to_date(week_start), "lte": key_to_date(add_days_key(week_start, 6))}},
                                     order={"date": "asc"})
    t, h, f, cal, g = report["tasks"], report["habits"], report["finance"], report["calendar"], report["goals"]
    return {
        "weekStart": week_start,
        "weekEnd": add_days_key(week_start, 6),
        "label": report["period"]["label"],
        "isCurrent": report["period"]["isCurrent"],
        "currency": report["currency"],
        "score": score,
        "previousScore": previous.score if previous else None,
        "previousPriorities": previous.priorities if previous else [],
        "review": _week_view(review) if review else None,
        "stats": {
            "tasks": {k: t[k] for k in ("completed", "pending", "overdue", "overdueNow")} if t else None,
            "habits": {k: h[k] for k in ("done", "expected", "completionRate")} if h else None,
            "finance": {k: f[k] for k in ("income", "expenses", "savings")} if f else None,
            "events": cal["count"] if cal else None,
            "goalsUpdated": g["progressUpdates"] if g else None,
        },
        "days": [_day_view(l) for l in logs],
    }


@router.put("/week")
async def update_week(body: WeekBody, user=Depends(plus_user)):
    ctx = await _context(user)
    week_start = _week_of(body.date, ctx["weekStartsOn"]) if body.date else (await _review_target(user, ctx))["weekStart"]
    start = key_to_date(week_start)
    data: dict = {}
    if body.priorities is not None:
        data["priorities"] = [p.strip(_JS_SPACE) for p in body.priorities if p.strip(_JS_SPACE)][:3]
    for field in ("wentWell", "improve"):
        if body.sent(field):
            data[field] = trim_or_null(getattr(body, field))
    if body.done is not None:
        data["reviewedAt"] = datetime.now(timezone.utc) if body.done else None
        data["score"] = (await _score_for(user, ctx, week_start))["total"] if body.done else None
    row = await db.weekreview.upsert(where={"userId_weekStart": {"userId": user.id, "weekStart": start}},
                                     data={"create": {"userId": user.id, "weekStart": start, **data}, "update": data})
    if body.done:
        await log_activity(user.id, "ritual.weekReviewed", entity_id=row.id, weekStart=week_start, score=row.score)
    return _week_view(row)
