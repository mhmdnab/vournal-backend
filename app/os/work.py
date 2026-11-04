"""/api/work/overview (port of vournalOS WorkService): a read-only view of the WORK life
area. Work tasks grouped (overdue, today, upcoming, no date), meetings over the next 14
days with repeats expanded, work projects with progress, deadlines over the next 30 days,
and the week's stats. Creating happens through /tasks, /events and /projects."""

from datetime import datetime, timedelta, timezone

from fastapi import Depends

from app.db import db
from app.os.common import dates
from app.os.common.deps import plus_user
from app.os.common.http import nest_router
from app.os.common.i18n import fmt, to_lang
from app.os.insight_text import js_round
from app.os.profile import ensure_os_profile
from app.os.study import events_overlapping, expand_events

router = nest_router("/work")

_TASK_FIELDS = ("id", "title", "description", "status", "priority", "lifeArea", "dueAt", "hasDueTime", "estimatedMinutes", "completedAt",
                "parentId", "projectId")
_MEETING_FIELDS = ("id", "title", "description", "location", "type", "priority", "color", "startAt", "endAt", "allDay", "rrule",
                   "recurrenceEndAt", "reminderMinutes", "projectId")
_PROJECT_FIELDS = ("id", "name", "description", "status", "lifeArea", "color", "icon", "startDate", "deadline", "progressOverride",
                   "completedAt", "updatedAt")
