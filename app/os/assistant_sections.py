"""The assistant in every section (design 2026-11-18): get_section reads ten vournal life sections
through their own list routes and keeps a few fields each, with ids for the change tools; and the
section switch, so a tool of a section the user turned off says so instead of adding things they
can't see. A document's number and notes never reach the model; the rituals' reflective writing
only with the journal permission."""

from datetime import timedelta

from app.db import db
from app.os import assistant_tools as t
from app.os.assistant_tools import ToolError
from app.os.common import dates
from app.os.common.serialize import to_json
from app.os.common.space import personal as personal_space

MAX_ITEMS = 30
MAX_DAYS = 31  # a meal plan read covers at most this many days

# tool name -> the Settings section it belongs to (filled by the section modules)
SECTION_OF: dict[str, str] = {}
SECTION_NAMES = {"meals": "Meals", "finance": "Finance", "documents": "Documents", "maintenance": "Maintenance", "travel": "Travel",
                 "study": "Study", "work": "Work"}
# get_section's sections -> Settings section (rituals live on the dashboard: no switch)
SECTIONS = {"meal_plan": "meals", "recipes": "meals", "budgets": "finance", "recurring_payments": "finance", "documents": "documents",
            "maintenance": "maintenance", "trips": "travel", "study": "study", "work": "work", "rituals": None}
DOCUMENT_SECRETS = ("documentNumber", "documentNumberMasked", "hasDocumentNumber", "notes", "hasNotes")
DAY_REFLECTIONS = ("intention", "win", "mood", "note")
WEEK_REFLECTIONS = ("wentWell", "improve")


async def section_refusal(ctx, key: str | None) -> str | None:
    if key is None:
        return None
    from app.os.profile import ensure_os_profile
    from app.os.settings import normalize_sections

    if key in normalize_sections((await ensure_os_profile(ctx.user)).enabledSections):
        return None
    return f"Error: The {SECTION_NAMES.get(key, key.title())} section is turned off in Settings."


def scrub_document(view: dict) -> dict:
    """A document as the model may see it: never its number or notes (owner decision 2026-11-18)."""
    return {k: v for k, v in view.items() if k not in DOCUMENT_SECRETS}


def day(v) -> str | None:
    return to_json(v)[:10] if v else None


def num(v):
    return None if v is None else float(v)


def pick(row: dict, *fields: str) -> dict:
    return {f: row.get(f) for f in fields}


def capped(items: list) -> dict:
    out = {"items": items[:MAX_ITEMS]}
    if len(items) > MAX_ITEMS:
        out["more"] = True
    return out


async def _meal_plan(i, ctx) -> dict:
    from app.os import meal_plan

    lo, hi = await meal_plan._range(ctx.user, t.opt_date(i.get("from")), t.opt_date(i.get("to")))
    if hi - lo > timedelta(days=MAX_DAYS - 1):
        hi = lo + timedelta(days=MAX_DAYS - 1)
    listed = await meal_plan.list_entries(t._req({"from": dates.format_date_only(lo), "to": dates.format_date_only(hi)}), user=ctx.user)
    items = [{"id": e["id"], "date": day(e["date"]), "meal": e["mealType"], "title": e["title"], "recipeId": e["recipeId"],
              "servings": e["servings"]} for e in listed["items"]]
    return {"from": listed["from"], "to": listed["to"], **capped(items)}


async def _recipes(i, ctx) -> dict:
    from app.os import recipes

    if t._text(i, "id"):
        r = await recipes.get(t._text(i, "id"), user=ctx.user)
        return {**pick(r, "id", "name", "description", "servings", "prepMinutes", "cookMinutes", "instructions", "isFavorite"),
                "ingredients": [{"name": g["name"], "quantity": num(g["quantity"]), "unit": g["unit"]} for g in r["ingredients"]]}
    query = t._text(i, "query")
    listed = await recipes.list_recipes(t._req({"q": query[:200] if query else None}), user=ctx.user)
    return capped([pick(r, "id", "name", "servings", "isFavorite") for r in listed["items"]])


async def _budgets(i, ctx) -> dict:
    from app.os import budgets

    listed = await budgets.list_budgets(t._req({}), user=ctx.user)
    return capped([{**pick(b, "id", "name", "currency", "period"), "amount": num(b["amount"]), "category": (b.get("category") or {}).get("name"),
                    "spent": num(b["spent"]), "remaining": num(b["remaining"]), "percentUsed": b["percentUsed"]} for b in listed["items"]])


async def _recurring_payments(i, ctx) -> dict:
    from app.os import recurring_payments

    listed = await recurring_payments.list_payments(t._req({}), user=ctx.user)
    return capped([{**pick(p, "id", "name", "currency", "kind", "cycle", "status"), "amount": num(p["amount"]), "nextDate": day(p["nextDate"])}
                   for p in listed["items"]])


async def _documents(i, ctx) -> dict:
    from app.os import documents

    query = t._text(i, "query")
    listed = await documents.list_documents(t._req({"q": query[:100] if query else None}), user=ctx.user)
    return capped([{**pick(scrub_document(d), "id", "name", "type", "issuer", "holderName", "daysRemaining", "status"),
                    "issueDate": day(d["issueDate"]), "expiryDate": day(d["expiryDate"])} for d in listed["items"]])


