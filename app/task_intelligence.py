"""Task metadata and contextual questions, shared by both extraction paths.

The model judges each to-do's urgency, place and repeat from how the person
talks (English, Arabic, Lebanese dialect, Arabizi). The code here only guards
against clear errors — evidence that isn't in the transcript, values outside
the allowed set — and falls back to "unknown" (which may ask the user), never to
a guess. Typed !, !! and !!! attached to a task always win.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

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
- "kind": "event" for a meeting, appointment, class, flight, dinner or anything happening at a time with other people or at a venue; otherwise "task". A phone call, an errand or a chore is a "task".
- "urgency": how urgent the speaker says THIS to-do is — 3 high ("it's urgent", "very necessary", "ضروري كتير", "darure ktir", "عاجل جداً"), 2 medium, 1 low ("whenever I get a chance", "no rush", "mesh mesta3jel", "مش مستعجل", "لا داعي للعجلة"), 0 if they gave no sign. Plain obligation words — "need to", "have to", "must", "lezem", "لازم", "يجب" — are NOT urgency on their own: they just make it a to-do (0). Use the string "unclear" when they signal importance but not the level ("it's important", "soon", "mhem", "مهم", "قريباً"). Typed !, !! and !!! after a to-do mean 1, 2 and 3. Excitement or emotion about the day ("what a great day!!!", "شو هالنهار الحلو!!!") is NOT urgency for any to-do.
- "urgencyEvidence": the exact words (verbatim from THIS to-do's part of the transcript) that show the urgency, or null when urgency is 0 or "unclear".
- "dueDateEvidence": the exact words (verbatim from the transcript) that name the day ("tomorrow", "on Friday", "bukra", "نهار الجمعة"), or null when "dueDate" is null.
- "location": a place said for THIS to-do, written exactly as spoken, or null. Never invent one.
- "recurrence": null for a one-time to-do. If the speaker asks for it to repeat: {"freq": "daily"|"weekly"|"monthly"|"yearly", "interval": number (every 2 weeks -> 2), "weekdays": ["MO","TU","WE","TH","FR","SA","SU"] or null, "until": "YYYY-MM-DD" or null}. "every Monday and Thursday" -> {"freq":"weekly","interval":1,"weekdays":["MO","TH"],"until":null}. Use the string "unclear" when they want it to repeat but don't say how often ("every so often", "from time to time", "every now and then", "kel fatra", "كل فترة", "من وقت لآخر").
- "category": one of work, health, family, finance, personal, learning, errands.
Never invent a date, a clock time, a place or a repeat. Urgency is not a date: "Call the bank !!!" with no day keeps "dueDate": null. A meeting or appointment with a day but no clock time keeps "time": null; the app asks the user.

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


_FOLD = str.maketrans(
    {
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",  # alef forms
        "ة": "ه",  # ta marbuta
        "ى": "ي",  # alef maqsura
        "’": "'", "‘": "'", "`": "'", "“": '"', "”": '"',  # iOS smart punctuation
        **{chr(0x0660 + d): str(d) for d in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + d): str(d) for d in range(10)},  # Persian digits
    }
)


def _fold(text: str) -> str:
    """Compare text the way a reader would: case-insensitive; Arabic without
    diacritics/tatweel and with alef, ta marbuta and alef maqsura unified; smart
    quotes and Arabic digits made plain; punctuation ignored; whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text).casefold().translate(_FOLD)
    text = "".join(
        " " if unicodedata.category(ch).startswith("P") and ch != "'" else ch
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


def _sentence_of(clause: str, transcript: str) -> str:
    """The transcript sentence holding the clause, so urgency said just before
    the task ("This can't wait — I have to…") still counts. Sentences end at any
    . ! ? ؟ (lowercase Arabizi speech too); the clause itself when it spans more."""
    target = _fold(clause)
    return next(
        (s for s in re.split(r"[\n;]+|(?<=[.!?؟])\s+", transcript) if target and target in _fold(s)),
        clause,
    )


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
    clause = _task_clause(item, transcript)
    levels = {len(m) for m in re.findall(r"!+", clause)}
    if levels:
        if len(levels) == 1 and 1 <= next(iter(levels)) <= 3:
            return next(iter(levels)), False
        return 0, True  # conflicting or out-of-range markers: ask
    level = item.get("urgency")
    if level == "unclear":
        return 0, True  # importance signalled, level unknown: ask
    if type(level) is int and 1 <= level <= 3:
        evidence = item.get("urgencyEvidence")
        if isinstance(evidence, str) and not evidence.strip(" !"):
            return 0, False  # bare "!!!" outside the task is excitement, not urgency
        # The words must belong to THIS task's sentence when its clause is known.
        if _in_transcript(evidence, _sentence_of(clause, transcript) if clause else transcript):
            return level, False
        return 0, True  # unverifiable or from another sentence: ask
    return 0, False


def _recurrence(item: dict, tz=None) -> dict:
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
    if not isinstance(raw, dict) or not isinstance(raw.get("freq"), str) or raw["freq"] not in RECURRENCES:
        return out
    interval = raw.get("interval", 1)
    weekdays = raw.get("weekdays") if raw["freq"] == "weekly" else None
    until = raw.get("until")
    try:
        # The end of that day where the user lives, stored in UTC.
        until_at = (
            datetime.strptime(until, "%Y-%m-%d")
            .replace(hour=23, minute=59, tzinfo=tz or timezone.utc)
            .astimezone(timezone.utc)
            if isinstance(until, str)
            else None
        )
    except ValueError:
        until_at = None
    due = item.get("dueDate")
    if until_at and isinstance(due, str) and until < due:
        until_at = None  # an end before the first occurrence is a mistake
    out.update(
        recurrence=raw["freq"],
        recurrenceInterval=interval if type(interval) is int and 1 <= interval <= 365 else 1,
        recurrenceWeekdays=[d for d in WEEKDAYS if isinstance(weekdays, list) and d in weekdays],
        recurrenceUntil=until_at,
    )
    return out


def normalize_metadata(item: dict, transcript: str, tz=None) -> dict:
    urgency, unclear = _urgency(item, transcript)
    location = item.get("location")
    category = item.get("category")
    kind = item.get("kind")
    return dict(
        # The model's kind wins ("book the dentist" is a task); keywords only fill in.
        kind=kind if kind in ("event", "task") else "event" if EVENT.search(str(item.get("text") or "")) else "task",
        urgency=urgency,
        urgencyUnclear=unclear,
        category=category
        if isinstance(category, str) and category in CATEGORIES
        else category_for(str(item.get("text") or "")),
        location=location.strip() if _in_transcript(location, transcript) else None,
        **_recurrence(item, tz),
    )


# Weekday names in day evidence, after folding. Words that are also numbers or
# "week" (tnen = two, jem3a = a week, التنين = both) count only after nhar/yom.
_DAY_PREFIX = r"(?:nhar|nahar|yom|youm|yawm|يوم|نهار)\s+(?:el\s+|l\s+|al\s+)?"
_WEEKDAY_NAMES = (
    (0, r"monday|الاثنين", r"tnen|etnen|itnen|tanen|ithnayn|اثنين|الاتنين|اتنين|التنين"),
    (1, r"tuesday|الثلاثاء|الثلاثا|التلاتا", r"talata|tlata|tleta|tlete|ثلاثاء|تلاتا"),
    (2, r"wednesday|الاربعاء|الاربعا", r"arba3a|arb3a|erb3a|arbi3a|اربعاء|اربعا"),
    (3, r"thursday|الخميس|khamis|5amis|khmis", r"خميس"),
    (4, r"friday", r"jem3a|jom3a|jum3a|jim3a|الجمعه|جمعه"),
    (5, r"saturday|السبت|sabt|sabet|sebt", r"سبت"),
    (6, r"sunday|الاحد", r"a7ad|ahad|احد"),
)
_RELATIVE = re.compile(r"(?<!\w)(?:before|after|until|till|abel|2abel|ba3d|la7ad|قبل|بعد|حتى|لحد)(?!\w)")


def _named_weekday(evidence) -> int | None:
    """The one weekday the day evidence names (0 = Monday), or None when it names
    none, several, or a relative day ("before Monday")."""
    if not isinstance(evidence, str):
        return None
    text = _fold(evidence)
    if _RELATIVE.search(text):
        return None
    named = {
        day
        for day, bare, prefixed in _WEEKDAY_NAMES
        if re.search(rf"(?<!\w)(?:{bare})(?!\w)|{_DAY_PREFIX}(?:{prefixed})(?!\w)", text)
    }
    return named.pop() if len(named) == 1 else None


def verified_day(item: dict, transcript: str, today: date | None = None) -> tuple:
    """(dueDate, time) — dropped when the model named a day but its evidence isn't
    in the transcript (an invented day, e.g. "today" for an urgent task). Replies
    without a dueDateEvidence key keep the model's values. When the evidence names
    one weekday and the date falls on another, the date moves to the nearest such
    weekday (keeping the model's week), never before `today`."""
    due, time = item.get("dueDate"), item.get("time")
    evidence = item.get("dueDateEvidence")
    if due and "dueDateEvidence" in item and not _in_transcript(evidence, transcript):
        return None, None
    weekday = _named_weekday(evidence)
    if isinstance(due, str) and weekday is not None:
        try:
            day = date.fromisoformat(due)
        except ValueError:
            return due, time
        forward = (weekday - day.weekday()) % 7
        day += timedelta(days=forward if forward <= 3 else forward - 7)
        while today and day < today:
            day += timedelta(days=7)
        due = day.isoformat()
    return due, time


def questions(task) -> list[dict]:
    """Ask only what the AI couldn't work out: an event's day and time, an
    urgency it couldn't judge, a repeat it couldn't pin down."""
    dismissed = set(getattr(task, "dismissedQuestions", []) or [])
    output = []
    is_event = task.kind == "event"
    # A repeat needs a start day unless the repeat itself says when: every day,
    # or weekly on named days ("every Monday and Thursday").
    repeat_needs_day = bool(task.recurrence) and not (
        task.recurrence == "daily"
        or (task.recurrence == "weekly" and getattr(task, "recurrenceWeekdays", None))
    )
    repeat_says_when = bool(task.recurrence) and not repeat_needs_day
    if (repeat_needs_day or (is_event and not repeat_says_when)) and not task.dueDate:
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