_PERCENT_COMPLETE = {"en": "{percent}% complete", "ar": "مكتمل بنسبة {percent}%"}
_PRIORITY_RANK = {"URGENT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_MEETING_MINUTES = 60  # assumed length of a meeting without an end, for "meeting hours"
_INFINITY = float("inf")


def _project_brief(project) -> dict | None:
    return {"id": project.id, "name": project.name, "color": project.color} if project else None


def _rank(priority: str) -> int:
    return _PRIORITY_RANK.get(priority, 9)


def _ms(moment: datetime | None) -> float:
    return moment.timestamp() * 1000 if moment else _INFINITY


@router.get("/overview")
async def overview(user=Depends(plus_user)):
    s = await ensure_os_profile(user)
    tz, lang = s.timezone, to_lang(s.language)
    now = datetime.now(timezone.utc)
    today = dates.range("day", now, tz)
    week = dates.range("week", now, tz, s.weekStartsOn)
    meetings_to = dates.end_of_day(dates.add_days(now, 13, tz), tz)  # today and the next 13 days
    deadlines_to = dates.end_of_day(dates.add_days(now, 30, tz), tz)
    today_date_only = dates.today_date_only(tz)

    # a task belongs to work when its life area is WORK or it sits in a WORK project
    work_task = {"userId": user.id, "OR": [{"lifeArea": "WORK"}, {"project": {"is": {"lifeArea": "WORK"}}}]}
    open_rows = await db.task.find_many(where={**work_task, "status": {"not": "COMPLETED"}}, include={"project": True},
                                        order=[{"dueAt": {"sort": "asc", "nulls": "last"}}, {"createdAt": "desc"}], take=300)
    completed_this_week = await db.task.count(where={**work_task, "status": "COMPLETED", "completedAt": {"gte": week["from"], "lte": week["to"]}})
    meeting_rows = await db.event.find_many(where={"userId": user.id, "type": "MEETING", **events_overlapping(today["from"], meetings_to)},
                                            include={"project": True})
    week_meeting_rows = await db.event.find_many(where={"userId": user.id, "type": "MEETING", **events_overlapping(week["from"], week["to"])},
                                                 include={"project": True})
    projects = await db.project.find_many(where={"userId": user.id, "lifeArea": "WORK", "status": {"not_in": ["CANCELLED"]}},
                                          order=[{"deadline": {"sort": "asc", "nulls": "last"}}, {"updatedAt": "desc"}])

    # ----- tasks grouped -----
    def overdue(t) -> bool:
        return bool(t.dueAt) and (t.dueAt < now if t.hasDueTime else t.dueAt < today["from"])

    decorated = []
    for t in open_rows:
        late = overdue(t)
        decorated.append({**{f: getattr(t, f) for f in _TASK_FIELDS}, "project": _project_brief(t.project), "createdAt": t.createdAt,
                          "isOverdue": late, "isToday": bool(t.dueAt) and not late and today["from"] <= t.dueAt <= today["to"]})
    decorated.sort(key=lambda t: (_ms(t["dueAt"]), _rank(t["priority"])))  # stable, as Array.prototype.sort
    tasks = {
        "overdue": [t for t in decorated if t["isOverdue"]],
        "today": [t for t in decorated if t["isToday"]],
        "upcoming": [t for t in decorated if t["dueAt"] and not t["isOverdue"] and not t["isToday"]],
        "noDate": sorted((t for t in decorated if not t["dueAt"]), key=lambda t: _rank(t["priority"])),
    }

    # ----- meetings (next 14 days, repeats expanded) -----
    def meeting_view(ev) -> dict:
        return {**{f: getattr(ev, f) for f in _MEETING_FIELDS}, "project": _project_brief(ev.project)}

    meetings = []
    for m in expand_events(meeting_rows, today["from"], meetings_to, tz, meeting_view):
        end = m["occurrenceEnd"] or m["occurrenceStart"] + timedelta(minutes=_MEETING_MINUTES)
        meetings.append({**m, "date": dates.local_date_string(m["occurrenceStart"], tz),
                         "isToday": today["from"] <= m["occurrenceStart"] <= today["to"],
                         "isNow": m["occurrenceStart"] <= now <= end, "isPast": end < now})
    week_meetings = expand_events(week_meeting_rows, week["from"], week["to"], tz, meeting_view)
    meeting_minutes = 0.0
    for m in week_meetings:
        if m["allDay"]:
            continue
        minutes = (m["occurrenceEnd"] - m["occurrenceStart"]) / timedelta(minutes=1) if m["occurrenceEnd"] else _MEETING_MINUTES
        meeting_minutes += max(0, minutes)

    # ----- projects with progress (top-level tasks only) -----
    ids = [p.id for p in projects]
    counted = await db.task.find_many(where={"userId": user.id, "projectId": {"in": ids}, "parentId": None}) if ids else []
    views = []
    for p in projects:
        own = [t for t in counted if t.projectId == p.id]
        total = len(own)
        completed = sum(1 for t in own if t.status == "COMPLETED")
        late = sum(1 for t in own if t.status != "COMPLETED" and t.dueAt and t.dueAt < now)
        auto = js_round(completed / total * 100) if total else 0
        views.append({**{f: getattr(p, f) for f in _PROJECT_FIELDS},
                      "progress": 100 if p.status == "COMPLETED" else (p.progressOverride if p.progressOverride is not None else auto),
                      "taskCounts": {"total": total, "completed": completed, "open": total - completed, "overdue": late},
                      "daysUntilDeadline": dates.days_until(p.deadline, tz) if p.deadline else None})
    active = [p for p in views if p["status"] != "COMPLETED"]

    # ----- deadlines (tasks and projects, next 30 days; overdue ones first) -----
    last_day = dates.add_days_date_only(today_date_only, 30)
    deadlines = [{
        "kind": "task", "id": t["id"], "title": t["title"], "date": t["dueAt"], "hasTime": t["hasDueTime"],
        "daysUntil": dates.days_until(dates.to_date_only(t["dueAt"], tz), tz), "isOverdue": t["isOverdue"], "priority": t["priority"],
        "color": (t["project"] or {}).get("color"), "subtitle": (t["project"] or {}).get("name"), "link": f"/tasks?taskId={t['id']}",
    } for t in decorated if t["dueAt"] and t["dueAt"] <= deadlines_to and t["parentId"] is None]
    deadlines += [{
        "kind": "project", "id": p["id"], "title": p["name"], "date": p["deadline"], "hasTime": False, "daysUntil": p["daysUntilDeadline"],
        "isOverdue": p["daysUntilDeadline"] < 0, "priority": None, "color": p["color"],
        "subtitle": fmt(_PERCENT_COMPLETE[lang], percent=p["progress"]), "link": f"/projects/{p['id']}",
    } for p in active if p["deadline"] and p["deadline"] <= last_day]
    deadlines.sort(key=lambda d: (d["daysUntil"], _ms(d["date"])))

    return {
        "generatedAt": now,
        "week": {"from": week["from"], "to": week["to"]},
        "tasks": tasks,
        "meetings": meetings,
        "projects": active,
        "completedProjects": [p for p in views if p["status"] == "COMPLETED"][:10],
        "deadlines": deadlines,
        "stats": {
            "openTasks": len(decorated),
            "overdueCount": len(tasks["overdue"]),
            "todayCount": len(tasks["today"]),
            "completedThisWeek": completed_this_week,
            "meetingsToday": sum(1 for m in meetings if m["isToday"]),
            "meetingsThisWeek": len(week_meetings),
            "meetingHoursThisWeek": js_round(meeting_minutes / 60 * 10) / 10,
            "activeProjects": len(active),
        },
    }
