"""Task metadata and contextual questions, shared by both extraction paths.

The model judges each to-do's urgency, place and repeat from how the person
talks (English, Arabic, Lebanese dialect, Arabizi). The code here only guards
against clear errors — evidence that isn't in the transcript, values outside
the allowed set — and falls back to "unknown" (which may ask the user), never to
a guess. Typed !, !! and !!! attached to a task always win.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

CATEGORIES = {"work", "health", "family", "finance", "personal", "learning", "errands"}
RECURRENCES = {"daily", "weekly", "monthly", "yearly"}
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
EVENT = re.compile(
    r"\b(meeting|appointment|lunch|dinner|flight|interview|coffee with)\b|اجتماع|موعد",
    re.I,
)

PROMPT = """

TO-DO DETAILS (read carefully):
Each object in "todos" ALSO has these keys:
- "sourceText": the exact words from the transcript this to-do came from, copied verbatim (keep any !, !! or !!!).
- "kind": "event" for a meeting, appointment, class, flight, dinner or anything happening at a time; otherwise "task".
- "urgency": how urgent the speaker says THIS to-do is — 3 high (must happen first / very soon / serious consequences), 2 medium (important, some flexibility), 1 low (whenever, no rush), 0 if they gave no sign either way. Use null ONLY when they clearly signal importance but you cannot tell the level ("it's important", "soon"). Judge from how they talk: "I really have to", "lezem 2ablo bukra", "ضروري جداً" -> 3; "whenever I get a chance", "mesh mesta3jel", "مش مستعجل" -> 1. Typed !, !! and !!! after a to-do mean 1, 2 and 3. Excitement or emotion about the day ("what a great day!!!") is NOT urgency for any to-do.
- "urgencyEvidence": the exact words (verbatim from the transcript) that show the urgency, or null when urgency is 0 or null.
- "location": a place said for THIS to-do, written exactly as spoken, or null. Never invent one.
- "recurrence": null for a one-time to-do. If the speaker asks for it to repeat: {"freq": "daily"|"weekly"|"monthly"|"yearly", "interval": number (every 2 weeks -> 2), "weekdays": ["MO","TU","WE","TH","FR","SA","SU"] or null, "until": "YYYY-MM-DD" or null}. "every Monday and Thursday" -> {"freq":"weekly","interval":1,"weekdays":["MO","TH"],"until":null}. Use the string "unclear" when they ask for a repeat but you cannot tell how often.
- "category": one of work, health, family, finance, personal, learning, errands.
Never invent a date, a clock time, a place or a repeat. A meeting or appointment with a day but no clock time keeps "time": null; the app asks the user.

Output only the JSON object, nothing else."""


def category_for(text: str) -> str:
    for category, pattern in (
        ("health", r"dentist|doctor|medicine|gym|طبيب|دواء"),
        ("finance", r"invoice|rent|bill|bank|pay|فاتورة|دفع"),
        ("work", r"meeting|client|project|report|اجتماع|عمل"),
        ("family", r"mom|dad|family|sister|brother|عائلة|أمي"),
        ("learning", r"study|course|learn|read|درس|تعلم"),
        ("errands", r"buy|shop|grocer|passport|شراء|جواز"),
    ):
        if re.search(pattern, text, re.I):
            return category
    return "personal"


def _fold(text: str) -> str:
    """Compare text the way a reader would: case-insensitive, without Arabic
    diacritics or tatweel, Unicode-normalized, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        ch
        for ch in text
        if not ("\u064b" <= ch <= "\u065f" or ch in "\u0670\u0640")
    )
    return " ".join(text.split())


def _in_transcript(phrase, transcript: str) -> bool:
    return isinstance(phrase, str) and bool(phrase.strip()) and _fold(phrase) in _fold(transcript)


def _clauses(text: str) -> list[str]:
    # A new sentence starts at a capital or an Arabic letter, so "! !!" stays one clause.
    return [
        s
        for s in re.split(r"[\n;]+|(?<=[.!?؟])\s+(?=[A-Z\u0600-\u06ff])", text)
        if s.strip()
    ]


def _task_clause(item: dict, transcript: str) -> str:
    """The verbatim clause the to-do came from: the model's sourceText when it is
    really in the transcript (and is one clause), else the clause holding its text."""
    source = item.get("sourceText")
    if _in_transcript(source, transcript) and len(_clauses(source)) <= 1:
        return source
    label = _fold(str(item.get("text") or ""))
    return next((c for c in _clauses(transcript) if label and label in _fold(c)), "")


def _urgency(item: dict, transcript: str) -> tuple[int, bool]:
    """(urgency 0-3, unclear). Typed markers in the task's clause win; otherwise
    the model's level stands only when its evidence is really in the transcript."""
    levels = {len(m) for m in re.findall(r"!+", _task_clause(item, transcript))}
    if levels:
        if len(levels) == 1 and 1 <= next(iter(levels)) <= 3:
            return next(iter(levels)), False
        return 0, True  # conflicting or out-of-range markers: ask
    if "urgency" in item and item["urgency"] is None:
        return 0, True  # importance signalled, level unknown: ask
    level = item.get("urgency")
    if type(level) is int and 1 <= level <= 3:
        if _in_transcript(item.get("urgencyEvidence"), transcript):
            return level, False
        return 0, True  # unverifiable: ask instead of trusting it
    return 0, False


