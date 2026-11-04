"""/api/ai and the food estimate (port of vournalOS AiService, capture.schema/normalize and
food.schema). Off unless the server has ANTHROPIC_API_KEY *and* the user turned AI on. It
only ever proposes a draft; saving goes through the normal routes once the user confirms.

Privacy, as before: a capture sends the typed sentence plus the *names* of the user's
categories, accounts and shopping lists. People are matched here afterwards and never sent.

The model is claude-sonnet-5-5 (owner decision; AI_MODEL overrides it). Chat, capture and
food estimates share one monthly allowance (AI_MONTHLY_LIMIT), counted from activity-log
rows in the user's own month. Each spend is taken under a per-user lock before the call, so
a burst can't run past it. The prompts and output formats are copied from the NestJS build
(tests/vournal_os/golden/ai_golden.mjs)."""

import json
import logging
import math
import os
import re
from datetime import date
from pathlib import Path

import anthropic
from fastapi import Depends

from app.db import db
from app.os.common import dates
from app.os.common.body import RuleBody, text
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.jsparse import _JS_SPACE, js_slice
from app.os.household import household_id_of, list_access
from app.os.insight_text import js_round
from app.os.profile import ensure_os_profile
from app.rate_limit import rate_limited

log = logging.getLogger("vournal.os")
router = nest_router("/ai")

PROMPTS = json.loads((Path(__file__).parent / "data" / "ai_prompts.json").read_text())
MODEL = os.environ.get("AI_MODEL", "").strip() or "claude-sonnet-5-5"
MONTHLY_LIMIT = int(os.environ.get("AI_MONTHLY_LIMIT", "").strip() or 3000)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
STRUCTURED_BETA = "structured-outputs-2025-12-15"  # the TS SDK's parse() always added it
CAPTURE, FOOD, CHAT = "ai.capture", "ai.food", "ai.chat"
WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

# the SDK reads ANTHROPIC_API_KEY (and ANTHROPIC_BASE_URL) from the environment; tests swap this
_client = anthropic.AsyncAnthropic(max_retries=2, timeout=30) if os.environ.get("ANTHROPIC_API_KEY", "").strip() else None


def client():
    """The shared client (None without ANTHROPIC_API_KEY), for the chatbot too."""
    return _client


# ----- the allowance ---------------------------------------------------------------------


def _month_start(tz: str):
    zone = dates.valid_zone(tz)
    today = dates.local_date_string(dates._now(), zone)
    return dates.local_midnight(f"{today[:7]}-01", zone)


async def _used(db_or_tx, user_id: str, tz: str) -> int:
    return await db_or_tx.activitylog.count(where={"userId": user_id, "action": {"in": [CAPTURE, FOOD, CHAT]}, "createdAt": {"gte": _month_start(tz)}})


async def remaining(user_id: str, tz: str) -> int:
    return max(0, MONTHLY_LIMIT - await _used(db, user_id, tz))


async def spend(user_id: str, tz: str, action: str) -> int | None:
    """Takes one request of this month's allowance before a call; None when none is left.
    Under a per-user lock, so parallel requests can't all read the same count."""
    async with db.tx() as tx:
        await tx.query_raw("SELECT 1 AS ok FROM pg_advisory_xact_lock(hashtext($1))", f"vournal-os-ai:{user_id}")
        used = await _used(tx, user_id, tz)
        if used >= MONTHLY_LIMIT:
            return None
        await tx.activitylog.create(data={"userId": user_id, "action": action})
        return MONTHLY_LIMIT - used - 1


async def status(user) -> dict:
    s = await ensure_os_profile(user)
    return {"configured": _client is not None, "enabled": s.aiEnabled, "dailyLimit": MONTHLY_LIMIT, "remainingToday": await remaining(user.id, s.timezone)}


# ----- normalizing what the model returns -------------------------------------------------

_SPACES = re.compile(f"[{re.escape(_JS_SPACE)}]+")


def _trim(value: str) -> str:
    return value.strip(_JS_SPACE)


def _norm(name: str) -> str:
    return _SPACES.sub(" ", _trim(name).lower())


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value: float):
    """A JavaScript number as JSON writes it: 12, not 12.0."""
    return int(value) if float(value).is_integer() else value


def _round(value: float, places: int):
    """Math.round(value * 10^places) / 10^places."""
    scale = 10 ** places
    return _num(js_round(value * scale) / scale)


def _find(items: list[dict], name: str | None) -> dict | None:
    if not name:
        return None
    n = _norm(name)
    return next((i for i in items if _norm(i["name"]) == n), None)


