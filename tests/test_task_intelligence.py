import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.task_intelligence import normalize_metadata, questions


def _norm(item, transcript):
    return normalize_metadata({"text": "Task", **item}, transcript)


class TaskRules(unittest.TestCase):
    """The AI decides; these guards only catch clear errors (falling back to
    "unknown", which may ask, never to a guess)."""

    def test_model_urgency_kept_when_evidence_is_real(self):
        transcript = "I really really need to call the bank before Friday."
        result = _norm(
            {"text": "Call the bank", "urgency": 3, "urgencyEvidence": "really really need to"},
            transcript,
        )
        self.assertEqual((result["urgency"], result["urgencyUnclear"]), (3, False))

    def test_markers_only_from_the_task_clause(self):
        transcript = "Call dentist !!. What a great day!!!"
        dentist = _norm({"text": "Call dentist", "sourceText": "Call dentist !!"}, transcript)
        self.assertEqual(dentist["urgency"], 2)
        other = _norm({"text": "Buy milk", "sourceText": "Buy milk", "urgency": 0}, transcript + " Buy milk")
        self.assertEqual(other["urgency"], 0)
        # Typed markers beat the model's own judgement for that task.
        typed = _norm({"text": "Pay rent", "sourceText": "Pay rent !", "urgency": 3,
                       "urgencyEvidence": "Pay rent"}, "Pay rent !")
        self.assertEqual(typed["urgency"], 1)

    def test_conflicting_markers_ask(self):
        result = _norm({"text": "Call dentist", "sourceText": "Call dentist ! !!"}, "Call dentist ! !!")
        self.assertEqual((result["urgency"], result["urgencyUnclear"]), (0, True))

    def test_unverifiable_urgency_becomes_a_question(self):
        result = _norm({"urgency": 3, "urgencyEvidence": "absolutely critical"}, "I should call the bank.")
        self.assertEqual((result["urgency"], result["urgencyUnclear"]), (0, True))

    def test_signalled_but_unclear_urgency_asks(self):
        result = _norm({"urgency": "unclear"}, "Pay rent soon, it's important.")
        self.assertTrue(result["urgencyUnclear"])

    def test_no_urgency_said_is_zero_without_a_question(self):
        result = _norm({"urgency": 0}, "Buy milk.")
        self.assertEqual((result["urgency"], result["urgencyUnclear"]), (0, False))

    def test_arabic_evidence_matches_without_diacritics(self):
        transcript = "لازم ادفع الإيجار ضروري جداً"
        result = _norm({"urgency": 3, "urgencyEvidence": "ضروري جدا"}, transcript)
        self.assertEqual(result["urgency"], 3)

    def test_location_not_in_transcript_is_dropped(self):
        self.assertIsNone(_norm({"location": "Paris"}, "Meeting with Rami tomorrow.")["location"])

    def test_location_in_arabic_kept(self):
        result = _norm({"location": "مكتب الحمرا"}, "عندي اجتماع بمكتب الحمرا بكرا")
        self.assertEqual(result["location"], "مكتب الحمرا")

    def test_weekly_with_weekdays(self):
        result = _norm(
            {"recurrence": {"freq": "weekly", "interval": 1, "weekdays": ["MO", "TH"], "until": None}},
            "Gym every Monday and Thursday.",
        )
        self.assertEqual(result["recurrence"], "weekly")
        self.assertEqual(result["recurrenceWeekdays"], ["MO", "TH"])
        self.assertFalse(result["recurrenceUnclear"])

    def test_unclear_repeat_asks(self):
        result = _norm({"recurrence": "unclear"}, "Water the plants every so often.")
        self.assertIsNone(result["recurrence"])
        self.assertTrue(result["recurrenceUnclear"])

    def test_repeat_until_and_interval(self):
        result = _norm(
            {"recurrence": {"freq": "monthly", "interval": 2, "weekdays": None, "until": "2027-06-30"}},
            "Pay the bill every two months until June.",
        )
        self.assertEqual((result["recurrence"], result["recurrenceInterval"]), ("monthly", 2))
        self.assertEqual(result["recurrenceUntil"].date().isoformat(), "2027-06-30")

    def test_garbage_metadata_defaults(self):
        result = _norm(
            {"urgency": "high", "recurrence": {"freq": "hourly", "weekdays": ["XX"]}, "location": 7,
             "kind": "party"},
            "Something.",
        )
        self.assertEqual(result["urgency"], 0)
        self.assertIsNone(result["recurrence"])
        self.assertEqual(result["recurrenceWeekdays"], [])
        self.assertIsNone(result["location"])
        self.assertEqual(result["kind"], "task")

    def test_event_with_date_no_time_asks_time(self):
        task = SimpleNamespace(text="Meeting with Rami", kind="event", dueDate="2030-01-03",
                               startAt=None, urgencyUnclear=False, recurrence=None,
                               recurrenceUnclear=False, dismissedQuestions=[])
        self.assertEqual([q["field"] for q in questions(task)], ["time"])
        task.dismissedQuestions = ["time"]
        self.assertEqual(questions(task), [])

    def test_event_without_date_asks_date_then_time(self):
        task = SimpleNamespace(text="Dentist appointment", kind="event", dueDate=None,
                               startAt=None, urgencyUnclear=False, recurrence=None,
                               recurrenceUnclear=False, dismissedQuestions=[])
        self.assertEqual([q["field"] for q in questions(task)], ["date", "time"])

    def test_daily_or_weekday_repeat_needs_no_start_day(self):
        for recurrence, weekdays in (("daily", []), ("weekly", ["MO", "TH"])):
            task = SimpleNamespace(text="Gym", kind="task", dueDate=None, startAt=None,
                                   urgencyUnclear=False, recurrence=recurrence,
                                   recurrenceWeekdays=weekdays, recurrenceUnclear=False,
                                   dismissedQuestions=[])
            self.assertEqual(questions(task), [], recurrence)
        monthly = SimpleNamespace(text="Pay the bill", kind="task", dueDate=None, startAt=None,
                                  urgencyUnclear=False, recurrence="monthly",
                                  recurrenceWeekdays=[], recurrenceUnclear=False,
                                  dismissedQuestions=[])
        self.assertEqual([q["field"] for q in questions(monthly)], ["date"])

    def test_recurring_event_on_named_days_asks_only_missing_time(self):
        task = SimpleNamespace(text="Team sync", kind="event", dueDate=None, startAt=None,
                               urgencyUnclear=False, recurrence="weekly",
                               recurrenceWeekdays=["TU"], recurrenceUnclear=False,
                               dismissedQuestions=[])
        self.assertEqual([q["field"] for q in questions(task)], ["time"])

    def test_plain_task_without_date_asks_nothing(self):
        task = SimpleNamespace(text="Buy milk", kind="task", dueDate=None, startAt=None,
                               urgencyUnclear=False, recurrence=None, recurrenceUnclear=False,
                               dismissedQuestions=[])
        self.assertEqual(questions(task), [])


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
                "Authorization": f"Bearer {security.create_access_token(user.id)}"
            }

            async def extract(transcript, **kwargs):
                return (
                    _normalize(
                        {
                            "summary": transcript,
                            "mood": {"score": 3, "label": "neutral", "emotions": []},
                            "todos": [
                                {
                                    "text": "Meeting",
                                    "sourceText": transcript,
                                    "dueDate": "2030-01-02",
                                    "dueDateEvidence": "tomorrow",
                                    "time": None,
                                    "kind": "event",
                                    "category": "work",
                                    "urgency": 0,
                                    "location": None,
                                    "recurrence": None,
                                }
                            ],
                            "topics": [],
                            "people": [],
                            "places": [],
                        }
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
                    "Authorization": f"Bearer {security.create_access_token('missing-user')}"
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
                    "Authorization": f"Bearer {security.create_access_token(other.id)}"
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
                weekly = await client.patch(
                    f"/todos/{task['id']}/details",
                    json={"recurrence": "weekly", "recurrenceWeekdays": ["MO", "TH"]},
                )
                self.assertEqual(weekly.json()["recurrenceWeekdays"], ["MO", "TH"])
                monthly = await client.patch(
                    f"/todos/{task['id']}/details", json={"recurrence": "monthly"}
                )
                self.assertEqual(monthly.json()["recurrenceWeekdays"], [])
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
