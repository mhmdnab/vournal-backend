"""The assistant's study tools (design 2026-11-18): courses, assessments (with grades) and study
sessions."""

from vournal_os.assistant_life_base import LifeBase


class StudyTools(LifeBase):
    async def course(self, name="Statistics"):
        return await self.post("/api/study/courses", {"name": name})

    async def test_add_course_and_undo(self):
        a = await self.act("add_study_item", {"kind": "course", "title": "Statistics", "code": "STA101", "term": "Fall 2026"})
        self.assertEqual((a["kind"], a["entityType"], a["label"], a["link"]), ("create", "course", "Statistics", f"/study?course={a['entityId']}"))
        c = await self.db.course.find_unique(where={"id": a["entityId"]})
        self.assertEqual((c.name, c.code, c.term), ("Statistics", "STA101", "Fall 2026"))
        await self.undo(a)
        self.assertIsNone(await self.db.course.find_unique(where={"id": a["entityId"]}))

    async def test_add_assessment_with_local_due_and_weight(self):
        c = await self.course()
        a = await self.act("add_study_item", {"kind": "assessment", "course_id": c["id"], "title": "Midterm", "type": "EXAM",
                                              "due": "2026-12-03T10:00", "weight": 40})
        self.assertEqual((a["entityType"], a["link"]), ("assessment", f"/study?assessment={a['entityId']}"))
        row = await self.db.assessment.find_unique(where={"id": a["entityId"]})
        self.assertEqual((row.type, row.dueAt.isoformat()[:16], float(row.weight), row.status), ("EXAM", "2026-12-03T08:00", 40.0, "NOT_STARTED"))

    async def test_assessment_due_date_only_is_end_of_that_local_day(self):
        c = await self.course()
        a = await self.act("add_study_item", {"kind": "assessment", "course_id": c["id"], "title": "Essay", "due": "2026-12-03"})
        row = await self.db.assessment.find_unique(where={"id": a["entityId"]})
        self.assertEqual(row.dueAt.isoformat()[:16], "2026-12-03T21:59")  # 23:59 Beirut

    async def test_add_assessment_needs_course_id(self):
        self.assertIn("course_id", await self.act_error("add_study_item", {"kind": "assessment", "title": "Quiz"}))

    async def test_add_session_minutes(self):
        c = await self.course()
        a = await self.act("add_study_item", {"kind": "session", "minutes": 45, "course_id": c["id"], "topic": "Chapter 2"})
        self.assertEqual((a["kind"], a["entityType"], a["link"]), ("create", "study_session", "/study"))
        s = await self.db.studysession.find_unique(where={"id": a["entityId"]})
        self.assertEqual((s.durationMinutes, s.topic, s.courseId), (45, "Chapter 2", c["id"]))
        await self.undo(a)
        self.assertIsNone(await self.db.studysession.find_unique(where={"id": a["entityId"]}))

    async def test_add_session_needs_minutes(self):
        self.assertIn("minutes", await self.act_error("add_study_item", {"kind": "session", "topic": "X"}))

    async def test_record_grade_sets_graded_and_undo(self):
        c = await self.course()
        x = await self.post("/api/study/assessments", {"courseId": c["id"], "title": "Quiz 1"})
        a = await self.act("update_study_item", {"kind": "assessment", "id": x["id"], "grade": 17, "max_grade": 20})
        self.assertEqual((a["kind"], a["entityType"]), ("update", "assessment"))
        row = await self.db.assessment.find_unique(where={"id": x["id"]})
        self.assertEqual((float(row.grade), float(row.maxGrade), row.status), (17.0, 20.0, "GRADED"))
        await self.undo(a)
        row = await self.db.assessment.find_unique(where={"id": x["id"]})
        self.assertEqual((row.grade, row.maxGrade, row.status), (None, None, "NOT_STARTED"))

    async def test_archive_course_and_undo(self):
        c = await self.course()
        a = await self.act("update_study_item", {"kind": "course", "id": c["id"], "archived": True})
        self.assertTrue((await self.db.course.find_unique(where={"id": c["id"]})).isArchived)
        await self.undo(a)
        self.assertFalse((await self.db.course.find_unique(where={"id": c["id"]})).isArchived)

    async def test_field_of_another_kind_is_an_error(self):
        c = await self.course()
        self.assertIn('"grade"', await self.act_error("update_study_item", {"kind": "course", "id": c["id"], "grade": 10}))
        self.assertIn('"minutes"', await self.act_error("add_study_item", {"kind": "course", "title": "X", "minutes": 5}))

    async def test_delete_course_with_assessments_asks(self):
        c = await self.course()
        await self.post("/api/study/assessments", {"courseId": c["id"], "title": "Final"})
        a = await self.delete("course", c["id"], "Statistics")
        self.assertEqual(a["status"], "pending")
        done = await self.approve(a)
        self.assertIsNone(await self.db.course.find_unique(where={"id": c["id"]}))
        await self.undo(done)
        self.assertEqual(await self.db.assessment.count(where={"courseId": c["id"]}), 1)

    async def test_delete_assessment(self):
        c = await self.course()
        x = await self.post("/api/study/assessments", {"courseId": c["id"], "title": "Quiz"})
        a = await self.delete("assessment", x["id"], "Quiz")
        self.assertEqual(a["status"], "done")
        self.assertIsNone(await self.db.assessment.find_unique(where={"id": x["id"]}))

    async def test_study_section_off_refuses(self):
        await self.client.patch("/api/settings", json={"enabledSections": ["tasks"]})
        text = await self.act_error("add_study_item", {"kind": "course", "title": "X"})
        self.assertEqual(text, "Error: The Study section is turned off in Settings.")


class StudyCards(LifeBase):
    async def test_manual_card_names_the_kind_it_adds(self):
        from vournal_os.parity import fake_model as fm

        await self.post("/api/settings", {"assistantMode": "manual"}, method="patch")
        c = await self.post("/api/study/courses", {"name": "Statistics"})
        t = await self.post("/api/trips", {"name": "Paris", "startDate": "2026-12-12"})
        fm.script(fm.tools(("add_study_item", {"kind": "assessment", "course_id": c["id"], "title": "Quiz"}),
                           ("add_study_item", {"kind": "session", "minutes": 30}),
                           ("add_trip_item", {"trip_id": t["id"], "list": "packing", "title": "Socks"})), fm.message(fm.text("Approve below.")))
        actions = (await self.say("add them"))["messages"][1]["actions"]
        self.assertEqual([(a["status"], a["entityType"]) for a in actions],
                         [("pending", "assessment"), ("pending", "study_session"), ("pending", "packing_item")])
