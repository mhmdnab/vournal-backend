"""Nudges written by a small model (design 2026-11-18).

Only for Plus users who agreed (nudgeAiConsentAt); not counted against the monthly AI uses. The
model gets names and counts (life_facts, app_facts), the nudges of the last 3 days so it doesn't
repeat itself, and, only with journal permission, the summaries (never the words) of the last 3
days' entries, for the evening check-in. Its reply is checked before it can reach a lock screen:
lengths, no amount of money, no run of 6+ words from a summary. Anything wrong raises
WriterFailed, and the caller sends the rule-based text instead."""

import json
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.db import db
from app.nudges.rules import Nudge
from app.os import ai
from app.os.assistant_consent import journal_allowed
from app.os.common.defaults import SUPPORTED_CURRENCIES
from app.review import IN_JOURNAL

NUDGE_MODEL = "claude-haiku-5-5"
MAX_TITLE, MAX_BODY, MAX_PROMPT = 40, 140, 120
SUMMARY_DAYS, MAX_SUMMARIES = 3, 6
QUOTE_WORDS = 6


class WriterFailed(Exception):
    """The model failed, refused, or wrote something that can't go on a lock screen."""


SYSTEM = """You write one short phone notification for a person who uses vournal, a voice journal and life planner.
It is the {moment} nudge: {purpose}
Rules:
- Calm, specific and kind. No guilt, no pressure, no exclamation marks in a row.
- One idea only. Use the facts given; never invent tasks, people or plans.
- Never include an amount of money, a quote from the journal, or anything about health.
- Never mention Plus, prices, subscriptions, upgrades or the app's features.
- Don't repeat what the recent nudges already said.
- title: at most 40 characters. body: at most 140 characters.
- opens: "life" for plans, tasks, bills, habits and goals; "record" when you invite them to talk or journal.
- prompt: only when opens is "record", one gentle question (at most 120 characters) shown above the record button; otherwise null.
- If there is nothing genuinely useful to say, reply with send: false.
- Write in this language: {language}."""

PURPOSES = {
    "morning": "a plan for the day ahead, from today's tasks, events, bills due soon, habit streaks and birthdays.",
    "midday": "one timely suggestion or follow-up, based on the triggers listed in the facts.",
    "evening": "a gentle check-in that invites them to record how the day went. If their journal summaries mention "
               "something they planned or worried about, you may ask about it, without quoting them.",
}
LANGUAGES = {"en": "English", "ar": "Arabic"}

FORMAT = {"type": "json_schema", "schema": {
    "type": "object", "additionalProperties": False,
    "properties": {
        "send": {"type": "boolean"},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "opens": {"type": "string", "enum": ["life", "record"]},
        "prompt": {"type": ["string", "null"]},
    },
    "required": ["send", "title", "body", "opens", "prompt"],
}}

_SYMBOLS = r"[$€£¥₺﷼]"
_CODES = "|".join(sorted({c for c in SUPPORTED_CURRENCIES} | {"USD", "EUR", "LBP", "GBP"}))
_MONEY = re.compile(rf"\d\s?{_SYMBOLS}|{_SYMBOLS}\s?\d|\d\s?(?:{_CODES})\b|\b(?:{_CODES})\s?\d|\d{{1,3}}(?:,\d{{3}})+")
_WORD = re.compile(r"\w+", re.UNICODE)


def _grams(text: str) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD.findall(text)]
    return {tuple(words[i:i + QUOTE_WORDS]) for i in range(len(words) - QUOTE_WORDS + 1)}


def passes_checks(n: Nudge, summaries: list[str]) -> bool:
    if not n.title.strip() or not n.body.strip():
        return False
    if len(n.title) > MAX_TITLE or len(n.body) > MAX_BODY or (n.prompt is not None and len(n.prompt) > MAX_PROMPT):
        return False
    texts = [n.title, n.body] + ([n.prompt] if n.prompt else [])
    if any(_MONEY.search(t) for t in texts):
        return False
    quoted = set().union(*(_grams(s) for s in summaries)) if summaries else set()
    return not any(_grams(t) & quoted for t in texts)


async def journal_summaries(user, settings, tz: ZoneInfo, today) -> list[str]:
    """The summaries of the last 3 local days' entries, only with journal permission."""
    if not journal_allowed(settings):
        return []
    since = datetime.combine(today - timedelta(days=SUMMARY_DAYS - 1), time(0), tzinfo=tz)
    rows = await db.entry.find_many(where={"userId": user.id, "createdAt": {"gte": since}, "summary": {"not": None}, "AND": [IN_JOURNAL]},
                                    order={"createdAt": "desc"}, take=MAX_SUMMARIES)
    return [r.summary for r in rows if r.summary and r.summary.strip()]


async def write(moment: str, *, app: dict, life: dict, recent: list[str], summaries: list[str], language: str) -> Nudge | None:
    system = SYSTEM.format(moment=moment, purpose=PURPOSES[moment], language=LANGUAGES.get(language, "English"))
    facts = {"today": {**life, "journal": app}, "recentNudges": recent}
    if moment == "evening" and summaries:
        facts["journalSummaries"] = summaries
    try:
        refused, reply = await ai._structured(system, json.dumps(facts, ensure_ascii=False), FORMAT, model=NUDGE_MODEL)
    except ai.Unavailable as exc:
        raise WriterFailed from exc
    if refused or not isinstance(reply, dict):
        raise WriterFailed
    if not reply["send"]:
        return None
    prompt = (reply.get("prompt") or "").strip() or None
    nudge = Nudge(reply["title"].strip(), reply["body"].strip(), reply["opens"], prompt if reply["opens"] == "record" else None)
    if not passes_checks(nudge, summaries):
        raise WriterFailed
    return nudge
