"""/api/study (port of vournalOS StudyService, StudyItemsService, study.util and
event-expansion): courses with their grades, assessments (assignments, exams, quizzes…),
study sessions, and the overview of the week."""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request

from app.db import db
from app.os.common import dates, rrule, validators
from app.os.common.activity import log_activity
from app.os.common.body import RuleBody, choice, iso_date, number, text
from app.os.common.currency import as_decimal
from app.os.common.deps import plus_user
from app.os.common.http import invalid_data, nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.ownership import assert_owned
from app.os.common.query import _query_bool, _query_date, _query_int, _query_list, unknown_params
from app.os.health_calc import _js_round
from app.os.profile import ensure_os_profile

router = nest_router("/study")

ASSESSMENT_TYPES = ("ASSIGNMENT", "EXAM", "QUIZ", "PROJECT", "PRESENTATION", "LAB", "OTHER")
ASSESSMENT_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "SUBMITTED", "GRADED")
OPEN_STATUSES = ("NOT_STARTED", "IN_PROGRESS")
MESSAGES = {"en": {"archivedCourse": "Archived course", "general": "General"},
            "ar": {"archivedCourse": "مقرر مؤرشف", "general": "دراسة عامة"}}

_COURSE_FIELDS = ("id", "name", "code", "instructor", "term", "credits", "color", "targetGrade", "location", "notes",
                  "isArchived", "createdAt", "updatedAt")
_ASSESSMENT_FIELDS = ("id", "courseId", "title", "type", "status", "dueAt", "location", "grade", "maxGrade", "weight",
                      "notes", "createdAt", "updatedAt")
_SESSION_FIELDS = ("id", "courseId", "startAt", "durationMinutes", "topic", "notes", "createdAt")
_CLASS_FIELDS = ("id", "title", "description", "location", "type", "color", "startAt", "endAt", "allDay", "rrule",
                 "recurrenceEndAt", "courseId")
_TASK_FIELDS = ("id", "title", "status", "priority", "dueAt", "hasDueTime", "completedAt", "courseId", "lifeArea")
_ASSESSMENT_ORDER = [{"dueAt": "asc"}, {"createdAt": "asc"}]  # nulls last, as Postgres sorts them ascending


# ----- grades (study.util) -------------------------------------------------------------


def num(value) -> float | None:
    return None if value is None else float(value)


def _round(n: float, decimals: int = 2) -> float:
    f = 10**decimals
    return _js_round(n * f) / f


def compute_grade(assessments) -> dict:
    """Current grade = Σ(grade/max × weight) / Σweight × 100 over graded assessments with a
    weight; with none weighted, the plain average. A missing max counts as 100."""
    total_weight = graded_weight = weighted_sum = simple_sum = 0.0
    graded_count = 0
    for a in assessments:
        w = num(a.weight) or 0
        total_weight += w
        g = num(a.grade)
        if g is None:
            continue
        top = num(a.maxGrade)
        top = 100 if top is None else top
        if top <= 0:
            continue
        ratio = g / top
        graded_count += 1
        simple_sum += ratio
        if w > 0:
            weighted_sum += ratio * w
            graded_weight += w
    current, method = None, None
    if graded_weight > 0:
        current, method = _round((weighted_sum / graded_weight) * 100), "weighted"
    elif graded_count > 0:
        current, method = _round((simple_sum / graded_count) * 100), "average"
    return {"currentGrade": current, "gradeMethod": method, "gradedCount": graded_count,
            "gradedWeight": _round(graded_weight), "totalWeight": _round(total_weight)}


_SCALE = ((93, 4.0, "A"), (90, 3.7, "A-"), (87, 3.3, "B+"), (83, 3.0, "B"), (80, 2.7, "B-"), (77, 2.3, "C+"),
          (73, 2.0, "C"), (70, 1.7, "C-"), (67, 1.3, "D+"), (63, 1.0, "D"), (60, 0.7, "D-"))


