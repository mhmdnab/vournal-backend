import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.task_intelligence import local_extract, normalize_metadata, questions


class TaskRules(unittest.TestCase):
    def test_priority_stays_with_its_own_sentence(self):
        transcript = (
            "Call dentist !!!; Pay rent !!; Buy milk !; I had a wonderful day!!!"
        )
        for label, expected in (("Call dentist", 3), ("Pay rent", 2), ("Buy milk", 1)):
            self.assertEqual(
                normalize_metadata({"text": label}, transcript)["urgency"], expected
            )
        self.assertEqual(
            normalize_metadata({"text": "Study"}, transcript)["urgency"], 0
        )

    def test_ambiguous_priority_and_no_invented_metadata(self):
        result = normalize_metadata(
            {
                "text": "Call dentist",
                "sourceText": "fictional",
                "location": "Paris",
                "recurrence": "daily",
            },
            "Call dentist ! !!",
        )
        self.assertTrue(result["urgencyUnclear"])
        self.assertEqual(result["urgency"], 0)
        self.assertIsNone(result["location"])
        self.assertIsNone(result["recurrence"])
        self.assertEqual(result["category"], "health")
        self.assertFalse(result["calendarRequested"])
        for source in (
            "Do not add call dentist to Apple Calendar",
            "Don't add call dentist to Apple Reminders",
        ):
            result = normalize_metadata(
                {"text": "call dentist", "sourceText": source}, source
            )
            self.assertFalse(result["calendarRequested"])
            self.assertFalse(result["remindersRequested"])
        transcript = "Call dentist !!!; Buy milk"
        self.assertEqual(
            normalize_metadata(
                {"text": "Buy milk", "sourceText": transcript}, transcript
            )["urgency"],
            0,
        )

    def test_contextual_questions_and_dismissal(self):
        task = SimpleNamespace(
            text="Meeting with Rami",
            kind="event",
            dueDate="2030-01-03",
            startAt=None,
            urgencyUnclear=True,
            recurrence=None,
            dismissedQuestions=[],
        )
        self.assertEqual([q["field"] for q in questions(task)], ["time", "urgency"])
        task.dismissedQuestions = ["time", "urgency"]
        self.assertEqual(questions(task), [])

    def test_calendar_request_needs_a_date_and_time_even_for_a_task(self):
        task = SimpleNamespace(
            text="Call dentist",
            kind="task",
            dueDate=None,
            startAt=None,
            urgencyUnclear=False,
            recurrence=None,
            calendarRequested=True,
        )
        self.assertEqual([q["field"] for q in questions(task)], ["date", "time"])

    def test_offline_fixture_keeps_missing_time_and_explicit_repeat(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        result = local_extract(
            "I have a meeting tomorrow !!; Add call doctor every 2 weeks at 2pm tomorrow to Apple Calendar",
            now,
        )
        self.assertIsNone(result["todos"][0]["time"])
        self.assertEqual(result["todos"][0]["dueDate"], "2030-01-02")
        self.assertEqual(result["todos"][1]["recurrence"], "weekly")
        self.assertEqual(result["todos"][1]["recurrenceInterval"], 2)
        self.assertEqual(result["todos"][1]["time"], "14:00")
        source = "Add meeting at Beirut Office on 2030-01-02 every week to Apple Calendar !!!"
        self.assertEqual(
            local_extract(source, now)["todos"][0]["location"], "Beirut Office"
        )


class TasksIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_entry_followup_correction_and_ownership(self):
        from app.db import db
        from app.main import app
        from app import security
        from app.extraction import _normalize
        import httpx
        import uuid

        await db.connect()
        account = f"tasks-{uuid.uuid4().hex}@example.test"
        user = await db.user.create(data={"email": account, "timezone": "Asia/Beirut"})
        other = await db.user.create(data={"email": "other-" + account})
        try:
            headers = {
                "Authorization": f"Bearer {security.create_access_token(user.id, True)}"
            }

            async def extract(transcript, **kwargs):
                return (
                    _normalize(
                        local_extract(
                            transcript, datetime(2030, 1, 1, tzinfo=timezone.utc)
                        )
                    ),
                    None,
                )

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=headers,
            ) as client:
                with patch("app.main.extract", extract):
                    response = await client.post(
                        "/entries", json={"transcript": "I have a meeting tomorrow !!"}
                    )
                self.assertEqual(response.status_code, 200, response.text)
                entry = response.json()
                task = entry["todos"][0]
                self.assertEqual(task["urgency"], 2)
                self.assertEqual(task["category"], "work")
                self.assertEqual(task["followUps"][0]["field"], "time")
                self.assertIsNone(task["startAt"])
                response = await client.patch(
                    f"/todos/{task['id']}/details",
                    json={
                        "time": "14:30",
                        "location": "Beirut office",
                        "locationLatitude": 33.8938,
                        "locationLongitude": 35.5018,
                        "recurrence": "weekly",
                        "category": "work",
                        "calendarRequested": True,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["startAt"], "2030-01-02T12:30:00Z")
                self.assertEqual(response.json()["followUps"], [])
                self.assertEqual(response.json()["locationLatitude"], 33.8938)
                self.assertEqual(response.json()["locationLongitude"], 35.5018)
                saved_entry = (await client.get(f"/entries/{entry['id']}")).json()
                self.assertEqual(saved_entry["todos"][0]["locationLatitude"], 33.8938)
                self.assertEqual(
                    (await client.get(f"/entries/{entry['id']}")).json()[
                        "rawTranscript"
                    ],
                    "I have a meeting tomorrow !!",
                )
                for bad in (
                    {"time": "25:30"},
                    {"date": "2030-02-31"},
                    {"urgency": 4},
                    {"recurrenceInterval": 0},
                    {"locationLatitude": 33.89},
                    {"locationLatitude": None, "locationLongitude": 35.5},
                    {"locationLatitude": 91, "locationLongitude": 35.5},
                    {"locationLatitude": 33.8, "locationLongitude": -181},
                ):
                    self.assertEqual(
                        (
                            await client.patch(f"/todos/{task['id']}/details", json=bad)
                        ).status_code,
                        422,
                    )
                wrong_user_headers = {
                    "Authorization": f"Bearer {security.create_access_token('missing-user', True)}"
                }
                self.assertEqual(
                    (
                        await client.patch(
                            f"/todos/{task['id']}/details",
                            json={"urgency": 3},
                            headers=wrong_user_headers,
                        )
                    ).status_code,
                    401,
                )
                other_headers = {
                    "Authorization": f"Bearer {security.create_access_token(other.id, True)}"
                }
                self.assertEqual(
                    (
                        await client.patch(
                            f"/todos/{task['id']}/details",
                            json={"urgency": 3},
                            headers=other_headers,
                        )
                    ).status_code,
                    404,
                )
                todos = (await client.get("/todos")).json()
                self.assertEqual(todos[0]["location"], "Beirut office")
                self.assertEqual(todos[0]["locationLongitude"], 35.5018)
                cleared = await client.patch(
                    f"/todos/{task['id']}/details",
                    json={
                        "location": None,
                        "locationLatitude": None,
                        "locationLongitude": None,
                    },
                )
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertIsNone(cleared.json()["locationLatitude"])
                self.assertIsNone(cleared.json()["locationLongitude"])
                self.assertEqual(
                    (
                        await client.patch(f"/todos/{task['id']}", json={"done": True})
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    (await client.delete(f"/todos/{task['id']}")).status_code, 204
                )
        finally:
            await db.user.delete(where={"id": user.id})
            await db.user.delete(where={"id": other.id})
            await db.disconnect()
