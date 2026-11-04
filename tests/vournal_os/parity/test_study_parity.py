"""Study answers like NestJS did: courses with weighted and plain grades, assessments
(grading moves the status), study sessions and their totals, and the overview with this
week's minutes and today's weekly classes, in a Beirut-time account."""

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from vournal_os.parity.harness import ParityCase

BEIRUT = ZoneInfo("Asia/Beirut")
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StudyParity(ParityCase):
    maxDiff = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.assertParity(*await self.both("PATCH", "/settings", {"timezone": "Asia/Beirut"}))

    async def pair(self, method, paths, body=None, *, status=None):
        """Same request to both; `paths` and `body` may be (nest, python) pairs. Returns both bodies."""
        out = []
        for i, (client, prefix) in enumerate(((self.nest, ""), (self.py, "/api"))):
            p = paths if isinstance(paths, str) else paths[i]
            out.append(await client.request(method, f"{prefix}{p}", json=body[i] if isinstance(body, tuple) else body))
        self.assertParity(*out)
        if status is not None:
            self.assertEqual(out[0].status_code, status, out[0].text)
        if out[0].status_code == 400 and isinstance(out[0].json().get("message"), list):
            self.assertEqual(out[0].json()["message"], out[1].json()["message"])  # word for word
        return tuple(r.json() if r.content else None for r in out)

    async def make(self, path, body):
        nest, py = await self.pair("POST", path, body, status=201)
        return nest["id"], py["id"]

    @staticmethod
    def each(ids, build):
        return tuple(build(i) for i in ids)

    async def test_courses_assessments_and_sessions(self):
        now = datetime.now(timezone.utc)
        maths = await self.make("/study/courses", {"name": " Calculus ", "code": "MATH201", "credits": 3, "targetGrade": 85, "color": "#6366f1",
                                                   "instructor": "Dr. Saad", "term": "Fall 2026", "location": "Room 4"})
        history = await self.make("/study/courses", {"name": "History", "credits": 2.5})
        old = await self.make("/study/courses", {"name": "Archived course", "isArchived": True})
        graded = [
            {"title": "Midterm", "type": "EXAM", "grade": 78, "maxGrade": 90, "weight": 30, "dueAt": iso(now - timedelta(days=10))},
            {"title": "Quiz 1", "type": "QUIZ", "grade": 9.5, "maxGrade": 10, "weight": 10},
            {"title": "HW1", "grade": 100},
            {"title": "Final", "type": "EXAM", "weight": 60, "dueAt": iso(now + timedelta(days=12))},
            {"title": "Essay", "type": "PROJECT", "status": "IN_PROGRESS", "dueAt": iso(now - timedelta(days=1))},
            {"title": "Lab", "type": "LAB", "status": "SUBMITTED", "dueAt": iso(now - timedelta(days=2))},
        ]
        made = [await self.make("/study/assessments", self.each(maths, lambda c, b=b: {**b, "courseId": c})) for b in graded]
        await self.make("/study/assessments", self.each(history, lambda c: {"courseId": c, "title": "Paper", "grade": 72}))
        await self.make("/study/assessments", self.each(old, lambda c: {"courseId": c, "title": "Old", "dueAt": iso(now + timedelta(days=3))}))
        for query in ("", "?includeArchived=true", "?archived=true", "?archived=maybe", "?bogus=1"):
            with self.subTest(query=query):
                await self.pair("GET", f"/study/courses{query}")
        await self.pair("GET", self.each(maths, lambda c: f"/study/courses/{c}"))
        for query in ("", "?type=EXAM,QUIZ", "?status=NOT_STARTED&status=IN_PROGRESS", "?upcoming=true", "?overdue=true",
                      "?limit=2", "?type=ESSAY", "?limit=501", f"?from={iso(now)}", f"?to={iso(now)}", "?from=soon"):
            with self.subTest(query=query):
                await self.pair("GET", f"/study/assessments{query}")
        await self.pair("GET", self.each(maths, lambda c: f"/study/assessments?courseId={c}"))
        final = self.each(made[3], lambda a: f"/study/assessments/{a}")
        await self.pair("PATCH", final, {"grade": 81})  # grading marks it GRADED, out of 100
        await self.pair("PATCH", final, {"grade": 81})
        await self.pair("PATCH", final, {"grade": 70, "maxGrade": 80, "status": "SUBMITTED"})
        await self.pair("PATCH", final, {"courseId": None})
        await self.pair("PATCH", final, self.each(history, lambda c: {"courseId": c, "dueAt": None, "location": "Hall B"}))
        await self.pair("PATCH", final, {"courseId": "cnosuchcoursexxxxxxxxxxxx"})
        await self.pair("GET", final)
        await self.pair("DELETE", self.each(made[5], lambda a: f"/study/assessments/{a}"))
        await self.pair("GET", "/study/assessments/cnosuchassessmentxxxxxxx")

        week_start = now - timedelta(minutes=90)
        await self.make("/study/sessions", self.each(maths, lambda c: {"courseId": c, "durationMinutes": 45, "startAt": iso(week_start), "topic": " Limits "}))
        await self.make("/study/sessions", self.each(history, lambda c: {"courseId": c, "durationMinutes": 30, "startAt": iso(now - timedelta(minutes=40))}))
        await self.make("/study/sessions", self.each(old, lambda c: {"courseId": c, "durationMinutes": 20, "startAt": iso(now - timedelta(minutes=30))}))
        general = await self.make("/study/sessions", {"durationMinutes": 15, "startAt": iso(now - timedelta(minutes=20)), "notes": "flashcards"})
        await self.make("/study/sessions", {"durationMinutes": 60, "startAt": "2026-01-05T10:00:00.000Z"})
        for query in ("", "?limit=1", f"?from={iso(now - timedelta(days=1))}", "?to=2026-02-01", "?limit=0", "?bogus=1"):
            with self.subTest(query=query):
                await self.pair("GET", f"/study/sessions{query}")
        await self.pair("GET", self.each(maths, lambda c: f"/study/sessions?courseId={c}"))
        session = self.each(general, lambda s: f"/study/sessions/{s}")
        await self.pair("PATCH", session, self.each(maths, lambda c: {"courseId": c, "durationMinutes": 25}))
        await self.pair("PATCH", session, {"courseId": None, "topic": None})
        await self.pair("DELETE", session)
        await self.pair("PATCH", session, {"durationMinutes": 5})

        # a weekly class today, and a one-off class later today
        local = now.astimezone(BEIRUT)
        start = (local - timedelta(minutes=30)).replace(second=0, microsecond=0)
        await self.make("/events", self.each(maths, lambda c: {"title": "Lecture", "type": "CLASS", "courseId": c, "startAt": iso(start - timedelta(days=7)),
                                                              "endAt": iso(start - timedelta(days=7) + timedelta(hours=1)), "rrule": f"FREQ=WEEKLY;BYDAY={DAYS[local.weekday()]}"}))
        await self.make("/events", self.each(history, lambda c: {"title": "Seminar", "type": "CLASS", "courseId": c, "startAt": iso(now + timedelta(minutes=5)),
                                                                "location": "Library"}))
        await self.pair("GET", "/study/overview")
        await self.pair("GET", self.each(maths, lambda c: f"/study/courses/{c}"))
        self.assertParity(*await self.both("PATCH", "/settings", {"language": "ar"}))
        await self.pair("GET", "/study/overview")

        await self.pair("PATCH", self.each(history, lambda c: f"/study/courses/{c}"), {"isArchived": True})
        await self.pair("PATCH", self.each(history, lambda c: f"/study/courses/{c}"), {"code": None, "credits": 4.5})
        await self.pair("GET", "/study/overview")
        await self.pair("DELETE", self.each(maths, lambda c: f"/study/courses/{c}"))
        await self.pair("GET", "/study/sessions")
        await self.pair("GET", "/events")
        await self.pair("GET", self.each(maths, lambda c: f"/study/courses/{c}"))

    async def test_validation(self):
        for body in ({}, {"name": ""}, {"name": "x" * 121}, {"name": "A", "credits": 1.25}, {"name": "A", "credits": 1000},
                     {"name": "A", "targetGrade": 101}, {"name": "A", "color": "blue"}, {"name": "A", "code": "x" * 31},
                     {"name": "A", "isArchived": "yes"}, {"name": "A", "bogus": 1}):
            with self.subTest(course=body):
                await self.pair("POST", "/study/courses", body)
        course = await self.make("/study/courses", {"name": "Physics"})
        for body in ({}, {"title": "x"}, {"courseId": "", "title": "x"}, {"courseId": "cnosuchcoursexxxxxxxxxxxx", "title": "x"},
                     {"title": "x", "type": "ESSAY"}, {"title": "x", "grade": -1}, {"title": "x", "maxGrade": 0}, {"title": "x", "weight": 100.5},
                     {"title": "x", "grade": 1.234}, {"title": "x", "dueAt": "soon"}, {"title": "x", "status": "DONE"}):
            with self.subTest(assessment=body):
                await self.pair("POST", "/study/assessments", self.each(course, lambda c, b=body: {"courseId": c, **b} if "courseId" not in b else b))
        for body in ({}, {"durationMinutes": 0}, {"durationMinutes": 1441}, {"durationMinutes": 1.5}, {"durationMinutes": 10, "startAt": "x"},
                     {"durationMinutes": 10, "courseId": "cnosuchcoursexxxxxxxxxxxx"}, {"durationMinutes": 10, "topic": "x" * 201}):
            with self.subTest(session=body):
                await self.pair("POST", "/study/sessions", body)
        await self.pair("PATCH", "/study/courses/cnosuchcoursexxxxxxxxxxxx", {"name": "x"})


if __name__ == "__main__":
    unittest.main()