def percent_to_gpa(p: float) -> float:
    return next((gpa for floor, gpa, _ in _SCALE if p >= floor), 0)


def percent_to_letter(p: float) -> str:
    return next((letter for floor, _, letter in _SCALE if p >= floor), "F")


def compute_average(courses: list[dict]) -> dict:
    """Average over courses with a grade, weighted by credits when every one has them."""
    graded = [c for c in courses if c["currentGrade"] is not None]
    if not graded:
        return {"averageGrade": None, "gpa": None, "letter": None, "creditWeighted": False, "gradedCourses": 0}
    credit_weighted = all((num(c["credits"]) or 0) > 0 for c in graded)
    w_sum = p_sum = g_sum = 0.0
    for c in graded:
        w = num(c["credits"]) if credit_weighted else 1
        w_sum += w
        p_sum += c["currentGrade"] * w
        g_sum += percent_to_gpa(c["currentGrade"]) * w
    average = _round(p_sum / w_sum)
    return {"averageGrade": average, "gpa": _round(g_sum / w_sum), "letter": percent_to_letter(average),
            "creditWeighted": credit_weighted, "gradedCourses": len(graded)}


# ----- views ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def course_brief(course) -> dict | None:
    return {"id": course.id, "name": course.name, "code": course.code, "color": course.color} if course else None


def assessment_view(a, now: datetime, with_course: bool = True) -> dict:
    out = {f: getattr(a, f) for f in _ASSESSMENT_FIELDS}
    if with_course:
        out["course"] = course_brief(a.course)
    g, top = num(a.grade), num(a.maxGrade)
    top = 100 if top is None else top
    return {**out, "isOverdue": a.status in OPEN_STATUSES and a.dueAt is not None and a.dueAt < now,
            "percent": _round((g / top) * 100) if g is not None and top > 0 else None}


def session_view(s, with_course: bool = True) -> dict:
    out = {f: getattr(s, f) for f in _SESSION_FIELDS}
    return {**out, "course": course_brief(s.course)} if with_course else out


def _task_overdue(t, now: datetime, start_of_today: datetime) -> bool:
    """Timed → due before now; all-day → due before the start of today (user time)."""
    if not t.dueAt:
        return False
    return t.dueAt < now if t.hasDueTime else t.dueAt < start_of_today


def task_view(t, now: datetime, start_of_today: datetime, with_course: bool = False) -> dict:
    out = {f: getattr(t, f) for f in _TASK_FIELDS}
    if with_course:
        out["course"] = course_brief(t.course)
    return {**out, "isOverdue": t.status != "COMPLETED" and _task_overdue(t, now, start_of_today)}


async def course_stats(user_id: str, ids: list[str]) -> dict:
    """Assessments, open-task counts and study minutes for a set of courses."""
    stats = {"assessments": {}, "openTasks": {}, "studyMinutes": {}}
    if not ids:
        return stats
    for a in await db.assessment.find_many(where={"userId": user_id, "courseId": {"in": ids}}, order={"dueAt": "asc"}):
        stats["assessments"].setdefault(a.courseId, []).append(a)
    for g in await db.task.group_by(by=["courseId"], where={"userId": user_id, "workspaceId": None, "courseId": {"in": ids}, "status": {"not": "COMPLETED"}},
                                    count={"_all": True}):
        stats["openTasks"][g["courseId"]] = g["_count"]["_all"]
    for g in await db.studysession.group_by(by=["courseId"], where={"userId": user_id, "courseId": {"in": ids}},
                                            sum={"durationMinutes": True}):
        stats["studyMinutes"][g["courseId"]] = (g.get("_sum") or {}).get("durationMinutes") or 0
    return stats