def _valid_date(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    year, month, day = (int(p) for p in value.split("-"))
    try:  # what new Date(...).toISOString() round-trips (year 0 is a leap year there)
        date(year or 2000, month, day)
    except ValueError:
        return None
    return value


def _valid_time(value: str | None) -> str | None:
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", _trim(value)) if value else None
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2])
    return f"{hour:02d}:{match[2]}" if hour <= 23 and minute <= 59 else None


def match_person(people: list[dict], name: str) -> dict | None:
    """Exact name first, then a unique first-name match ("Ahmad" → "Ahmad Khalil")."""
    n = _norm(name)
    exact = next((p for p in people if _norm(p["name"]) == n), None)
    if exact:
        return exact
    by_first = [p for p in people if _norm(p["name"]).split(" ")[0] == n]
    return by_first[0] if len(by_first) == 1 else None


# zod's enums, which betaZodOutputFormat moved into descriptions on the wire: parse() still
# enforced them, so a reply outside them was "unavailable"
CAPTURE_KINDS = ("task", "expense", "income", "event", "reminder", "note", "shopping", "habit", "debt", "unknown")
CAPTURE_ENUMS = {"kind": CAPTURE_KINDS, "priority": ("LOW", "MEDIUM", "HIGH", "URGENT", None), "debtDirection": ("I_OWE", "OWED_TO_ME", None)}


def normalize_capture(raw: dict, lookups: dict) -> dict | None:
    """The model's capture as a form draft with the user's own ids; None when nothing is usable
    (unknown kind, empty title, or money without an amount)."""
    kind = raw["kind"]
    if kind == "unknown":
        return None
    person_raw = _trim(raw["person"]) if raw.get("person") else ""
    title = js_slice(_trim(raw["title"]) or (person_raw if kind == "debt" else ""), 200)
    money = kind in ("expense", "income", "debt")
    amount = _round(raw["amount"], 2) if money and _finite(raw.get("amount")) and raw["amount"] > 0 else None
    if money and amount is None:
        return None
    debt = kind == "debt"
    person_name = js_slice(person_raw, 120) if debt and person_raw else None
    if debt and (not raw.get("debtDirection") or not person_name):
        return None
    if not money and not title:
        return None

    category = _find(lookups["expenseCategories"] if kind == "expense" else lookups["incomeCategories"], raw.get("category")) if money and not debt else None
    currency = raw.get("currency")
    currency = (currency.upper() if currency and currency.upper() in SUPPORTED_CURRENCIES else lookups["defaultCurrency"]) if money else None
    duration = raw.get("durationMinutes")
    duration = min(js_round(duration), 24 * 60) if kind == "event" and _finite(duration) and duration > 0 else None
    quantity = raw.get("quantity")
    quantity = _round(quantity, 2) if kind == "shopping" and _finite(quantity) and quantity > 0 else None
    note = _trim(raw["note"]) if raw.get("note") else ""
    account = _find(lookups["accounts"], raw.get("account")) if money and not debt else None
    shopping_list = _find(lookups["shoppingLists"], raw.get("list")) if kind == "shopping" else None
    person = match_person(lookups.get("people") or [], person_name) if person_name else None
    return {
        "kind": kind,
        "title": title,
        "amount": amount,
        "currency": currency,
        "date": _valid_date(raw.get("date")),
        "time": _valid_time(raw.get("time")),
        "durationMinutes": duration,
        "categoryId": category["id"] if category else None,
        "categoryName": category["name"] if category else None,
        "accountId": account["id"] if account else None,
        "listId": shopping_list["id"] if shopping_list else None,
        "quantity": quantity,
        "priority": raw.get("priority") if kind == "task" else None,
        "note": js_slice(note, 1000) if note else None,
        "debtDirection": raw.get("debtDirection") if debt else None,
        "personId": person["id"] if person else None,
        "personName": person_name,
    }


def normalize_estimate(raw: dict) -> list[dict]:
    """Drops anything unusable and clamps the numbers to what one item could plausibly be."""

    def grams(v):
        return min(500, _round(v, 1)) if _finite(v) and v >= 0 else None

    usable = [i for i in raw["items"] if i.get("name") and _trim(i["name"]) and _finite(i.get("calories")) and i["calories"] >= 0][:15]
    return [{
        "name": js_slice(_trim(i["name"]), 120),
        "servings": min(50, _round(i["servings"], 2)) if _finite(i.get("servings")) and i["servings"] > 0 else 1,
        "serving": js_slice(_trim(i.get("serving") or ""), 60),
        "calories": min(5000, js_round(i["calories"])),
        "protein": grams(i.get("protein")),
        "carbs": grams(i.get("carbs")),
        "fat": grams(i.get("fat")),
    } for i in usable]


