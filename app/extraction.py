"""Extraction engine — the brain of the voice journal.

Turns a raw spoken transcript into normalized structured data (summary, mood,
todos, topics, people) via the Anthropic API. Parsing is defensive: the model
is asked for pure JSON, but we strip code fences, fall back to the outermost
{...}, and normalize every field so a malformed response can never crash the
caller.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from anthropic import AsyncAnthropic

# Fast + cheap default for high-volume extraction. Override with EXTRACTION_MODEL
# (e.g. claude-sonnet-5) for higher quality.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024

# Caps applied during normalization.
MAX_EMOTIONS = 4
MAX_TOPICS = 6
MAX_PLACES = 6

# The core of the product — do not rewrite. Passed verbatim as the system prompt.
SYSTEM_PROMPT = """You are the extraction engine for a voice-journaling app. The user speaks freely into their phone about their day, thoughts, and plans. You receive the raw transcript and turn it into clean structured data.

The transcript is spoken, not written. Expect rambling, filler words, self-corrections, run-on sentences, and false starts. It may be in English, Arabic, Lebanese Arabic written in Latin letters and numbers (Arabizi, e.g. "3am b7es 7ali ta3ban"), or a mix of these in one entry. Understand all of them.

Return ONLY a single JSON object, no markdown, no code fences, no commentary before or after. The object MUST have exactly these keys:

{
  "summary": string,          // one neutral sentence capturing the entry
  "mood": {
    "score": integer,         // 1=very negative .. 5=very positive; 3 if unclear
    "label": string,          // short human phrase, e.g. "anxious but hopeful"
    "emotions": string[]      // 0-4 lowercase emotion words actually expressed
  },
  "todos": [
    {
      "text": string,         // a task the speaker intends, OR a meeting/appointment/plan they have
      "dueDate": string|null, // "YYYY-MM-DD" — the DAY, if a day is stated or implied, else null
      "time": string|null     // "HH:MM" 24-hour — ONLY if a specific clock time is spoken, else null
    }
  ],
  "topics": string[],         // 1-6 recurring themes, see rules
  "people": string[],         // names/handles of people mentioned, [] if none
  "places": string[]          // specific named locations mentioned, [] if none
}

Rules:
- summary: write it in the SAME language as the entry, in the FIRST PERSON — the speaker's own voice, as if they wrote it in their own journal (e.g. "I have a meeting tomorrow", "عندي اجتماع بكرا"). Do NOT describe the speaker in the third person. This also keeps gendered languages like Arabic neutral: use first-person forms ("عندي", "بدي", "لازم", "جواز سفري"), NEVER third-person ("عندها"/"عندو", "بدها"/"بدو", "سفرها"/"سفرو") — never assume or infer the speaker's gender. Keep it to one sentence. Describe, don't judge.
- mood.score: judge the OVERALL emotional tone of the whole entry, not the last sentence. If genuinely neutral or unreadable, use 3.
- mood.label and mood.emotions: write these in ENGLISH so they stay consistent for trend charts. emotions are single lowercase words (e.g. "tired", "grateful", "frustrated"). Only include emotions the speaker actually expressed; never pad the list.
- todos: extract two kinds of forward-looking items the speaker actually voiced: (1) TASKS they intend to do — "I need to", "I have to", "I should", "remind me to", "don't forget"; and (2) SCHEDULED COMMITMENTS or PLANS they have or intend — a meeting, an appointment, an event, or seeing someone ("I have a meeting with…", "I have a doctor's appointment", "we're meeting…", "I'm seeing…", "I might go see…", "dinner with…", "flight on…"). Include a plan even when softly stated ("might", "thinking of", "planning to"), but NOT pure hypotheticals ("if I get time maybe…"), and NEVER a past event. Do NOT invent anything. Rewrite each into a short clear label — an imperative, a verbal noun, or for a meeting/appointment a short "Meeting with X" / "Coffee with Y" form — NOT a full sentence about the speaker. English: "Call the landlord about the leak", "Renew passport", "Meeting with Ahmad", "Coffee with Bilal". Arabic: a neutral form like "الاتصال بصاحب البيت", "تجديد جواز السفر", "اجتماع مع أحمد" — NEVER a gendered sentence like "بدها تجدد جواز سفرها". If none, return [].
- dueDate: the DAY the task is for. Set it only when a day is stated or clearly implied. Resolve relative day expressions ("tomorrow", "next Monday", "in two days", "end of the month", "tonight") against the CURRENT DATE AND TIME provided below. Otherwise null.
- time: the specific clock time in "HH:MM" 24-hour form. Set it ONLY when the speaker actually states a time — e.g. "at 2pm" -> "14:00", "at 10" -> "10:00", "at 8 tonight" -> "20:00", "half past 9" -> "09:30", "in an hour" / "in 30 minutes" (compute it from the current time). If you set a time, you MUST also set dueDate to the day that time falls on. NEVER invent a time: a task with only a day ("call the dentist tomorrow") or only a vague reference ("later", "soon", "sometime", "this week") has time = null. When am/pm is unstated, use common sense — an appointment, meeting, or errand is normally during the day.
- Dialect / Arabizi times: understand spoken Lebanese/Arabic times too. "el sa3a 3" = at 3 o'clock; "bukra el sa3a 3" = tomorrow at 3:00; "el se3a 8 el masa" / "8 bil lél" = 20:00; "ba3d sa3a" = in an hour; "ba3d nos sa3a" = in half an hour; "el lela" / "el layle" = tonight; "bukra" = tomorrow; "ba3d bukra" = the day after tomorrow.
- topics: normalize to general, reusable ENGLISH nouns so they aggregate across days. Prefer "work", "sleep", "health", "family", "money", "relationship" over hyper-specific phrases. This is what powers "you talked about sleep 5 times this week", so favor stable labels.
- people: use the name as spoken. If the speaker refers to someone only by role ("my boss", "my sister") and no name, you may include that role. Skip vague references. [] if none.
- places: SPECIFIC named locations the speaker was at, went to, or is going to — a city, neighborhood, venue, or a named personal place ("Beirut", "Hamra", "the gym", "the office", "Mom's"). Use the name as spoken (keep the original language). Do NOT include vague or generic locations ("home", "outside", "there", "somewhere"), and do NOT invent a place that was not mentioned. [] if none.
- If the transcript is empty or has no real content, return the object with an empty summary, mood.score 3, and empty arrays.

