"""POST /api/onboarding/starter (port of vournalOS OnboardingService): a template's starter
items, in the user's language and only for enabled sections, created once per account (a
retry or a double click can't duplicate them). The packs are generated from the NestJS
build (tests/vournal_os/golden/starter_golden.mjs)."""

import json
from datetime import timedelta
from pathlib import Path

from fastapi import Depends
from prisma import Json

from app.db import db
from app.os.common import dates
from app.os.common.body import RuleBody, choice
from app.os.common.deps import plus_user
from app.os.common.http import nest_router
from app.os.common.i18n import default_name, to_lang
from app.os.profile import ensure_os_profile

router = nest_router("/onboarding")

_DATA = json.loads((Path(__file__).parent / "data" / "starter_packs.json").read_text())
TEMPLATES = tuple(_DATA["templates"])
STARTER_ACTION = "onboarding.starter"


class StarterBody(RuleBody):
    RULES = (("template", False, False, choice(TEMPLATES)),)
    template: object = None


@router.post("/starter")
async def starter(body: StarterBody, user=Depends(plus_user)):
    if await db.activitylog.count(where={"userId": user.id, "action": STARTER_ACTION}):
        return {"created": False, "tasks": 0, "habits": 0, "goals": 0, "projects": 0, "shoppingItems": 0, "notes": 0}
    s = await ensure_os_profile(user)
    lang = to_lang(s.language)
    on = set(s.enabledSections).__contains__
    pack = _DATA["packs"][body.template]
    today = dates.today_date_only(s.timezone)

    def due(days: int):
        """All-day tasks sit at local midnight of their day, as the tasks section stores them."""
        return dates.start_of_day(today + timedelta(days=days, hours=12), s.timezone)

    counts = {"created": True, "tasks": 0, "habits": 0, "goals": 0, "projects": 0, "shoppingItems": 0, "notes": 0}
    async with db.tx() as tx:
        for position, t in enumerate(pack["tasks"], start=1):
            await tx.task.create(data={"userId": user.id, "title": t["title"][lang], "priority": t.get("priority") or "MEDIUM",
                                       "dueAt": None if t.get("dueInDays") is None else due(t["dueInDays"]), "position": position})
            counts["tasks"] += 1
        if on("habits"):
            for sort_order, h in enumerate(pack["habits"]):
                await tx.habit.create(data={
                    "userId": user.id, "name": h["name"][lang], "icon": h["icon"], "color": h["color"], "frequency": h.get("frequency") or "DAILY",
                    "daysOfWeek": h.get("daysOfWeek") or [], "timesPerWeek": h.get("timesPerWeek"), "targetPerDay": h.get("targetPerDay") or 1,
                    "unit": h["unit"][lang] if h.get("unit") else None, "startDate": today, "sortOrder": sort_order,
                })
                counts["habits"] += 1
        if on("goals"):
            for g in pack["goals"]:
                await tx.goal.create(data={
                    "userId": user.id, "name": g["name"][lang], "description": g["description"][lang] if g.get("description") else None,
                    "type": g["type"], "icon": g["icon"], "currency": s.defaultCurrency if g["type"] == "FINANCIAL" else None, "startDate": today,
                })
                counts["goals"] += 1
        project = pack.get("project")
        if on("projects") and project:
            row = await tx.project.create(data={"userId": user.id, "name": project["name"][lang], "description": project["description"][lang],
                                                "icon": project["icon"], "color": project["color"], "status": "ACTIVE"})
            for p, title in enumerate(project["tasks"], start=1):
                await tx.task.create(data={"userId": user.id, "projectId": row.id, "title": title[lang], "position": 100 + p})
                counts["tasks"] += 1
            counts["projects"] += 1
        if on("shopping") and pack.get("shopping"):
            lst = await tx.shoppinglist.find_first(where={"userId": user.id, "isArchived": False}, order=[{"sortOrder": "asc"}, {"createdAt": "asc"}])
            if lst is None:
                lst = await tx.shoppinglist.create(data={"userId": user.id, "name": default_name("Grocery", lang), "icon": "shopping-basket",
                                                         "color": "#22c55e", "currency": s.defaultCurrency})
            for order, name in enumerate(pack["shopping"]):
                await tx.shoppingitem.create(data={"userId": user.id, "listId": lst.id, "name": name[lang], "sortOrder": order})
                counts["shoppingItems"] += 1
        if on("notes"):
            await tx.note.create(data={"userId": user.id, "title": pack["note"]["title"][lang], "content": pack["note"]["content"][lang], "isPinned": True})
            counts["notes"] += 1
        # the marker is written with the items, so "ran once" and "items exist" never disagree
        await tx.activitylog.create(data={"userId": user.id, "action": STARTER_ACTION, "metadata": Json({"template": body.template, **counts})})
    return counts
