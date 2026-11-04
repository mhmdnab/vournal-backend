"""/api/search (port of vournalOS SearchService): a case-insensitive search over every
enabled section (2 characters at least), best matches first, grouped by type. Document
numbers and notes are encrypted and never searched (names and issuers only)."""

import re

from fastapi import Depends, Request

from app.db import db
from app.os.common.currency import to_number
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import fmt, to_lang
from app.os.common.jsparse import _JS_SPACE
from app.os.common.query import _query_int, _query_list, unknown_params
from app.os.insight_text import enum_label, fmt_day, ltr, money, shared
from app.os.insight_time import day_key
from app.os.life_data import iso_ms
from app.os.profile import ensure_os_profile

router = nest_router("/search")

SEARCH_TYPES = ("task", "project", "event", "expense", "income", "goal", "habit", "note", "shopping", "subscription", "bill", "trip",
                "person", "document", "course", "assessment", "reminder", "maintenance", "recipe", "debt")
TYPE_SECTION = {"task": "tasks", "project": "projects", "event": "calendar", "expense": "finance", "income": "finance", "goal": "goals",
                "habit": "habits", "note": "notes", "shopping": "shopping", "subscription": "finance", "bill": "finance", "trip": "travel",
                "person": "people", "document": "documents", "course": "study", "assessment": "study", "reminder": "reminders",
                "maintenance": "maintenance", "recipe": "meals", "debt": "finance"}
MESSAGES = {
    "en": {"project": "Project · {status}", "repeats": "Repeats", "subscription": "Subscription", "goal": "{type} goal · {status}",
           "habit": "{frequency} habit", "habitArchived": "{frequency} habit · Archived", "note": "Note", "noteArchived": "Archived note",
           "purchased": "Purchased", "toBuy": "To buy", "person": "Person", "debtIOwe": "I owe {amount}", "debtOwedToMe": "Owed to me · {amount}",
           "documentExpires": "{type} · expires {date}", "course": "Course", "reminder": "Reminder", "reminderDone": "Reminder · Done",
           "recipe": "Recipe", "recipeMinutes": "Recipe · {minutes} min"},
    "ar": {"project": "مشروع · {status}", "repeats": "متكرر", "subscription": "اشتراك", "goal": "هدف {type} · {status}",
           "habit": "عادة · {frequency}", "habitArchived": "عادة · {frequency} · مؤرشفة", "note": "ملاحظة", "noteArchived": "ملاحظة مؤرشفة",
           "purchased": "تم الشراء", "toBuy": "للشراء", "person": "شخص", "debtIOwe": "عليّ {amount}", "debtOwedToMe": "لي {amount}",
           "documentExpires": "{type} · تنتهي صلاحيته في {date}", "course": "مقرر", "reminder": "تذكير", "reminderDone": "تذكير · تم",
           "recipe": "وصفة", "recipeMinutes": "وصفة · {minutes} د"},
}
_SPACES = re.compile("[" + re.escape(_JS_SPACE) + "]+")
_WORD_BREAK = re.compile("[" + re.escape(_JS_SPACE) + r"\-_/.,،؛]+")
_MARKDOWN = re.compile(r"[#*_`>\[\]()!-]+")


def _u16(text: str) -> bytes:
    return text.encode("utf-16-le", "surrogatepass")


def _slice16(text: str, start: int, end: int | None = None) -> str:
    """String#slice by UTF-16 units (a half emoji at a cut is dropped)."""
    raw = _u16(text)
    return raw[start * 2:None if end is None else end * 2].decode("utf-16-le", errors="ignore")


def _len16(text: str) -> int:
    return len(_u16(text)) // 2


def rank_title(title: str, needle: str) -> int:
    """0 exact · 1 prefix · 2 a word's prefix · 3 contains · 4 matched another field."""
    t = title.lower()
    if t == needle:
        return 0
    if t.startswith(needle):
        return 1
    if any(w.startswith(needle) for w in _WORD_BREAK.split(t)):
        return 2
    if needle in t:
        return 3
    return 4