def course_view(course, stats: dict) -> dict:
    now = _now()
    items = stats["assessments"].get(course.id, [])
    grade = compute_grade(items)
    open_items = [a for a in items if a.status in OPEN_STATUSES]
    upcoming = [a for a in open_items if a.dueAt and a.dueAt >= now]
    overdue = [a for a in open_items if a.dueAt and a.dueAt < now]
    nxt = upcoming[0] if upcoming else None
    target = num(course.targetGrade)
    current = grade["currentGrade"]
    return {
        **{f: getattr(course, f) for f in _COURSE_FIELDS},
        **grade,
        "letterGrade": percent_to_letter(current) if current is not None else None,
        "gradeDelta": _round(current - target) if current is not None and target is not None else None,
        "onTrack": current >= target if current is not None and target is not None else None,
        "assessmentCount": len(items),
        "upcomingCount": len(upcoming),
        "overdueCount": len(overdue),
        "nextAssessment": {"id": nxt.id, "title": nxt.title, "type": nxt.type, "status": nxt.status, "dueAt": nxt.dueAt} if nxt else None,
        "openTaskCount": stats["openTasks"].get(course.id, 0),
        "totalStudyMinutes": stats["studyMinutes"].get(course.id, 0),
    }


def _iso_ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def expand_events(events, start: datetime, end: datetime, tz: str, view=None) -> list[dict]:
    """One-off and weekly events → occurrences overlapping [start, end], on the user's
    wall clock (event-expansion.ts; prayer anchors aren't followed here, as in NestJS).
    `view(event)` is what each occurrence carries (a class, unless the caller says)."""
    out = []
    for ev in events:
        rest = view(ev) if view else {**{f: getattr(ev, f) for f in _CLASS_FIELDS}, "course": course_brief(ev.course)}
        duration = max(timedelta(0), ev.endAt - ev.startAt) if ev.endAt else timedelta(0)

        def make(first: datetime) -> dict:
            return {**rest, "occurrenceId": f"{ev.id}:{_iso_ms(first)}", "occurrenceStart": first,
                    "occurrenceEnd": first + duration if ev.endAt else None, "isRecurring": bool(ev.rrule)}

        if not ev.rrule:
            if ev.startAt <= end and (ev.endAt or ev.startAt) >= start:
                out.append(make(ev.startAt))
            continue
        for first in rrule.between(ev.rrule, ev.startAt, start - duration, end, ev.exDates or [], tz=tz, until=ev.recurrenceEndAt):
            if first <= end and first + duration >= start:
                out.append(make(first))
    return sorted(out, key=lambda o: o["occurrenceStart"])


def events_overlapping(start: datetime, end: datetime) -> dict:
    """Events that may have an occurrence overlapping [start, end]."""
    return {"OR": [
        {"rrule": None, "startAt": {"lte": end}, "OR": [{"endAt": {"gte": start}}, {"endAt": None, "startAt": {"gte": start}}]},
        {"rrule": {"not": None}, "startAt": {"lte": end}, "OR": [{"recurrenceEndAt": None}, {"recurrenceEndAt": {"gte": start}}]},
    ]}


# ----- bodies --------------------------------------------------------------------------


def _not_empty_string(value, field):
    """@IsString() @IsNotEmpty()."""
    return ([] if value not in ("", None) else [f"{field} should not be empty"]) + validators.string_rules(value, field)


def _partial(rules: tuple) -> tuple:
    return tuple((name, True, trimmed, rule) for name, _optional, trimmed, rule in rules)


COURSE_RULES = (
    ("name", False, True, text(120, 1)),
    ("code", True, True, text(30)),
    ("instructor", True, True, text(120)),
    ("term", True, True, text(60)),
    ("credits", True, False, number(0, 999, 1)),
    ("color", True, False, validators.color_rules),
    ("targetGrade", True, False, number(0, 100, 2)),
    ("location", True, True, text(200)),
    ("notes", True, False, text(10_000)),
    ("isArchived", True, False, validators.bool_rules),
)


class CourseBody(RuleBody):
    RULES = COURSE_RULES
    name: object = None
    code: object = None
    instructor: object = None
    term: object = None
    credits: object = None
    color: object = None
    targetGrade: object = None
    location: object = None
    notes: object = None
    isArchived: object = None