def _recurrence(item: dict) -> dict:
    raw = item.get("recurrence")
    out = {
        "recurrence": None,
        "recurrenceInterval": 1,
        "recurrenceWeekdays": [],
        "recurrenceUntil": None,
        "recurrenceUnclear": raw == "unclear",
    }
    if isinstance(raw, str) and raw in RECURRENCES:  # flat legacy form
        raw = {"freq": raw, "interval": item.get("recurrenceInterval", 1)}
    if not isinstance(raw, dict) or raw.get("freq") not in RECURRENCES:
        return out
    interval = raw.get("interval", 1)
    weekdays = raw.get("weekdays") if raw["freq"] == "weekly" else None
    until = raw.get("until")
    try:
        until_at = (
            datetime.strptime(until, "%Y-%m-%d").replace(hour=23, minute=59, tzinfo=timezone.utc)
            if isinstance(until, str)
            else None
        )
    except ValueError:
        until_at = None
    out.update(
        recurrence=raw["freq"],
        recurrenceInterval=interval if type(interval) is int and 1 <= interval <= 365 else 1,
        recurrenceWeekdays=[d for d in WEEKDAYS if isinstance(weekdays, list) and d in weekdays],
        recurrenceUntil=until_at,
    )
    return out


def normalize_metadata(item: dict, transcript: str) -> dict:
    urgency, unclear = _urgency(item, transcript)
    location = item.get("location")
    category = item.get("category")
    kind = item.get("kind")
    return dict(
        kind="event" if kind == "event" or EVENT.search(str(item.get("text") or "")) else "task",
        urgency=urgency,
        urgencyUnclear=unclear,
        category=category if category in CATEGORIES else category_for(str(item.get("text") or "")),
        location=location.strip() if _in_transcript(location, transcript) else None,
        **_recurrence(item),
    )


def questions(task) -> list[dict]:
    """Ask only what the AI couldn't work out: an event's day and time, an
    urgency it couldn't judge, a repeat it couldn't pin down."""
    dismissed = set(getattr(task, "dismissedQuestions", []) or [])
    output = []
    is_event = task.kind == "event"
    if (is_event or task.recurrence) and not task.dueDate:
        output.append({"field": "date", "question": f"What date is “{task.text}” for?"})
    if is_event and not task.startAt:
        output.append({"field": "time", "question": f"What time is “{task.text}”?"})
    if task.urgencyUnclear:
        output.append(
            {"field": "urgency", "question": f"How urgent is “{task.text}”? Choose !, !! or !!!."}
        )
    if getattr(task, "recurrenceUnclear", False) and not task.recurrence:
        output.append(
            {
                "field": "recurrence",
                "question": "How often should this repeat? Choose a supported frequency or leave it as a one-time task.",
            }
        )
    return [q for q in output if q["field"] not in dismissed]


def metadata_out(task) -> dict:
    # calendarRequested/remindersRequested stay in the DB but aren't sent: Apple
    # copies are automatic again (build 4 behaviour), so clients must not see them.
    keys = (
        "kind",
        "urgency",
        "category",
        "location",
        "locationLatitude",
        "locationLongitude",
        "recurrence",
        "recurrenceInterval",
        "recurrenceWeekdays",
        "recurrenceUntil",
    )
    return {**{key: getattr(task, key) for key in keys}, "followUps": questions(task)}


def local_extract(transcript: str, now: datetime | None = None) -> dict:
    """Offline fixture extractor for local UX testing; never calls an AI service.

    Deliberately small grammar: tomorrow/ISO date, HH:MM or 2pm, explicit repeats.
    Real model behavior is wired through PROMPT but requires separate staging QA.
    """
    now = now or datetime.now(timezone.utc)
    todos = []
    for source in re.split(r"[\n;]+|(?<=[.!?])\s+(?=[A-Z])", transcript.strip()):
        if not source.strip():
            continue
        if not re.search(
            r"\b(need|have|must|should|remind|meeting|appointment|buy|call|pay|add|every)\b|بدي|لازم|اجتماع|موعد",
            source,
            re.I,
        ):
            continue
        due = None
        iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", source)
        if iso:
            due = iso.group()
        elif re.search(r"tomorrow|بكرا", source, re.I):
            due = (now + timedelta(days=1)).date().isoformat()
        elif re.search(r"today|اليوم", source, re.I):
            due = now.date().isoformat()
        clock = re.search(
            r"\b(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?\b|\b(\d{1,2})\s*(am|pm)\b",
            source,
            re.I,
        )
        time_value = None
        if clock:
            hour, minute = int(clock[1] or clock[4]), int(clock[2] or 0)
            period = (clock[3] or clock[5] or "").lower()
            if period:
                hour = hour % 12 + (12 if period == "pm" else 0)
            time_value = f"{hour:02d}:{minute:02d}"
            due = due or now.date().isoformat()
        recurrence = next(
            (f for f in RECURRENCES if re.search(rf"\b{f}\b", source, re.I)), None
        )
        if not recurrence:
            recurrence = next(
                (
                    f
                    for word, f in (
                        ("day", "daily"),
                        ("week", "weekly"),
                        ("month", "monthly"),
                        ("year", "yearly"),
                    )
                    if re.search(rf"\bevery (?:\d+ )?{word}s?\b", source, re.I)
                ),
                None,
            )
        interval = re.search(r"\bevery (\d+) ", source, re.I)
        location = re.search(
            r"\bat (?:the )?([A-Z][\w -]+?)(?=\s+(?:tomorrow|today|every|at \d|on \d{4}-|to Apple)|[!.;]|$)",
            source,
        )
        todos.append(
            dict(
                text=source.strip().rstrip(".!?"),
                sourceText=source,
                dueDate=due,
                time=time_value,
                kind="event" if EVENT.search(source) else "task",
                location=location[1] if location else None,
                recurrence=(
                    {"freq": recurrence, "interval": int(interval[1]) if interval else 1}
                    if recurrence
                    else None
                ),
            )
        )
    return {
        "summary": transcript[:200],
        "mood": {"score": 3},
        "todos": todos,
        "people": [],
        "topics": [],
        "places": [],
    }