async def _maintenance(i, ctx) -> dict:
    from app.os import maintenance

    listed = await maintenance.list_items(t._req({}), user=ctx.user)
    return capped([{**pick(m, "id", "title", "area", "currency"), "cycle": m["frequencyLabel"], "nextDueAt": day(m["nextDueAt"]),
                    "lastDoneAt": day(m["lastDoneAt"]), "estimatedCost": num(m["estimatedCost"])} for m in listed["items"]])


def _trip(v: dict) -> dict:
    return {**pick(v, "id", "name", "destination", "status", "currency"), "startDate": day(v["startDate"]), "endDate": day(v["endDate"]),
            "budget": num(v.get("budget"))}


async def _trips(i, ctx) -> dict:
    from app.os import trips

    if t._text(i, "id"):
        v = await trips.get_trip(t._text(i, "id"), user=ctx.user)
        return {**_trip(v),
                "itinerary": [{**pick(x, "id", "title", "type", "location", "isDone"), "startAt": to_json(x["startAt"]) if x.get("startAt") else None}
                              for x in v["items"]][:MAX_ITEMS],
                "packing": [pick(p, "id", "name", "quantity", "group", "isPacked") for p in v["packing"]][:MAX_ITEMS * 2]}
    query = t._text(i, "query")
    listed = await trips.list_trips(t._req({"q": query[:100] if query else None}), user=ctx.user)
    return capped([_trip(v) for v in listed["items"]])


def _assessment(a: dict) -> dict:
    return {**pick(a, "id", "title", "type", "status"), "dueAt": to_json(a["dueAt"]) if a.get("dueAt") else None,
            "course": (a.get("course") or {}).get("name"), "weight": num(a.get("weight")), "grade": num(a.get("grade")),
            "maxGrade": num(a.get("maxGrade"))}


async def _study(i, ctx) -> dict:
    from app.os import study

    o = await study.overview(user=ctx.user)
    sessions = await study.list_sessions(t._req({"limit": 10}), user=ctx.user)
    return {"courses": [pick(c, "id", "name", "code") for c in o["courses"]][:MAX_ITEMS],
            "upcoming": [_assessment(a) for a in o["upcoming"]][:MAX_ITEMS],
            "overdue": [_assessment(a) for a in o["overdue"]][:MAX_ITEMS],
            "sessions": [{"id": s["id"], "minutes": s["durationMinutes"], "topic": s.get("topic"), "course": (s.get("course") or {}).get("name"),
                          "startAt": to_json(s["startAt"]) if s.get("startAt") else None} for s in sessions["items"]]}


async def _work(i, ctx) -> dict:
    from app.os import work

    o = await work.overview(personal_space(ctx.user))
    tasks = [{"id": x["id"], "title": x["title"], "due": to_json(x["dueAt"]) if x.get("dueAt") else None, "priority": x["priority"], "group": group}
             for group in ("overdue", "today", "upcoming", "noDate") for x in o["tasks"][group]]
    return {"tasks": tasks[:MAX_ITEMS],
            "meetings": [{"id": m["id"], "title": m["title"], "date": m.get("date"), "allDay": m.get("allDay")} for m in o["meetings"]][:MAX_ITEMS],
            "projects": [pick(p, "id", "name", "status", "progress") for p in o["projects"]][:MAX_ITEMS],
            "deadlines": [{"kind": d["kind"], "id": d["id"], "title": d["title"], "date": day(d["date"])} for d in o["deadlines"]][:MAX_ITEMS]}


async def _rituals(i, ctx) -> dict:
    from app.os import rituals

    asked = t.opt_date(i.get("from"))
    o = await rituals.overview(user=ctx.user)
    when = asked or o["today"]
    row = await db.daylog.find_unique(where={"userId_date": {"userId": ctx.user_id, "date": rituals.key_to_date(when)}})
    d = rituals._day_view(row) if row else {"planned": False, "reviewed": False, "intention": None, "win": None, "mood": None, "note": None}
    w = await rituals.week(t._req({"date": when}), user=ctx.user)
    review = w.get("review") or {}
    score = w.get("score")
    out_day = {"date": when, "planned": d["planned"], "reviewed": d["reviewed"]}
    out_week = {"weekStart": w["weekStart"], "priorities": review.get("priorities") or [], "reviewed": bool(review.get("reviewed")),
                "lifeScore": score.get("total") if isinstance(score, dict) else score}
    if ctx.journal:  # the reflective writing: only with the journal permission (owner 2026-11-18)
        out_day.update({f: d.get(f) for f in DAY_REFLECTIONS})
        out_week.update({f: review.get(f) for f in WEEK_REFLECTIONS})
    return {"day": out_day, "week": out_week}


_READERS = {"meal_plan": _meal_plan, "recipes": _recipes, "budgets": _budgets, "recurring_payments": _recurring_payments,
            "documents": _documents, "maintenance": _maintenance, "trips": _trips, "study": _study, "work": _work, "rituals": _rituals}


async def _get_section(i: dict, ctx) -> dict:
    section = i.get("section")
    if section not in SECTIONS:
        raise ToolError(f"section must be one of {', '.join(SECTIONS)}")
    refusal = await section_refusal(ctx, SECTIONS[section])
    if refusal:
        return {"content": refusal, "isError": True}
    return t._json(await _READERS[section](i, ctx))


t.register({"get_section": _get_section}, {})