class CourseUpdateBody(CourseBody):
    RULES = _partial(COURSE_RULES)


ASSESSMENT_RULES = (
    ("courseId", False, False, _not_empty_string),
    ("title", False, True, text(200, 1)),
    ("type", True, False, choice(ASSESSMENT_TYPES)),
    ("status", True, False, choice(ASSESSMENT_STATUSES)),
    ("dueAt", True, False, iso_date),
    ("location", True, True, text(200)),
    ("grade", True, False, number(0, 9999, 2)),
    ("maxGrade", True, False, number(0.01, 9999, 2)),
    ("weight", True, False, number(0, 100, 2)),
    ("notes", True, False, text(10_000)),
)


class AssessmentBody(RuleBody):
    RULES = ASSESSMENT_RULES
    courseId: object = None
    title: object = None
    type: object = None
    status: object = None
    dueAt: object = None
    location: object = None
    grade: object = None
    maxGrade: object = None
    weight: object = None
    notes: object = None


class AssessmentUpdateBody(AssessmentBody):
    RULES = _partial(ASSESSMENT_RULES)


SESSION_RULES = (
    ("durationMinutes", False, False, number(1, 1440)),
    ("startAt", True, False, iso_date),
    ("courseId", True, False, validators.string_rules),
    ("topic", True, True, text(200)),
    ("notes", True, False, text(5_000)),
)


class SessionBody(RuleBody):
    RULES = SESSION_RULES
    durationMinutes: object = None
    startAt: object = None
    courseId: object = None
    topic: object = None
    notes: object = None


class SessionUpdateBody(SessionBody):
    RULES = _partial(SESSION_RULES)


def _instant(value) -> datetime | None:
    return dates.parse_instant(value) if value else None


# ----- overview ------------------------------------------------------------------------


@router.get("/overview")
async def overview(user=Depends(plus_user)):
    settings = await ensure_os_profile(user)
    tz, m = settings.timezone, MESSAGES[to_lang(settings.language)]
    now = _now()
    today = dates.range("day", now, tz)
    week = dates.range("week", now, tz, settings.weekStartsOn)
    in30 = dates.end_of_day(dates.add_days(now, 30, tz), tz)
    active = {"course": {"is": {"isArchived": False}}}
    courses = await db.course.find_many(where={"userId": user.id, "isArchived": False}, order={"name": "asc"})
    open_where = {"userId": user.id, **active, "status": {"in": list(OPEN_STATUSES)}}
    upcoming = await db.assessment.find_many(where={**open_where, "dueAt": {"gte": now, "lte": in30}}, include={"course": True},
                                             order={"dueAt": "asc"}, take=50)
    overdue = await db.assessment.find_many(where={**open_where, "dueAt": {"lt": now}}, include={"course": True},
                                            order={"dueAt": "asc"}, take=50)
    week_sessions = await db.studysession.find_many(where={"userId": user.id, "startAt": {"gte": week["from"], "lte": week["to"]}})
    class_events = await db.event.find_many(where={"userId": user.id, "workspaceId": None, "type": "CLASS", **events_overlapping(today["from"], today["to"])},
                                            include={"course": True})
    course_tasks = await db.task.find_many(where={"userId": user.id, "workspaceId": None, **active, "status": {"not": "COMPLETED"}}, include={"course": True},
                                           order=[{"dueAt": "asc"}, {"createdAt": "desc"}], take=30)

    stats = await course_stats(user.id, [c.id for c in courses])
    course_views = [course_view(c, stats) for c in courses]
    course_map = {c.id: c for c in courses}
    by_course: dict = {}
    by_day = {dates.local_date_string(dates.add_days(week["from"], i, tz), tz): 0 for i in range(7)}
    week_minutes = 0
    for s in week_sessions:
        week_minutes += s.durationMinutes
        by_course[s.courseId] = by_course.get(s.courseId, 0) + s.durationMinutes
        day = dates.local_date_string(s.startAt, tz)
        by_day[day] = by_day.get(day, 0) + s.durationMinutes
    study_by_course = sorted(
        ({"courseId": cid, "name": course_map[cid].name if cid in course_map else (m["archivedCourse"] if cid else m["general"]),
          "code": course_map[cid].code if cid in course_map else None, "color": course_map[cid].color if cid in course_map else None,
          "minutes": minutes} for cid, minutes in by_course.items()),
        key=lambda c: -c["minutes"],
    )
    todays_classes = [
        {**o, "isNow": o["occurrenceStart"] <= now <= (o["occurrenceEnd"] or o["occurrenceStart"]),
         "isPast": (o["occurrenceEnd"] or o["occurrenceStart"]) < now}
        for o in expand_events(class_events, today["from"], today["to"], tz)
    ]

    def day_diff(d):
        return None if d is None else dates.days_until(dates.to_date_only(d, tz), tz)

    upcoming_views = [{**assessment_view(a, now), "daysUntil": day_diff(a.dueAt)} for a in upcoming]
    overdue_views = [{**assessment_view(a, now), "daysUntil": day_diff(a.dueAt)} for a in overdue]
    exams = [a for a in upcoming_views if a["type"] in ("EXAM", "QUIZ")]
    return {
        "generatedAt": now,
        "week": {"from": week["from"], "to": week["to"]},
        "upcoming": upcoming_views,
        "upcomingExams": exams,
        "upcomingDeadlines": [a for a in upcoming_views if a["type"] not in ("EXAM", "QUIZ")],
        "overdue": overdue_views,
        "todaysClasses": todays_classes,
        "study": {"weekMinutes": week_minutes, "byCourse": study_by_course,
                  "byDay": [{"date": d, "minutes": v} for d, v in by_day.items()], "sessionsCount": len(week_sessions)},
        "courses": course_views,
        "average": compute_average(course_views),
        "courseTasks": [task_view(t, now, today["from"], with_course=True) for t in course_tasks],
        "counts": {"courses": len(courses), "upcoming": len(upcoming_views), "upcomingExams": len(exams),
                   "overdue": len(overdue_views), "todaysClasses": len(todays_classes), "openCourseTasks": len(course_tasks)},
    }