def snippet(content: str, q: str) -> str:
    if not content:
        return ""
    plain = _SPACES.sub(" ", _MARKDOWN.sub(" ", content)).strip(_JS_SPACE)
    found = _u16(plain.lower()).find(_u16(q.lower()))
    idx = found // 2 if found >= 0 and found % 2 == 0 else -1
    if idx < 0:
        return _slice16(plain, 0, 80) + ("…" if _len16(plain) > 80 else "")
    start = max(0, idx - 30)
    return ("…" if start > 0 else "") + _slice16(plain, start, start + 90) + ("…" if start + 90 < _len16(plain) else "")


def _joined(*parts) -> str:
    return " · ".join(p for p in parts if p)


async def search(user, q_raw: str | None, types: list[str] | None, limit: int | None) -> dict:
    q = _SPACES.sub(" ", (q_raw or "").strip(_JS_SPACE))
    if _len16(q) < 2:
        return {"results": []}
    limit = limit or 30
    per_type = min(limit, 8)
    settings = await ensure_os_profile(user)
    enabled = set(settings.enabledSections)
    wanted = set(types or SEARCH_TYPES)

    def want(t: str) -> bool:
        return t in wanted and TYPE_SECTION[t] in enabled

    like = {"contains": q, "mode": "insensitive"}
    tz, lang = settings.timezone, to_lang(settings.language)
    M = MESSAGES[lang]
    uid = user.id
    out: list[dict] = []

    def add(kind, id, title, subtitle, link, date, section) -> None:
        out.append({"type": kind, "id": id, "title": title, "subtitle": subtitle, "link": link, "date": date, "section": section})

    if want("task"):
        for t in await db.task.find_many(where={"userId": uid, "OR": [{"title": like}, {"description": like}, {"notes": like}]},
                                         include={"project": True}, order=[{"status": "asc"}, {"updatedAt": "desc"}], take=per_type):
            add("task", t.id, t.title, _joined(enum_label(lang, "taskStatus", t.status),
                                               enum_label(lang, "priority", t.priority) if t.priority != "MEDIUM" else None, t.project.name if t.project else None),
                f"/tasks?id={t.id}", iso_ms(t.dueAt) if t.dueAt else None, "tasks")
    if want("project"):
        for p in await db.project.find_many(where={"userId": uid, "OR": [{"name": like}, {"description": like}, {"notes": like}]},
                                            order={"updatedAt": "desc"}, take=per_type):
            add("project", p.id, p.name, fmt(M["project"], status=enum_label(lang, "projectStatus", p.status)), f"/projects/{p.id}",
                iso_ms(p.deadline) if p.deadline else None, "projects")
    if want("event"):
        for e in await db.event.find_many(where={"userId": uid, "OR": [{"title": like}, {"description": like}, {"location": like}]},
                                          order={"startAt": "desc"}, take=per_type):
            add("event", e.id, e.title, _joined(enum_label(lang, "eventType", e.type), M["repeats"] if e.rrule else None, e.location),
                f"/calendar?id={e.id}&date={day_key(e.startAt, tz)}", iso_ms(e.startAt), "calendar")
    if want("expense") or want("income"):
        kinds = [k for k, t in (("EXPENSE", "expense"), ("INCOME", "income")) if want(t)]
        for t in await db.transaction.find_many(where={"userId": uid, "type": {"in": kinds},
                                                       "OR": [{"title": like}, {"source": like}, {"notes": like}, {"category": {"is": {"name": like}}}]},
                                                include={"category": True}, order={"date": "desc"}, take=per_type * len(kinds)):
            income = t.type == "INCOME"
            amount = money("en", to_number(t.amount), t.currency)
            category = t.category.name if t.category else None
            add("income" if income else "expense", t.id,
                t.title or t.source or category or shared(lang, "income" if income else "expense"),
                _joined(ltr(lang, f"{'+' if income else '−'}{amount}"), category, t.source if income else None),
                f"/finance/transactions?id={t.id}", iso_ms(t.date), "finance")
    if want("subscription") or want("bill"):
        kinds = ([{"kind": "SUBSCRIPTION"}] if want("subscription") else []) + ([{"kind": {"not": "SUBSCRIPTION"}}] if want("bill") else [])
        for r in await db.recurringpayment.find_many(where={"userId": uid, "AND": [{"OR": kinds}, {"OR": [{"name": like}, {"notes": like}]}]},
                                                     order={"nextDate": "asc"}, take=per_type):
            sub = r.kind == "SUBSCRIPTION"
            add("subscription" if sub else "bill", r.id, r.name,
                _joined(money(lang, to_number(r.amount), r.currency), M["subscription"] if sub else enum_label(lang, "recurringKind", r.kind),
                        enum_label(lang, "recurringStatus", r.status) if r.status != "ACTIVE" else None),
                f"/finance/subscriptions?id={r.id}" if sub else f"/finance/recurring?id={r.id}", iso_ms(r.nextDate), "finance")
    if want("goal"):
        for g in await db.goal.find_many(where={"userId": uid, "OR": [{"name": like}, {"description": like}, {"notes": like}]},
                                         order={"updatedAt": "desc"}, take=per_type):
            add("goal", g.id, g.name, fmt(M["goal"], type=enum_label(lang, "goalType", g.type), status=enum_label(lang, "goalStatus", g.status)),
                f"/goals/{g.id}", iso_ms(g.deadline) if g.deadline else None, "goals")
    if want("habit"):
        for h in await db.habit.find_many(where={"userId": uid, "OR": [{"name": like}, {"description": like}]}, order={"sortOrder": "asc"}, take=per_type):
            add("habit", h.id, h.name, fmt(M["habitArchived" if h.isArchived else "habit"], frequency=enum_label(lang, "habitFrequency", h.frequency)),
                f"/habits?id={h.id}", None, "habits")
    if want("note"):
        for n in await db.note.find_many(where={"userId": uid, "OR": [{"title": like}, {"content": like}]},
                                         order=[{"isArchived": "asc"}, {"updatedAt": "desc"}], take=per_type):
            add("note", n.id, n.title, snippet(n.content, q) or (M["noteArchived"] if n.isArchived else M["note"]), f"/notes?id={n.id}",
                iso_ms(n.updatedAt), "notes")
    if want("shopping"):
        for s in await db.shoppingitem.find_many(where={"userId": uid, "OR": [{"name": like}, {"store": like}, {"notes": like}]},
                                                 include={"list": True}, order=[{"isPurchased": "asc"}, {"updatedAt": "desc"}], take=per_type):
            add("shopping", s.id, s.name, f"{s.list.name} · {M['purchased'] if s.isPurchased else M['toBuy']}", f"/shopping?list={s.list.id}&id={s.id}",
                None, "shopping")
    if want("trip"):
        for t in await db.trip.find_many(where={"userId": uid, "OR": [{"name": like}, {"destination": like}, {"notes": like}]},
                                         order={"startDate": "desc"}, take=per_type):
            add("trip", t.id, t.name, f"{t.destination} · {enum_label(lang, 'tripStatus', t.status)}", f"/travel/{t.id}", iso_ms(t.startDate), "travel")
    if want("person"):
        for p in await db.person.find_many(where={"userId": uid, "OR": [{"name": like}, {"relationship": like}, {"email": like}, {"notes": like}]},
                                           order={"name": "asc"}, take=per_type):
            add("person", p.id, p.name, p.relationship or M["person"], f"/people?id={p.id}", iso_ms(p.birthday) if p.birthday else None, "people")
    if want("document"):
        for d in await db.document.find_many(where={"userId": uid, "OR": [{"name": like}, {"issuer": like}]}, order={"updatedAt": "desc"}, take=per_type):
            kind = enum_label(lang, "documentType", d.type)
            if d.expiryDate:
                when = iso_ms(d.expiryDate)[:10] if lang == "en" else fmt_day(lang, iso_ms(d.expiryDate)[:10], day=True, year=True)
                subtitle = fmt(M["documentExpires"], type=kind, date=when)
            else:
                subtitle = kind
            add("document", d.id, d.name, subtitle, f"/documents?id={d.id}", iso_ms(d.expiryDate) if d.expiryDate else None, "documents")
    if want("course"):
        for c in await db.course.find_many(where={"userId": uid, "OR": [{"name": like}, {"code": like}, {"instructor": like}]},
                                           order={"updatedAt": "desc"}, take=per_type):
            add("course", c.id, c.name, _joined(M["course"], c.code, c.term), f"/study?id={c.id}", None, "study")
    if want("assessment"):
        for a in await db.assessment.find_many(where={"userId": uid, "OR": [{"title": like}, {"notes": like}]}, include={"course": True},
                                               order={"dueAt": "desc"}, take=per_type):
            add("assessment", a.id, a.title, f"{enum_label(lang, 'assessmentType', a.type)} · {a.course.name}", f"/study?id={a.course.id}&assessment={a.id}",
                iso_ms(a.dueAt) if a.dueAt else None, "study")
    if want("reminder"):
        for r in await db.reminder.find_many(where={"userId": uid, "OR": [{"title": like}, {"notes": like}]},
                                             order=[{"isDone": "asc"}, {"remindAt": "asc"}], take=per_type):
            add("reminder", r.id, r.title, M["reminderDone"] if r.isDone else M["reminder"], f"/reminders?id={r.id}", iso_ms(r.remindAt), "reminders")
    if want("maintenance"):
        for m in await db.maintenanceitem.find_many(where={"userId": uid, "OR": [{"title": like}, {"description": like}, {"notes": like}]},
                                                    order={"nextDueAt": "asc"}, take=per_type):
            add("maintenance", m.id, m.title, shared(lang, "areaMaintenance", area=enum_label(lang, "maintenanceArea", m.area)),
                f"/maintenance?id={m.id}", iso_ms(m.nextDueAt), "maintenance")
    if want("recipe"):
        for r in await db.recipe.find_many(where={"userId": uid, "OR": [{"name": like}, {"description": like}, {"ingredients": {"some": {"name": like}}}]},
                                           order={"updatedAt": "desc"}, take=per_type):
            minutes = (r.prepMinutes or 0) + (r.cookMinutes or 0)
            add("recipe", r.id, r.name, fmt(M["recipeMinutes"], minutes=minutes) if minutes else M["recipe"], f"/meals?recipe={r.id}", None, "meals")
    if want("debt"):
        for d in await db.debt.find_many(where={"userId": uid, "OR": [{"title": like}, {"counterparty": like}, {"notes": like},
                                                                      {"person": {"is": {"name": like}}}]},
                                         include={"payments": True}, order=[{"status": "asc"}, {"createdAt": "desc"}], take=per_type):
            paid = 0.0
            for p in d.payments or []:
                paid = paid + float(p.amount)
            left = max(0, float(d.principal) - paid)
            add("debt", d.id, d.title, fmt(M["debtIOwe" if d.direction == "I_OWE" else "debtOwedToMe"], amount=money(lang, left, d.currency)),
                f"/finance/debts?id={d.id}", iso_ms(d.dueDate) if d.dueDate else None, "finance")

    needle = q.lower()
    ranked = sorted(((r, rank_title(r["title"], needle), i) for i, r in enumerate(out)),
                    key=lambda x: (x[1], SEARCH_TYPES.index(x[0]["type"]), x[2]))
    # keep the best results overall, then present them grouped by type (best group first)
    kept = ranked[:limit]
    group_rank: dict[str, int] = {}
    for idx, (r, _rank, _i) in enumerate(kept):
        group_rank.setdefault(r["type"], idx)
    kept.sort(key=lambda x: (group_rank[x[0]["type"]], x[1], x[2]))
    return {"results": [r for r, _rank, _i in kept]}


@router.get("")
async def find(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("q", "types", "limit"))
    q = request.query_params.get("q")
    if q is not None and _len16(q) > 100:
        errors.append("q must be shorter than or equal to 100 characters")
    types = _query_list(request, "types", SEARCH_TYPES, errors)
    limit = _query_int(request, "limit", errors, maximum=100, minimum=1)
    if errors:
        raise nest_error(400, errors)
    return await search(user, q, types, limit)