Output the JSON object and nothing else."""


# Appended to the system prompt (Step 17). Turns the known-names list in the user
# message into entity continuity — the model reuses an existing record instead of
# minting a near-duplicate, INCLUDING across scripts (بلال -> Bilal), while biasing
# conservative so it never fuses two different people. Static, so it stays cacheable.
KNOWN_NAMES_ADDENDUM = """

ENTITY CONTINUITY (people & topics):
The user's message may include a KNOWN PEOPLE list and a KNOWN TOPICS list — names and themes from their past entries. When this entry clearly refers to one of them, reuse the EXACT spelling from the list in your "people"/"topics" output, so it links to the existing record instead of creating a duplicate. This INCLUDES the same name written in a different script or spelling — Arabic "بلال" and Latin "Bilal" are the same person, so output "Bilal" if it is on the list.
Reuse ONLY when it is clearly the same person or theme. If you are unsure whether it is the same one, do NOT reuse — use the name as spoken and let a new record be created. Over-merging two different people corrupts a personal record and is far worse than a duplicate. Never collapse two clearly-different people onto one name.

Output only the JSON object, nothing else."""

# The full system prompt for plain extraction (text entries): core + continuity.
EXTRACT_SYSTEM_PROMPT = SYSTEM_PROMPT + KNOWN_NAMES_ADDENDUM


def _format_now(now: datetime) -> str:
    """A human, model-friendly reference for resolving relative dates AND times,
    e.g. "2026-07-20 14:30 (Monday, Asia/Beirut, UTC+03:00)". The weekday helps
    with "next Monday"; the zone tells the model this is the speaker's LOCAL wall
    clock, so "tonight" and "in an hour" resolve against their clock and the
    returned time is a local clock time. Falls back to a bare UTC label when
    `now` is naive."""
    zone = now.strftime("%Z") or "UTC"
    offset = now.strftime("%z")
    offset = f", UTC{offset[:3]}:{offset[3:]}" if offset else ""
    return now.strftime(f"%Y-%m-%d %H:%M (%A, {zone}{offset})")


def _known_block(known: Optional[dict[str, list[str]]]) -> str:
    """Render the user's known people/topics for the prompt, or "" if none. The
    display names are echoed verbatim so the model can reuse an exact spelling."""
    if not known:
        return ""
    parts: list[str] = []
    people = [p for p in (known.get("people") or []) if p]
    topics = [t for t in (known.get("topics") or []) if t]
    places = [pl for pl in (known.get("places") or []) if pl]
    if people:
        parts.append(
            "KNOWN PEOPLE (reuse the exact spelling when this entry refers to the same "
            "person, across scripts too):\n" + ", ".join(people)
        )
    if topics:
        parts.append(
            "KNOWN TOPICS (reuse the exact label when this entry is about the same theme):\n"
            + ", ".join(topics)
        )
    if places:
        parts.append(
            "KNOWN PLACES (reuse the exact spelling when this entry refers to the same "
            "location, across scripts too):\n" + ", ".join(places)
        )
    return "\n\n".join(parts)


def _user_message(transcript: str, now_str: str, known_block: str = "") -> str:
    kb = f"{known_block}\n\n" if known_block else ""
    return f'CURRENT DATE AND TIME: {now_str}\n\n{kb}TRANSCRIPT:\n"""\n{transcript}\n"""'


def _strip_and_parse(text: str) -> dict[str, Any]:
    """Best-effort parse of the model's text into a dict.

    Strips markdown code fences, then tries json.loads; on failure, falls back
    to the outermost {...} span. Returns {} if nothing parses.
    """
    text = (text or "").strip()

    # Strip a leading ```json / ``` fence and a trailing ``` fence.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass

    # Fallback: grab the outermost brace span and try again.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass

    return {}


def _clean_str_list(value: Any, *, lower: bool = False, cap: Optional[int] = None) -> list[str]:
    """Coerce an arbitrary value into a clean list of non-empty strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            item = str(item) if item is not None else ""
        item = item.strip()
        if lower:
            item = item.lower()
        if not item:
            continue
        key = item.lower()
        if key in seen:  # de-duplicate case-insensitively
            continue
        seen.add(key)
        out.append(item)
        if cap is not None and len(out) >= cap:
            break
    return out