# ----- courses -------------------------------------------------------------------------


async def find_course(user_id: str, id: str):
    row = await db.course.find_first(where={"id": id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Course not found")
    return row


@router.get("/courses")
async def list_courses(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("includeArchived", "archived"))
    include_archived = _query_bool(request, "includeArchived", errors)
    archived = _query_bool(request, "archived", errors)
    if errors:
        raise nest_error(400, errors)
    where: dict = {"userId": user.id}
    if archived is True:
        where["isArchived"] = True
    elif not include_archived:
        where["isArchived"] = False
    courses = await db.course.find_many(where=where, order=[{"isArchived": "asc"}, {"name": "asc"}])
    stats = await course_stats(user.id, [c.id for c in courses])
    items = [course_view(c, stats) for c in courses]
    return {"items": items, "total": len(items)}


@router.post("/courses", status_code=201)
async def create_course(body: CourseBody, user=Depends(plus_user)):
    data = {f: getattr(body, f) for f in ("name", "code", "instructor", "term", "color", "location", "notes")}
    course = await db.course.create(data={
        "userId": user.id, **data, "isArchived": bool(body.isArchived),
        "credits": None if body.credits is None else as_decimal(body.credits),
        "targetGrade": None if body.targetGrade is None else as_decimal(body.targetGrade),
    })
    await log_activity(user.id, "course.created", entity_id=course.id, name=course.name)
    return course_view(course, await course_stats(user.id, []))


@router.get("/courses/{id}")
async def get_course(id: str, user=Depends(plus_user)):
    course = await find_course(user.id, id)
    settings = await ensure_os_profile(user)
    tz, now = settings.timezone, _now()
    stats = await course_stats(user.id, [id])
    assessments = await db.assessment.find_many(where={"userId": user.id, "courseId": id}, order=_ASSESSMENT_ORDER)
    sessions = await db.studysession.find_many(where={"userId": user.id, "courseId": id}, order={"startAt": "desc"}, take=30)
    class_events = await db.event.find_many(where={"userId": user.id, "workspaceId": None, "courseId": id, "type": "CLASS"}, include={"course": True},
                                            order={"startAt": "asc"})
    tasks = await db.task.find_many(where={"userId": user.id, "workspaceId": None, "courseId": id}, order=[{"status": "asc"}, {"dueAt": "asc"}], take=100)
    notes = await db.note.find_many(where={"userId": user.id, "workspaceId": None, "courseId": id, "isArchived": False}, order={"updatedAt": "desc"}, take=50)
    lang = to_lang(settings.language)
    classes = []
    for ev in class_events:
        if ev.rrule:
            nxt = rrule.next_occurrence(ev.rrule, ev.startAt, now, tz=tz, until=ev.recurrenceEndAt, inclusive=True)
        else:
            nxt = ev.startAt if ev.startAt >= now else None
        classes.append({**{f: getattr(ev, f) for f in _CLASS_FIELDS}, "course": course_brief(ev.course),
                        "exDatesCount": len(ev.exDates or []), "recurrenceText": rrule.describe(ev.rrule, lang) if ev.rrule else None,
                        "nextOccurrence": nxt})
    start_of_today = dates.start_of_day(now, tz)
    return {
        **course_view(course, stats),
        "assessments": [assessment_view(a, now, with_course=False) for a in assessments],
        "sessions": [session_view(s, with_course=False) for s in sessions],
        "classes": classes,
        "tasks": [task_view(t, now, start_of_today) for t in tasks],
        "notesList": [{"id": n.id, "title": n.title, "updatedAt": n.updatedAt, "isPinned": n.isPinned} for n in notes],
    }


@router.patch("/courses/{id}")
async def update_course(id: str, body: CourseUpdateBody, user=Depends(plus_user)):
    existing = await find_course(user.id, id)
    if body.sent("name") and body.name is None:
        raise invalid_data()  # Prisma refused null on the required column
    data: dict = {}
    for field in ("name", "code", "instructor", "term", "color", "location", "notes"):
        if body.sent(field):
            data[field] = getattr(body, field)
    for field in ("credits", "targetGrade"):
        if body.sent(field):
            value = getattr(body, field)
            data[field] = None if value is None else as_decimal(value)
    if body.isArchived is not None:
        data["isArchived"] = body.isArchived
    course = await db.course.update(where={"id": existing.id}, data=data)
    if body.isArchived is not None and body.isArchived != existing.isArchived:
        await log_activity(user.id, "course.archived" if body.isArchived else "course.unarchived", entity_id=id, name=course.name)
    else:
        await log_activity(user.id, "course.updated", entity_id=id, name=course.name)
    return course_view(course, await course_stats(user.id, [id]))


@router.delete("/courses/{id}")
async def remove_course(id: str, user=Depends(plus_user)):
    """Deletes the course, its assessments and its weekly CLASS events; sessions, tasks and
    notes are kept, unlinked."""
    existing = await find_course(user.id, id)
    async with db.tx() as tx:
        await tx.event.delete_many(where={"userId": user.id, "workspaceId": None, "courseId": id, "type": "CLASS"})
        await tx.course.delete(where={"id": existing.id})
    await log_activity(user.id, "course.deleted", entity_id=id, name=existing.name)
    return {"ok": True}


# ----- assessments ---------------------------------------------------------------------


async def _find_assessment(user_id: str, id: str):
    row = await db.assessment.find_first(where={"id": id, "userId": user_id}, include={"course": True})
    if row is None:
        raise nest_error(404, "Assessment not found")
    return row


@router.get("/assessments")
async def list_assessments(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("courseId", "type", "status", "from", "to", "upcoming", "overdue", "limit"))
    types = _query_list(request, "type", ASSESSMENT_TYPES, errors)
    statuses = _query_list(request, "status", ASSESSMENT_STATUSES, errors)
    start = _query_date(request, "from", errors)
    end = _query_date(request, "to", errors)
    upcoming = _query_bool(request, "upcoming", errors)
    overdue = _query_bool(request, "overdue", errors)
    limit = _query_int(request, "limit", errors, maximum=500, minimum=1)
    if errors:
        raise nest_error(400, errors)
    now = _now()
    where: dict = {"userId": user.id}
    both: list = []
    if request.query_params.get("courseId"):
        where["courseId"] = request.query_params["courseId"]
    if types:
        where["type"] = {"in": types}
    if statuses:
        where["status"] = {"in": statuses}
    if start or end:
        both.append({"dueAt": {**({"gte": dates.parse_instant(start)} if start else {}), **({"lte": dates.parse_instant(end)} if end else {})}})
    if upcoming:
        both.append({"dueAt": {"gte": now}, "status": {"in": list(OPEN_STATUSES)}})
    if overdue:
        both.append({"dueAt": {"lt": now}, "status": {"in": list(OPEN_STATUSES)}})
    if both:
        where["AND"] = both
    rows = await db.assessment.find_many(where=where, include={"course": True}, order=_ASSESSMENT_ORDER, take=limit or 500)
    return {"items": [assessment_view(a, now) for a in rows], "total": await db.assessment.count(where=where)}


@router.post("/assessments", status_code=201)
async def create_assessment(body: AssessmentBody, user=Depends(plus_user)):
    await assert_owned("course", body.courseId, user.id)
    has_grade = body.grade is not None
    a = await db.assessment.create(data={
        "userId": user.id,
        "courseId": body.courseId,
        "title": body.title,
        "type": body.type or "ASSIGNMENT",
        "status": body.status or ("GRADED" if has_grade else "NOT_STARTED"),
        "dueAt": _instant(body.dueAt),
        "location": body.location,
        "grade": None if body.grade is None else as_decimal(body.grade),
        "maxGrade": as_decimal(body.maxGrade) if body.maxGrade is not None else (as_decimal(100) if has_grade else None),
        "weight": None if body.weight is None else as_decimal(body.weight),
        "notes": body.notes,
    }, include={"course": True})
    await log_activity(user.id, "assessment.created", entity_id=a.id, title=a.title, type=a.type, courseId=a.courseId)
    return assessment_view(a, _now())


@router.get("/assessments/{id}")
async def get_assessment(id: str, user=Depends(plus_user)):
    return assessment_view(await _find_assessment(user.id, id), _now())


@router.patch("/assessments/{id}")
async def update_assessment(id: str, body: AssessmentUpdateBody, user=Depends(plus_user)):
    existing = await _find_assessment(user.id, id)
    if body.sent("courseId") and body.courseId is None:
        raise nest_error(400, "courseId cannot be empty")
    if body.courseId and body.courseId != existing.courseId:
        await assert_owned("course", body.courseId, user.id)
    grade_set = body.grade is not None
    data: dict = {}
    for field in ("courseId", "title", "type"):
        if getattr(body, field) is not None:
            data[field] = getattr(body, field)
    status = body.status or ("GRADED" if grade_set and existing.status != "GRADED" else None)
    if status:
        data["status"] = status
    if body.sent("dueAt"):
        data["dueAt"] = _instant(body.dueAt)
    for field in ("location", "notes"):
        if body.sent(field):
            data[field] = getattr(body, field)
    for field in ("grade", "weight"):
        if body.sent(field):
            value = getattr(body, field)
            data[field] = None if value is None else as_decimal(value)
    if not body.sent("maxGrade") and grade_set and existing.maxGrade is None:
        data["maxGrade"] = as_decimal(100)
    elif body.sent("maxGrade"):
        data["maxGrade"] = None if body.maxGrade is None else as_decimal(body.maxGrade)
    a = await db.assessment.update(where={"id": existing.id}, data=data, include={"course": True})
    graded = grade_set and (existing.grade is None or float(existing.grade) != body.grade)
    if graded:
        await log_activity(user.id, "assessment.graded", entity_id=id, title=a.title, courseId=a.courseId, grade=body.grade,
                           maxGrade=num(a.maxGrade))
    else:
        await log_activity(user.id, "assessment.updated", entity_id=id, title=a.title, courseId=a.courseId)
    return assessment_view(a, _now())


@router.delete("/assessments/{id}")
async def remove_assessment(id: str, user=Depends(plus_user)):
    existing = await _find_assessment(user.id, id)
    await db.assessment.delete(where={"id": existing.id})
    await log_activity(user.id, "assessment.deleted", entity_id=id, title=existing.title)
    return {"ok": True}


# ----- study sessions ------------------------------------------------------------------


async def _find_session(user_id: str, id: str):
    row = await db.studysession.find_first(where={"id": id, "userId": user_id}, include={"course": True})
    if row is None:
        raise nest_error(404, "Study session not found")
    return row


@router.get("/sessions")
async def list_sessions(request: Request, user=Depends(plus_user)):
    errors = unknown_params(request, ("courseId", "from", "to", "limit"))
    start = _query_date(request, "from", errors)
    end = _query_date(request, "to", errors)
    limit = _query_int(request, "limit", errors, maximum=500, minimum=1)
    if errors:
        raise nest_error(400, errors)
    where: dict = {"userId": user.id}
    if request.query_params.get("courseId"):
        where["courseId"] = request.query_params["courseId"]
    if start or end:
        where["startAt"] = {**({"gte": dates.parse_instant(start)} if start else {}), **({"lte": dates.parse_instant(end)} if end else {})}
    rows = await db.studysession.find_many(where=where, include={"course": True}, order={"startAt": "desc"}, take=limit or 200)
    total = await db.studysession.count(where=where)
    summed = await db.studysession.group_by(by=["userId"], where=where, sum={"durationMinutes": True})
    minutes = sum((g.get("_sum") or {}).get("durationMinutes") or 0 for g in summed)
    return {"items": [session_view(s) for s in rows], "total": total, "totalMinutes": minutes}


@router.post("/sessions", status_code=201)
async def create_session(body: SessionBody, user=Depends(plus_user)):
    await assert_owned("course", body.courseId, user.id)
    start = _instant(body.startAt) or _now() - timedelta(minutes=body.durationMinutes)
    s = await db.studysession.create(data={
        "userId": user.id, "courseId": body.courseId, "startAt": start, "durationMinutes": body.durationMinutes,
        "topic": body.topic, "notes": body.notes,
    }, include={"course": True})
    # the activity's entity is the course (studySession.* → COURSE)
    await log_activity(user.id, "studySession.created", entity_id=s.courseId, sessionId=s.id, durationMinutes=s.durationMinutes)
    return session_view(s)


@router.patch("/sessions/{id}")
async def update_session(id: str, body: SessionUpdateBody, user=Depends(plus_user)):
    existing = await _find_session(user.id, id)
    if body.courseId:
        await assert_owned("course", body.courseId, user.id)
    data: dict = {}
    for field in ("courseId", "topic", "notes"):
        if body.sent(field):
            data[field] = getattr(body, field)
    if body.startAt:
        data["startAt"] = _instant(body.startAt)
    if body.durationMinutes is not None:
        data["durationMinutes"] = body.durationMinutes
    s = await db.studysession.update(where={"id": existing.id}, data=data, include={"course": True})
    await log_activity(user.id, "studySession.updated", entity_id=s.courseId, sessionId=id, durationMinutes=s.durationMinutes)
    return session_view(s)


@router.delete("/sessions/{id}")
async def remove_session(id: str, user=Depends(plus_user)):
    existing = await _find_session(user.id, id)
    await db.studysession.delete(where={"id": existing.id})
    await log_activity(user.id, "studySession.deleted", entity_id=existing.courseId, sessionId=id)
    return {"ok": True}