def capture_user_message(sentence: str, ctx: dict) -> str:
    """The per-request context and the text itself."""

    def names(items):
        return ", ".join(items) if items else "(none)"

    return "\n".join([
        "<context>",
        f"today: {ctx['today']} ({ctx['weekday']}), timezone {ctx['timezone']}",
        f"interface language: {ctx['language']}",
        f"default currency: {ctx['defaultCurrency']}",
        f"expense categories: {names(ctx['expenseCategories'])}",
        f"income categories: {names(ctx['incomeCategories'])}",
        f"accounts: {names(ctx['accounts'])}",
        f"shopping lists: {names(ctx['shoppingLists'])}",
        "</context>",
        "",
        "<text>",
        sentence,
        "</text>",
    ])


# ----- calling the model -----------------------------------------------------------------


def _shape_ok(value, schema: dict, defs: dict) -> bool:
    """What zod checked on the reply: the types, required keys and enums of the output format."""
    if "$ref" in schema:
        return _shape_ok(value, defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return any(_shape_ok(value, s, defs) for s in schema[key])
    kind = schema.get("type")
    if isinstance(kind, list):
        return any(_shape_ok(value, {**schema, "type": k}, defs) for k in kind)
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "object":
        props = schema.get("properties", {})
        return isinstance(value, dict) and all(k in value for k in schema.get("required", [])) and all(
            _shape_ok(v, props[k], defs) for k, v in value.items() if k in props)
    if kind == "array":
        return isinstance(value, list) and all(_shape_ok(v, schema.get("items", {}), defs) for v in value)
    checks = {"string": lambda v: isinstance(v, str), "number": _finite, "integer": lambda v: _finite(v) and float(v).is_integer(),
              "boolean": lambda v: isinstance(v, bool), "null": lambda v: v is None}
    return checks.get(kind, lambda v: True)(value)


class Unavailable(Exception):
    """The provider failed, or the reply didn't fit the output format."""


async def _structured(system: str, content: str, output_format: dict, enums: dict | None = None):
    """One structured-output request: (refused, parsed reply or None)."""
    try:
        response = await _client.beta.messages.create(
            model=MODEL,
            max_tokens=2048,
            betas=[FALLBACK_BETA, STRUCTURED_BETA],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": output_format},
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        log.error("AI request rejected: check ANTHROPIC_API_KEY")
        raise Unavailable from exc
    except anthropic.RateLimitError as exc:
        log.warning("AI provider rate limit reached")
        raise Unavailable from exc
    except anthropic.APIStatusError as exc:
        log.warning("AI request failed (%s): %s", exc.status_code, exc.message)
        raise Unavailable from exc
    except Exception as exc:  # noqa: BLE001 — a network error, a timeout
        log.warning("AI request failed: %s", exc)
        raise Unavailable from exc
    # as the SDK's parse() did: every text block is parsed (one that doesn't fit fails the
    # request), the first one is the answer, and only then is a refusal looked at
    parsed = None
    for block in response.content:
        if getattr(block, "type", None) != "text":
            continue
        try:
            value = json.loads(block.text)
        except ValueError as exc:
            log.warning("AI request failed: the reply wasn't the structured output")
            raise Unavailable from exc
        if not _shape_ok(value, output_format["schema"], output_format["schema"].get("$defs", {})) or any(
                value.get(k) not in allowed for k, allowed in (enums or {}).items()):
            log.warning("AI request failed: the reply didn't match the output format")
            raise Unavailable
        parsed = value if parsed is None else parsed
    return response.stop_reason == "refusal", parsed


async def _lookups(user_id: str, default_currency: str) -> dict:
    household_id = await household_id_of(user_id)
    categories = await db.category.find_many(where={"userId": user_id, "scope": {"in": ["EXPENSE", "INCOME"]}}, order={"sortOrder": "asc"}, take=80)
    accounts = await db.account.find_many(where={"userId": user_id, "isArchived": False}, take=30)
    lists = await db.shoppinglist.find_many(where={**list_access(user_id, household_id), "isArchived": False}, take=30)
    people = await db.person.find_many(where={"userId": user_id}, take=1000)  # matched after the call, never sent

    def pick(rows):
        return [{"id": r.id, "name": r.name} for r in rows]

    return {
        "expenseCategories": pick(c for c in categories if c.scope == "EXPENSE"),
        "incomeCategories": pick(c for c in categories if c.scope == "INCOME"),
        "accounts": pick(accounts),
        "shoppingLists": pick(lists),
        "people": pick(people),
        "defaultCurrency": default_currency,
    }


async def capture(user, sentence: str) -> dict:
    """One sentence → a draft to confirm. Never raises for expected failures: the status says."""
    s = await ensure_os_profile(user)
    left = await remaining(user.id, s.timezone)

    def done(state, draft=None):
        return {"status": state, "draft": draft, "remainingToday": left}

    if _client is None:
        return done("not_configured")
    if not s.aiEnabled:
        return done("disabled")
    if left <= 0:
        return done("limit")
    lookups = await _lookups(user.id, s.defaultCurrency)
    tz = dates.valid_zone(s.timezone)
    today = dates.local_date_string(dates._now(), tz)
    content = capture_user_message(_trim(sentence), {
        "today": today,
        "weekday": WEEKDAYS[(date(*map(int, today.split("-"))).weekday() + 1) % 7],
        "timezone": s.timezone,
        "language": to_lang(s.language),
        "defaultCurrency": s.defaultCurrency,
        "expenseCategories": [c["name"] for c in lookups["expenseCategories"]],
        "incomeCategories": [c["name"] for c in lookups["incomeCategories"]],
        "accounts": [a["name"] for a in lookups["accounts"]],
        "shoppingLists": [ls["name"] for ls in lookups["shoppingLists"]],
    })
    spent = await spend(user.id, s.timezone, CAPTURE)
    if spent is None:
        left = 0
        return done("limit")
    left = spent
    try:
        refused, raw = await _structured(PROMPTS["captureSystem"], content, PROMPTS["captureFormat"], CAPTURE_ENUMS)
    except Unavailable:
        return done("unavailable")
    draft = normalize_capture(raw, lookups) if raw and not refused else None
    return done("ok", draft) if draft else done("not_understood")


async def estimate_food(user, sentence: str) -> dict:
    """Calories and macros for a described meal. Saves nothing: the client shows them to confirm."""
    s = await ensure_os_profile(user)
    left = await remaining(user.id, s.timezone)

    def done(state, items=None):
        return {"status": state, "items": items or [], "remainingToday": left}

    if _client is None:
        return done("not_configured")
    if not s.aiEnabled:
        return done("disabled")
    if left <= 0:
        return done("limit")
    spent = await spend(user.id, s.timezone, FOOD)
    if spent is None:
        left = 0
        return done("limit")
    left = spent
    content = f"<language>{to_lang(s.language)}</language>\n<text>\n{_trim(sentence)}\n</text>"
    try:
        refused, raw = await _structured(PROMPTS["foodSystem"], content, PROMPTS["foodFormat"])
    except Unavailable as exc:
        log.warning("AI food estimate failed: %s", exc.__cause__ or "bad reply")
        return done("unavailable")
    if refused or not raw:
        return done("not_understood")
    items = normalize_estimate(raw)
    return done("ok", items) if items else done("not_understood")


# ----- routes ----------------------------------------------------------------------------


def _min_max(v, f):
    """@IsString() @MinLength(2) @MaxLength(500): every failing rule, the last decorator first."""
    if isinstance(v, str):
        return ([f"{f} must be shorter than or equal to 500 characters"] if len(v) > 500 else []) + (
            [f"{f} must be longer than or equal to 2 characters"] if len(v) < 2 else [])
    return [f"{f} must be shorter than or equal to 500 characters", f"{f} must be longer than or equal to 2 characters", f"{f} must be a string"]


class CaptureBody(RuleBody):
    RULES = (("text", False, False, _min_max),)
    text: object = None


class EstimateBody(RuleBody):
    RULES = (("text", False, False, lambda v, f: text(500, 2)(v, f)),)
    text: object = None


def throttle(name: str, user_id: str) -> None:
    """20 a minute, as NestJS's @Throttle on every AI route."""
    if rate_limited(name, user_id, 20, 60):
        raise nest_error(429, "ThrottlerException: Too Many Requests")


@router.get("/status")
async def get_status(user=Depends(plus_user)):
    return await status(user)


@router.post("/capture")
async def post_capture(body: CaptureBody, user=Depends(plus_user)):
    """One sentence → a draft to confirm. Saves nothing."""
    throttle("ai-capture", user.id)
    return await capture(user, body.text)
