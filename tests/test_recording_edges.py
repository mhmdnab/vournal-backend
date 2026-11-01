"""Recordings at the edges: silence, and stopping right at the plan's limit.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.quota import reject_if_recording_too_long


class RecordingLimit(unittest.TestCase):
    def _check(self, seconds):
        with patch("app.quota.max_recording_seconds", return_value=90):
            reject_if_recording_too_long(seconds, SimpleNamespace())

    def test_stopping_just_past_the_limit_is_accepted(self):
        """The phone stops within a moment of the limit, and rounds the length."""
        for seconds in (90, 91, 92):
            self._check(seconds)

    def test_a_longer_recording_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self._check(93)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(caught.exception.detail["maxSeconds"], 90)


class SilentRecording(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db

        self.db = db
        await db.connect()
        self.user = await db.user.create(data={"email": f"silent-{uuid.uuid4().hex}@example.test"})

    async def asyncTearDown(self):
        await self.db.user.delete(where={"id": self.user.id})
        await self.db.disconnect()

    async def _run(self, text):
        from app import main

        entry = await self.db.entry.create(
            data={"userId": self.user.id, "rawTranscript": "", "audioUrl": "local.m4a", "status": "PENDING"}
        )
        extract = AsyncMock()
        with (
            patch.object(main, "resolve_audio_path", AsyncMock(return_value="/nonexistent/audio.m4a")),
            patch.object(main, "transcribe", AsyncMock(return_value={"text": text, "language": None, "durationSec": 6.0})),
            patch.object(main, "process_transcript", extract),
        ):
            await main.process_audio_entry(entry.id)
        return await self.db.entry.find_unique(where={"id": entry.id}), extract

    async def test_no_words_fails_kindly_and_spends_no_ai(self):
        for text in ("", "   "):
            with patch.object(__import__("app.main", fromlist=["main"]), "delete_audio", AsyncMock(return_value=True)):
                entry, extract = await self._run(text)
            self.assertEqual(entry.status, "FAILED")
            self.assertIn("didn’t catch any words", entry.error)
            extract.assert_not_awaited()

    async def test_silence_costs_nothing_and_leaves_nothing_behind(self):
        from app import main

        await self.db.user.update(where={"id": self.user.id}, data={"usagePeriodEntries": 5})
        with patch.object(main, "delete_audio", AsyncMock(return_value=True)) as delete:
            entry, _ = await self._run("")
        delete.assert_awaited_once_with("local.m4a")
        self.assertIsNone(entry.audioUrl)
        user = await self.db.user.find_unique(where={"id": self.user.id})
        self.assertEqual(user.usagePeriodEntries, 4)

    async def test_the_refund_never_goes_below_zero(self):
        from app import main

        await self.db.user.update(where={"id": self.user.id}, data={"usagePeriodEntries": 0})
        with patch.object(main, "delete_audio", AsyncMock(return_value=True)):
            await self._run("")
        user = await self.db.user.find_unique(where={"id": self.user.id})
        self.assertEqual(user.usagePeriodEntries, 0)

    async def test_the_journal_hides_silence_but_keeps_real_failures(self):
        from app import main

        with patch.object(main, "delete_audio", AsyncMock(return_value=True)):
            silent, _ = await self._run("")
        real = await self.db.entry.create(
            data={"userId": self.user.id, "rawTranscript": "", "audioUrl": "kept.m4a", "status": "FAILED"}
        )
        words = await self.db.entry.create(
            data={"userId": self.user.id, "rawTranscript": "Call the bank", "status": "DONE"}
        )
        listed = await main.list_entries(limit=20, current=self.user)
        ids = {entry.id for entry in listed}
        self.assertNotIn(silent.id, ids)
        self.assertLessEqual({real.id, words.id}, ids)
        # The screen waiting on it can still read why.
        self.assertEqual((await main.get_entry(silent.id, current=self.user)).status, "FAILED")

    async def test_words_go_on_to_extraction(self):
        _, extract = await self._run("Call the bank")
        extract.assert_awaited_once()