def _clean_time(value: Any) -> Optional[str]:
    """Validate a spoken time into "HH:MM" (24h), or None.

    Accepts "9:00" / "09:00" / "9:5"-ish and pads/validates; anything out of
    range or malformed becomes None so a bad value can never form a bad startAt.
    """
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{1,2}):(\d{1,2})$", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a parsed dict into the guaranteed output shape.

    Guarantees: summary is a string; mood.score is an int in 1..5 (default 3);
    all list fields are clean string lists; empty todos are dropped; emotions
    capped at 4 and topics at 6.
    """
    # summary
    summary = raw.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    # mood
    mood_raw = raw.get("mood")
    if not isinstance(mood_raw, dict):
        mood_raw = {}

    score = mood_raw.get("score", 3)
    try:
        score = int(score)
    except (ValueError, TypeError):
        score = 3
    score = max(1, min(5, score))

    label = mood_raw.get("label")
    label = label.strip() if isinstance(label, str) else ""

    emotions = _clean_str_list(mood_raw.get("emotions"), lower=True, cap=MAX_EMOTIONS)

    # todos
    todos: list[dict[str, Any]] = []
    todos_raw = raw.get("todos")
    if isinstance(todos_raw, list):
        for item in todos_raw:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            text = text.strip() if isinstance(text, str) else ""
            if not text:  # drop empty todos
                continue
            due = item.get("dueDate")
            due = due.strip() if isinstance(due, str) and due.strip() else None
            todos.append({"text": text, "dueDate": due, "time": _clean_time(item.get("time"))})

    # topics / people / places
    topics = _clean_str_list(raw.get("topics"), cap=MAX_TOPICS)
    people = _clean_str_list(raw.get("people"))
    places = _clean_str_list(raw.get("places"), cap=MAX_PLACES)

    return {
        "summary": summary,
        "mood": {"score": score, "label": label, "emotions": emotions},
        "todos": todos,
        "topics": topics,
        "people": people,
        "places": places,
    }


def _empty_result() -> dict[str, Any]:
    """The normalized shape for an empty/blank transcript (no API call needed)."""
    return _normalize({})


def usage_of(response) -> dict[str, int]:
    """Token usage {inTokens, outTokens} from an Anthropic response (Step 11 —
    recorded per entry for unit-economics)."""
    u = getattr(response, "usage", None)
    return {
        "inTokens": int(getattr(u, "input_tokens", 0) or 0),
        "outTokens": int(getattr(u, "output_tokens", 0) or 0),
    }


async def extract(
    transcript: str,
    now: Optional[datetime] = None,
    known: Optional[dict[str, list[str]]] = None,
) -> tuple[dict[str, Any], Optional[dict[str, int]]]:
    """Extract structured data from a transcript.

    Args:
        transcript: the raw spoken transcript (plain text for now).
        now: the current datetime used to resolve BOTH relative dates and times
            ("in an hour", "tonight at 8"). Defaults to now (UTC).
        known: the user's existing {"people": [...], "topics": [...]} display names
            (Step 17, Layer 2). Fed to the model so it reuses an existing record for
            the same person/theme — including across scripts — instead of a near-dup.

    Returns:
        (result, usage) — result is a normalized dict with keys: summary,
        mood{score,label,emotions}, todos[{text,dueDate,time}], topics[], people[];
        usage is {inTokens, outTokens} or None when no API call was made. Never
        raises on a malformed model response — worst case it returns the
        empty-result shape.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # No real content: skip the API call entirely.
    if not transcript or not transcript.strip():
        return _empty_result(), None

    client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment
    model = os.environ.get("EXTRACTION_MODEL") or DEFAULT_MODEL

    response = await client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=EXTRACT_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _user_message(transcript, _format_now(now), _known_block(known)),
            }
        ],
    )

    # Concatenate any text blocks in the response.
    text = "".join(block.text for block in response.content if block.type == "text")

    return _normalize(_strip_and_parse(text)), usage_of(response)
