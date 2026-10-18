"""Task metadata and contextual questions, shared by both extraction paths.

Explicit punctuation takes precedence. Source spans must be verbatim so unrelated
excitement elsewhere in a journal cannot make every task urgent.
"""

import re
from datetime import datetime, timedelta, timezone

CATEGORIES = {"work", "health", "family", "finance", "personal", "learning", "errands"}
RECURRENCES = {"daily", "weekly", "monthly", "yearly"}
EVENT = re.compile(
    r"\b(meeting|appointment|lunch|dinner|flight|interview|coffee with)\b|اجتماع|موعد",
    re.I,
)

PROMPT = """
For each todo, additionally return:
sourceText: the EXACT verbatim sentence/clause in the transcript for this task,
kind: "event" for a meeting/appointment or "task" otherwise,
category: work|health|family|finance|personal|learning|errands,
location: a location explicitly associated with THIS task, otherwise null,
recurrence: daily|weekly|monthly|yearly ONLY if the user explicitly requests it,
recurrenceInterval: a positive integer, default 1,
calendarRequested and remindersRequested: booleans, true ONLY when the user explicitly
asks to add THIS task to Apple Calendar or Apple Reminders respectively.
urgencyUnclear: true when urgency is vague ("urgent", "ASAP") or contradictory.
Preserve !, !! and !!! in sourceText. These are low, medium and high priority.
Do not invent missing dates, clock times, places or recurrence patterns. Meetings
without times must retain time=null; the app will ask a follow-up. If the user
asks for an unsupported or unclear recurrence, leave recurrence=null; the app
will ask them to choose. Ordinary excitement is not a task priority marker.
"""


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


def normalize_metadata(item: dict, transcript: str) -> dict:
    source = item.get("sourceText")
    source = source.strip() if isinstance(source, str) else ""

    # Fallback is conservative: associate only the exact task label within one
    # sentence. Never apply a whole-entry priority to every extracted task.
    def clauses(text):
        return [
            s
            for s in re.split(r"[\n;]+|(?<=[.!?؟])\s+(?=[A-Z\u0600-\u06ff])", text)
            if s.strip()
        ]

    if not source or source not in transcript or len(clauses(source)) > 1:
        source = next(
            (s for s in clauses(transcript) if item["text"].casefold() in s.casefold()),
            "",
        )
    markers = re.findall(r"!+", source)
    levels = {len(m) for m in markers}
    unclear = len(levels) > 1 or any(n > 3 for n in levels)
    urgency = next(iter(levels)) if len(levels) == 1 and not unclear else 0
    unclear = unclear or (
        not markers
        and bool(
            re.search(r"\b(urgent|asap|soon|important)\b|ضروري|مستعجل", source, re.I)
        )
    )
    kind = (
        "event" if item.get("kind") == "event" or EVENT.search(item["text"]) else "task"
    )
    category = item.get("category")
    location = item.get("location")
    # A location must be explicitly present in the verified task span.
    location = (
        location.strip()
        if isinstance(location, str)
        and location.strip()
        and location.strip().casefold() in source.casefold()
        else None
    )
    recurrence = item.get("recurrence")
    repeat_mentioned = bool(
        re.search(
            r"\b(every|daily|weekly|monthly|yearly|repeat)\b|كل |يومي|أسبوعي|شهري|سنوي",
            source,
            re.I,
        )
    )
    recurrence = recurrence if recurrence in RECURRENCES and repeat_mentioned else None
    interval = item.get("recurrenceInterval", 1)
    interval = interval if type(interval) is int and 1 <= interval <= 365 else 1
    # No automatic external writes based solely on generative booleans.
    negated = bool(
        re.search(
            r"\b(don't|do not|never|don't ever|without)\b|لا تضف|بدون", source, re.I
        )
    )
    calendar_requested = not negated and bool(
        re.search(
            r"\b(add|put|sync|save|schedule)\b.*\b(apple )?calendar\b|أضف.*التقويم",
            source,
            re.I,
        )
    )
    reminders_requested = not negated and bool(
        re.search(
            r"\b(add|put|sync|save)\b.*\b(apple )?reminders\b|أضف.*التذكيرات",
            source,
            re.I,
        )
    )
    return dict(
        kind=kind,
        urgency=urgency,
        urgencyUnclear=unclear,
        category=category if category in CATEGORIES else category_for(item["text"]),
        location=location,
        recurrence=recurrence,
        recurrenceInterval=interval,
        calendarRequested=calendar_requested,
        remindersRequested=reminders_requested,
        recurrenceUnclear=repeat_mentioned and not bool(recurrence),
    )


def questions(task) -> list[dict]:
    dismissed = set(getattr(task, "dismissedQuestions", []) or [])
    output = []
    if (task.kind == "event" or task.recurrence) and not task.dueDate:
        output.append({"field": "date", "question": f"What date is “{task.text}” for?"})
    if task.kind == "event" and not task.startAt:
        output.append({"field": "time", "question": f"What time is “{task.text}”?"})
    if task.urgencyUnclear:
        output.append(
            {
                "field": "urgency",
                "question": f"How urgent is “{task.text}”? Choose !, !! or !!!.",
            }
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
    from app.enhancements import enabled

    if not enabled():
        return {}
    keys = (
        "kind",
        "urgency",
        "category",
        "location",
        "recurrence",
        "recurrenceInterval",
        "recurrenceUntil",
        "calendarRequested",
        "remindersRequested",
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
                recurrence=recurrence,
                recurrenceInterval=int(interval[1]) if interval else 1,
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
