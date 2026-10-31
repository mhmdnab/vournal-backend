"""Fixes from the AI-accuracy review: unreadable replies, crashes on odd values,
excitement and evidence from other sentences, Arabic/iOS spelling, invented dates.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import unittest
from datetime import date, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from app.task_intelligence import _in_transcript, normalize_metadata, verified_day


def _replies(*pairs):
    responses = [
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=reason,
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )
        for reason, text in pairs
    ]
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=responses)
    return MagicMock(return_value=client)


FULL = '{"summary": "ok", "mood": {"score": 3}, "todos": []}'


class Replies(unittest.IsolatedAsyncioTestCase):
    async def test_unreadable_reply_fails_instead_of_saving_empty(self):
        from app import extraction, extraction_merged

        for module, call in ((extraction, extraction.extract), (extraction_merged, extraction_merged.extract_merged)):
            with patch.object(module, "AsyncAnthropic", _replies(("end_turn", "Sorry, here you go: not json"))):
                with self.assertRaises(extraction.ExtractionMalformed, msg=module.__name__):
                    await call("I need to call the bank tomorrow.")

    async def test_cut_off_reply_retries_once_with_more_room(self):
        from app import extraction, extraction_merged

        for module, call in ((extraction, extraction.extract), (extraction_merged, extraction_merged.extract_merged)):
            factory = _replies(("max_tokens", '{"summary": "ha'), ("end_turn", FULL))
            with patch.object(module, "AsyncAnthropic", factory):
                result, _ = await call("A very long entry.")
            self.assertEqual(result["summary"], "ok")
            calls = factory.return_value.messages.create.call_args_list
            self.assertEqual(calls[1].kwargs["max_tokens"], extraction.MAX_TOKENS_RETRY)

    def test_failures_have_plain_messages(self):
        from app.extraction import ExtractionMalformed, ExtractionRefused, ExtractionTruncated

        for exc in (ExtractionMalformed(), ExtractionRefused(), ExtractionTruncated()):
            self.assertNotIn("max_tokens", exc.user_message)
            self.assertTrue(exc.user_message.endswith("."))


class Guards(unittest.TestCase):
    def test_odd_value_types_fall_back_to_defaults(self):
        result = normalize_metadata(
            {"text": "Call", "category": ["work"], "kind": ["event"], "location": ["x"],
             "recurrence": {"freq": ["weekly"], "weekdays": "MO"}, "urgency": [3]},
            "Call the office.",
        )
        self.assertEqual((result["urgency"], result["recurrence"], result["kind"]), (0, None, "task"))

    def test_excitement_marks_are_not_evidence(self):
        transcript = "What a great day!!! Call mom"
        result = normalize_metadata(
            {"text": "Call mom", "sourceText": "Call mom", "urgency": 3, "urgencyEvidence": "!!!"},
            transcript,
        )
        self.assertEqual((result["urgency"], result["urgencyUnclear"]), (0, False))

    def test_evidence_from_another_sentence_asks(self):
        transcript = "Pay the rent. Call the plumber, it's urgent."
        result = normalize_metadata(
            {"text": "Pay the rent", "sourceText": "Pay the rent.", "urgency": 3, "urgencyEvidence": "it's urgent"},
            transcript,
        )
        self.assertTrue(result["urgencyUnclear"])

    def test_unclear_string_asks_and_null_is_none(self):
        self.assertTrue(normalize_metadata({"text": "Pay rent", "urgency": "unclear"}, "Pay rent soon.")["urgencyUnclear"])
        result = normalize_metadata({"text": "Buy milk", "urgency": None}, "Buy milk.")
        self.assertEqual((result["urgency"], result["urgencyUnclear"]), (0, False))

    def test_arabic_and_ios_spelling_variants_match(self):
        for phrase, transcript in (
            ("الإيجار", "لازم ادفع الايجار"),
            ("مدرسة", "من المدرسه"),
            ("على", "علي البحر"),
            ("it's urgent", "Call the bank, it’s urgent"),
            ("الساعة ٣", "الساعة 3"),
        ):
            self.assertTrue(_in_transcript(phrase, transcript), phrase)

    def test_until_is_local_and_after_the_first_day(self):
        tz = ZoneInfo("America/New_York")
        item = {"text": "Pay", "dueDate": "2026-11-01",
                "recurrence": {"freq": "monthly", "interval": 1, "weekdays": None, "until": "2027-03-31"}}
        until = normalize_metadata(item, "Pay every month until March.", tz)["recurrenceUntil"]
        self.assertEqual(until.astimezone(tz).strftime("%Y-%m-%d %H:%M"), "2027-03-31 23:59")
        item["recurrence"]["until"] = "2026-10-01"
        self.assertIsNone(normalize_metadata(item, "Pay every month.", tz)["recurrenceUntil"])

    def test_invented_day_is_dropped(self):
        self.assertEqual(
            verified_day({"dueDate": "2026-10-28", "time": "10:00", "dueDateEvidence": None}, "Call the bank !!!"),
            (None, None),
        )
        self.assertEqual(
            verified_day({"dueDate": "2026-10-29", "time": None, "dueDateEvidence": "tomorrow"}, "Call the bank tomorrow"),
            ("2026-10-29", None),
        )
        # No evidence key at all (older replies): keep what the model said.
        self.assertEqual(verified_day({"dueDate": "2026-10-29", "time": "09:00"}, "x"), ("2026-10-29", "09:00"))


class HeldOutFixes(unittest.TestCase):
    """Misses from the held-out evaluation (h1)."""

    TODAY = date(2026, 10, 31)  # a Saturday

    def _day(self, due, evidence, transcript, today=TODAY):
        return verified_day({"dueDate": due, "time": "10:00", "dueDateEvidence": evidence}, transcript, today)[0]

    def test_named_weekday_corrects_the_date(self):
        self.assertEqual(self._day("2026-11-03", "on Monday", "pick up the dry cleaning on Monday"), "2026-11-02")
        self.assertEqual(self._day("2026-11-04", "يوم الإثنين", "استلم الملابس يوم الإثنين"), "2026-11-02")
        self.assertEqual(self._day("2026-11-03", "nhar el tnen", "jib el tyeb nhar el tnen"), "2026-11-02")
        self.assertEqual(self._day("2026-11-06", "el khamis", "el khamis el jeye"), "2026-11-05")
        self.assertEqual(self._day("2026-11-03", "الإثنين المقبل", "الإثنين المقبل"), "2026-11-02")
        # The model's week is kept ("next Monday" a week out stays a week out)...
        self.assertEqual(self._day("2026-11-10", "next Monday", "next Monday"), "2026-11-09")
        # ...but never moved into the past.
        self.assertEqual(self._day("2026-11-01", "Friday", "on Friday"), "2026-11-06")

    def test_right_or_ambiguous_weekdays_are_left_alone(self):
        self.assertEqual(self._day("2026-11-02", "on Monday", "on Monday"), "2026-11-02")
        self.assertEqual(self._day("2026-11-01", "before Monday", "before Monday"), "2026-11-01")
        self.assertEqual(self._day("2026-11-03", "Monday or Tuesday", "Monday or Tuesday"), "2026-11-03")
        # "tnen" is also "two" and "jem3a" also "a week": only a day after nhar/yom.
        self.assertEqual(self._day("2026-11-07", "ba3d jem3a", "ba3d jem3a"), "2026-11-07")
        self.assertEqual(self._day("2026-11-01", "el tnen", "el tnen"), "2026-11-01")
        self.assertEqual(verified_day({"dueDate": "soon", "dueDateEvidence": "Monday"}, "Monday", self.TODAY), ("soon", None))

    def test_two_named_days_leave_the_date_alone(self):
        """و attaches to the next word, and tnen/jem3a only count after nhar/yom,
        so a second day could go unseen and a correct date got moved."""
        for due, evidence in (
            ("2026-11-05", "يوم الإثنين والخميس"),
            ("2026-11-06", "الثلاثاء والجمعة"),
            ("2026-11-08", "السبت والأحد"),
            ("2026-11-02", "el tnen w el khamis"),
            ("2026-11-06", "el jem3a w el sabt"),
            ("2026-11-06", "الجمعة أو السبت"),
            ("2026-11-05", "يوم إثنين وخميس"),
        ):
            self.assertEqual(self._day(due, evidence, evidence), due, evidence)

    def test_a_date_on_one_of_the_repeat_days_stays(self):
        """"كل يوم إثنين وخميس" said on a Wednesday: Thursday is the first session."""
        item = {"dueDate": "2026-10-29", "time": None, "dueDateEvidence": "يوم إثنين",
                "recurrence": {"freq": "weekly", "interval": 1, "weekdays": ["MO", "TH"], "until": None}}
        self.assertEqual(verified_day(item, "النادي كل يوم إثنين وخميس", date(2026, 10, 28))[0], "2026-10-29")

    def _repeat_day(self, due, evidence, transcript, today, days=("MO", "TH")):
        item = {"dueDate": due, "time": "18:00", "dueDateEvidence": evidence,
                "recurrence": {"freq": "weekly", "interval": 1, "weekdays": list(days), "until": None}}
        return verified_day(item, transcript, today)

    def test_a_repeat_said_without_a_start_begins_on_its_next_day(self):
        """Seen on a phone: "every Monday and Thursday" saved on Tuesday 3 November."""
        said = "Yoga every Monday and Thursday at 6pm"
        sunday, tuesday = date(2026, 11, 1), date(2026, 11, 3)
        self.assertEqual(self._repeat_day("2026-11-03", "every Monday and Thursday", said, sunday),
                         ("2026-11-02", "18:00"))
        self.assertEqual(self._repeat_day("2026-11-09", "every Monday and Thursday", said, tuesday)[0], "2026-11-05")
        self.assertEqual(self._repeat_day("2026-11-03", "كل يوم إثنين وخميس", "النادي كل يوم إثنين وخميس", sunday)[0],
                         "2026-11-02")
        self.assertEqual(self._repeat_day("2026-11-03", "kel tnen w khamis", "el gym kel tnen w khamis", sunday)[0],
                         "2026-11-02")

    def test_a_repeat_date_off_its_days_moves_to_the_nearest_one(self):
        said = "Yoga every Monday and Thursday from next week"
        today = date(2026, 11, 1)
        self.assertEqual(self._repeat_day("2026-11-10", "next week", said, today)[0], "2026-11-09")
        # Never before today.
        self.assertEqual(self._repeat_day("2026-11-03", "every Monday", "Yoga every Monday", date(2026, 11, 3), ("MO",))[0],
                         "2026-11-09")

    def test_a_chosen_start_on_a_repeat_day_stays(self):
        self.assertEqual(self._repeat_day("2026-11-05", "starting Thursday", "Yoga Monday and Thursday starting Thursday",
                                          date(2026, 11, 1))[0], "2026-11-05")
        self.assertEqual(self._repeat_day("2026-11-09", "next week", "Yoga every Monday and Thursday from next week",
                                          date(2026, 11, 1))[0], "2026-11-09")

    def test_only_weekly_repeats_with_named_days_are_moved(self):
        item = {"dueDate": "2026-11-03", "time": None, "dueDateEvidence": "tuesday",
                "recurrence": {"freq": "monthly", "interval": 1, "weekdays": [], "until": None}}
        self.assertEqual(verified_day(item, "rent on tuesday", date(2026, 11, 1))[0], "2026-11-03")

    def test_urgency_words_anywhere_in_the_tasks_sentence(self):
        for transcript, source, evidence in (
            ("This can't wait — I have to fix the car brakes before Monday.",
             "I have to fix the car brakes before Monday.", "This can't wait"),
            ("هذا الأمر لا يحتمل التأجيل، يجب أن أصلح فرامل السيارة قبل يوم الإثنين.",
             "يجب أن أصلح فرامل السيارة قبل يوم الإثنين", "هذا الأمر لا يحتمل التأجيل"),
        ):
            result = normalize_metadata(
                {"text": "Fix the brakes", "sourceText": source, "urgency": 3, "urgencyEvidence": evidence},
                transcript,
            )
            self.assertEqual((result["urgency"], result["urgencyUnclear"]), (3, False), transcript)

    def test_lowercase_speech_still_keeps_urgency_in_its_sentence(self):
        transcript = "bukra lezem e7ke el bank, mesta3jel. nhar el tnen 3ande interview"
        result = normalize_metadata(
            {"text": "Interview", "sourceText": "nhar el tnen 3ande interview", "urgency": 3,
             "urgencyEvidence": "mesta3jel"},
            transcript,
        )
        self.assertTrue(result["urgencyUnclear"])

    def test_unclear_repeat_as_an_object_still_asks(self):
        """Haiku sometimes nests it: {"freq": "unclear", ...} instead of "unclear"."""
        result = normalize_metadata(
            {"text": "Water the plants",
             "recurrence": {"freq": "unclear", "interval": None, "weekdays": None, "until": None}},
            "I want to water the plants every so often.",
        )
        self.assertEqual((result["recurrence"], result["recurrenceUnclear"]), (None, True))

    def test_the_models_kind_wins_over_keywords(self):
        booking = {"text": "Book dentist appointment for Sami", "kind": "task"}
        self.assertEqual(normalize_metadata(booking, "Need to book the dentist for Sami.")["kind"], "task")
        self.assertEqual(normalize_metadata({"text": "حجز موعد", "kind": "task"}, "احجز موعد")["kind"], "task")
        # Keywords only fill in when the model gave no usable kind.
        self.assertEqual(normalize_metadata({"text": "Dentist appointment"}, "Dentist appointment")["kind"], "event")


if __name__ == "__main__":
    unittest.main()
