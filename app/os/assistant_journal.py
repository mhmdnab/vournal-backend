"""The assistant's two journal tools (design 2026-11-17, Ask vournal): read-only, the user's own
saved journal entries only, and offered to the model only after the user allowed it
(assistant_consent.journal_allowed). Hidden entries — waiting for review, turned into a question,
or kept out of the journal — never come back."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.db import db
from app.review import IN_JOURNAL

DEFAULT_LIMIT, MAX_LIMIT, EXCERPT = 10, 20, 600
JOURNAL_TOOLS = frozenset({"search_journal", "get_journal_entries"})


def limit_of(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1:
        return DEFAULT_LIMIT
    return min(MAX_LIMIT, int(v))


def day_start(day: str, tz: str) -> datetime:
    """The instant `day` (YYYY-MM-DD) begins in the user's time zone."""
    return datetime.combine(datetime.fromisoformat(day).date(), time.min, ZoneInfo(tz))


def created_between(start: str | None, end: str | None, tz: str) -> dict:
    where = {}
    if start:
        where["gte"] = day_start(start, tz)
    if end:
        where["lt"] = day_start(end, tz) + timedelta(days=1)
    return {"createdAt": where} if where else {}


def matching(query: str) -> dict:
    """Entries whose words, summary, people, topics or places contain `query` (any case)."""
    like = {"contains": query, "mode": "insensitive"}
    return {"OR": [
        {"rawTranscript": like}, {"summary": like},
        {"people": {"some": {"person": {"name": like}}}},
        {"topics": {"some": {"topic": {"name": like}}}},
        {"places": {"some": {"place": {"name": like}}}},
    ]}


async def find(user_id: str, where: dict, limit: int) -> list:
    # AND, not a merged dict: IN_JOURNAL and a search both use an "OR" key, and one would replace the other
    return await db.entry.find_many(where={"userId": user_id, "AND": [IN_JOURNAL, where]}, order={"createdAt": "desc"}, take=limit)


def view(entry, local_day) -> dict:
    return {"id": entry.id, "date": local_day(entry.createdAt), "mood": entry.moodScore,
            "summary": entry.summary, "excerpt": (entry.rawTranscript or "")[:EXCERPT]}
