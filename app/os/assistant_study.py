"""The assistant's study tools (design 2026-11-18, the assistant in every section): courses,
assessments (with grades) and study sessions. Study has no workspaces (ruling R3)."""

import re

from app.db import db
from app.os import assistant_sections, study
from app.os import assistant_tools as t
from app.os.assistant_tools import Kind, ToolError
from app.os.calendar_utils import local_date_time

FIELDS = {
    "course": {"title", "code", "instructor", "term", "archived"},
    "assessment": {"course_id", "title", "type", "due", "weight", "grade", "max_grade", "status"},
    "session": {"minutes", "course_id", "topic", "date"},
}
COMMON = {"kind", "id"}
LOCAL = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:T([01][0-9]|2[0-3]):([0-5][0-9]))?")
END_OF_DAY, NOON = "23:59", "12:00"  # a due day without a time is due by its end; a session's day without one, midday
LINKS = {"course": lambda id: f"/study?course={id}", "assessment": lambda id: f"/study?assessment={id}", "study_session": lambda id: "/study"}


def _kind(i: dict, allowed: tuple) -> str:
    kind = i.get("kind")
    if kind not in allowed:
        raise ToolError(f'"kind" must be {" or ".join(allowed)}')
    for field in i:
        if field not in COMMON and field not in FIELDS[kind] and i[field] is not None:
            raise ToolError(f'"{field}" is not a field of a {kind}')
    return kind


def _local(v, field: str, default_time: str, ctx) -> str | None:
    if v is None:
        return None
    m = LOCAL.fullmatch(v) if isinstance(v, str) else None
    if not m or not t.opt_date(m.group(1)):
        raise ToolError(f'"{field}" must be YYYY-MM-DD or YYYY-MM-DDTHH:MM')
    return t._iso(local_date_time(m.group(1), f"{m.group(2)}:{m.group(3)}" if m.group(2) else default_time, ctx.tz))


def _num(i: dict, field: str, key: str, plain: dict) -> None:
    if t._number(i.get(field)):
        plain[key] = i[field]


def _plain(kind: str, i: dict, ctx) -> dict:
    plain: dict = {}
    if kind == "course":
        if t._text(i, "title"):
            plain["name"] = t._text(i, "title")
        for field in ("code", "instructor", "term"):
            if t._text(i, field):
                plain[field] = t._text(i, field)
        if isinstance(i.get("archived"), bool):
            plain["isArchived"] = i["archived"]
    elif kind == "assessment":
        if t._text(i, "course_id"):
            plain["courseId"] = t._text(i, "course_id")
        if t._text(i, "title"):
            plain["title"] = t._text(i, "title")
        if i.get("type") in study.ASSESSMENT_TYPES:
            plain["type"] = i["type"]
        if i.get("status") in study.ASSESSMENT_STATUSES:
            plain["status"] = i["status"]
        if i.get("due") is not None:
            plain["dueAt"] = _local(i["due"], "due", END_OF_DAY, ctx)
        _num(i, "weight", "weight", plain)
        _num(i, "grade", "grade", plain)
        _num(i, "max_grade", "maxGrade", plain)
    else:
        if t._number(i.get("minutes")) and float(i["minutes"]).is_integer():
            plain["durationMinutes"] = int(i["minutes"])
        if t._text(i, "course_id"):
            plain["courseId"] = t._text(i, "course_id")
        if t._text(i, "topic"):
            plain["topic"] = t._text(i, "topic")
        if i.get("date") is not None:
            plain["startAt"] = _local(i["date"], "date", NOON, ctx)
    return plain


async def _add_study_item(i: dict, ctx) -> dict:
    kind = _kind(i, ("course", "assessment", "session"))
    plain = _plain(kind, i, ctx)
    if kind == "course":
        plain["name"] = t._req_str(i.get("title"), "title")
        made = await study.create_course(t._check(study.CourseBody, plain), user=ctx.user)
        entity, label = "course", made["name"]
    elif kind == "assessment":
        t._req_str(i.get("course_id"), "course_id")
        t._req_str(i.get("title"), "title")
        made = await study.create_assessment(t._check(study.AssessmentBody, plain), user=ctx.user)
        entity, label = "assessment", made["title"]
    else:
        if "durationMinutes" not in plain:
            raise ToolError('"minutes" is required for a session')
        made = await study.create_session(t._check(study.SessionBody, plain), user=ctx.user)
        entity, label = "study_session", made.get("topic") or f"{made['durationMinutes']} min"
    return t._done({"kind": "create", "entityType": entity, "entityId": made["id"], "label": label, "link": LINKS[entity](made["id"]),
                    "undo": {"op": "delete", "type": entity, "id": made["id"]}}, {"id": made["id"], "kind": kind, "title": label})


async def _update_study_item(i: dict, ctx) -> dict:
    kind = _kind(i, ("course", "assessment"))
    id = t._req_str(i.get("id"), "id")
    plain = _plain(kind, i, ctx)
    if not plain:
        raise ToolError("Nothing to change")
    keys = set(plain)
    if kind == "assessment" and "grade" in plain:
        keys |= {"status", "maxGrade"}  # a grade marks it graded (and out of 100 by default)
    before = await t.fields_now(kind, id, keys, ctx)
    if kind == "course":
        made = await study.update_course(id, t._check(study.CourseUpdateBody, plain), user=ctx.user)
        label = made["name"]
    else:
        made = await study.update_assessment(id, t._check(study.AssessmentUpdateBody, plain), user=ctx.user)
        label = made["title"]
    return t._done({"kind": "update", "entityType": kind, "entityId": id, "label": label, "link": LINKS[kind](id),
                    "undo": await t._fields_undo(kind, id, before, ctx)}, {"id": id, "kind": kind, "title": label})


# ----- item types --------------------------------------------------------------------------


async def _course_classes(user_id: str, id: str) -> list:
    """The weekly CLASS events remove_course deletes with the course (by hand, not by foreign key)."""
    rows = await db.event.find_many(where={"userId": user_id, "workspaceId": None, "courseId": id, "type": "CLASS"})
    return [("Event", e.id) for e in rows]


async def _restore_course(ctx, id: str, before: dict) -> None:
    await study.update_course(id, t._check(study.CourseUpdateBody, before), user=ctx.user)


async def _restore_assessment(ctx, id: str, before: dict) -> None:
    await study.update_assessment(id, t._check(study.AssessmentUpdateBody, before), user=ctx.user)


async def _restore_session(ctx, id: str, before: dict) -> None:
    await study.update_session(id, t._check(study.SessionUpdateBody, before), user=ctx.user)


KINDS = {
    "course": Kind(model="Course", find=study.find_course, label=lambda r: r.name, link=lambda r: LINKS["course"](r.id),
                   delete=study.remove_course, restore=_restore_course, companions=_course_classes),
    "assessment": Kind(model="Assessment", find=study._find_assessment, label=lambda r: r.title, link=lambda r: LINKS["assessment"](r.id),
                       delete=study.remove_assessment, restore=_restore_assessment),
    "study_session": Kind(model="StudySession", find=study._find_session, label=lambda r: r.topic or f"{r.durationMinutes} min",
                          link=lambda r: "/study", delete=study.remove_session, restore=_restore_session),
}
TOOLS = {"add_study_item": _add_study_item, "update_study_item": _update_study_item}

t.register(TOOLS, KINDS)
assistant_sections.SECTION_OF.update({name: "study" for name in TOOLS})
