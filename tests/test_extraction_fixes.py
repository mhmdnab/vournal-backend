"""Fixes from the AI-accuracy review: unreadable replies, crashes on odd values,
excitement and evidence from other sentences, Arabic/iOS spelling, invented dates.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import unittest
from datetime import timezone
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


if __name__ == "__main__":
    unittest.main()
