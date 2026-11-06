"""Voice in the chat box (design 2026-11-06): a recording becomes text for the box, never sent by
itself and never spending the AI allowance. Up to 2 minutes, 60 minutes a month, 25 MB; it obeys
the whole-app kill switch, and the provider's copy is deleted. No real speech provider is called."""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from prisma import Json

from app import transcription
from vournal_os.support import OsTestCase

WEBM = b"\x1a\x45\xdf\xa3" + b"\0" * 200


def heard(text="add milk", seconds=4.2, seen=None):
    async def fake(path, *args, **kwargs):
        if seen is not None:
            seen.append((path, kwargs))
        return {"text": text, "language": "en", "durationSec": seconds}

    return fake


class Voice(OsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = await self.signed_in()
        await self.client.patch("/api/settings", json={"aiEnabled": True})

    async def send(self, data=WEBM, name="clip.webm"):
        return await self.client.post("/api/assistant/transcribe", files={"audio": (name, data, "audio/webm")})

    async def test_transcribes_and_counts_seconds(self):
        seen = []
        with patch.object(transcription, "transcribe", heard(seen=seen)):
            response = await self.send()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"text": "add milk", "voiceSecondsLeftThisMonth": 3595})
        self.assertTrue(seen[0][1].get("delete_job"))
        self.assertEqual((await self.client.get("/api/ai/status")).json()["voiceSecondsLeftThisMonth"], 3595)

    async def test_never_spends_the_ai_allowance(self):
        before = (await self.client.get("/api/ai/status")).json()["remainingThisMonth"]
        with patch.object(transcription, "transcribe", heard()):
            await self.send()
        self.assertEqual((await self.client.get("/api/ai/status")).json()["remainingThisMonth"], before)

    async def test_requires_ai_on(self):
        await self.client.patch("/api/settings", json={"aiEnabled": False})
        response = await self.send()
        self.assertEqual((response.status_code, response.json()["message"]), (403, "Turn on AI in Settings first."))

    async def test_refuses_non_audio(self):
        response = await self.send(b"hello, this is not a recording at all")
        self.assertEqual((response.status_code, response.json()["message"]), (400, "That doesn't look like a recording."))

    async def test_refuses_over_25_mb(self):
        response = await self.send(WEBM + b"\0" * (25 * 1024 * 1024))
        self.assertEqual((response.status_code, response.json()["message"]), (413, "That recording is too long."))

    async def test_refuses_when_minutes_are_used_up(self):
        await self.db.activitylog.create(data={"userId": self.client.user.id, "action": "ai.voice", "metadata": Json({"seconds": 3600})})
        with patch.object(transcription, "transcribe", AsyncMock()) as fake:
            response = await self.send()
        self.assertEqual((response.status_code, response.json()["message"]), (403, "You've used this month's voice minutes. Typing still works."))
        fake.assert_not_called()

    async def test_kill_switch_stops_voice(self):
        busy = AsyncMock(side_effect=HTTPException(status_code=503, detail={"code": "SERVICE_BUSY"}))
        with patch("app.quota.check_global_daily_cap", busy), patch.object(transcription, "transcribe", AsyncMock()) as fake:
            response = await self.send()
        self.assertEqual((response.status_code, response.json()["message"]), (503, "Voice is paused right now. Please type instead."))
        fake.assert_not_called()

    async def test_refuses_over_two_minutes(self):
        with patch.object(transcription, "transcribe", heard(seconds=130)):
            response = await self.send()
        self.assertEqual((response.status_code, response.json()["message"]), (400, "Recordings can be up to 2 minutes."))

    async def test_empty_transcript_counts_and_returns_empty_text(self):
        with patch.object(transcription, "transcribe", heard(text="", seconds=3)):
            response = await self.send()
        self.assertEqual(response.json(), {"text": "", "voiceSecondsLeftThisMonth": 3597})

    async def test_temp_file_removed_even_on_failure(self):
        seen = []

        async def broken(path, *args, **kwargs):
            seen.append(path)
            raise RuntimeError("provider down")

        with patch.object(transcription, "transcribe", broken):
            response = await self.send()
        self.assertEqual((response.status_code, response.json()["message"]), (502, "Couldn't turn that into text. Please try again."))
        self.assertFalse(os.path.exists(seen[0]))
        self.assertEqual((await self.client.get("/api/ai/status")).json()["voiceSecondsLeftThisMonth"], 3600)  # nothing was heard


class _Response:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SpeechmaticsJobDeleted(unittest.IsolatedAsyncioTestCase):
    def client(self, calls, fail_fetch=False):
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, **kwargs):
                calls.append(("POST", url))
                return _Response(body={"id": "job1"})

            async def get(self, url, **kwargs):
                calls.append(("GET", url))
                if url.endswith("/jobs/job1"):
                    return _Response(body={"job": {"status": "done", "duration": 3}})
                return _Response(500) if fail_fetch else _Response(body={"results": []}, text="hello")

            async def delete(self, url, **kwargs):
                calls.append(("DELETE", url))
                return _Response()

        return Client

    async def run_job(self, delete_job, fail_fetch=False):
        calls = []
        fd, path = tempfile.mkstemp(suffix=".webm")
        os.write(fd, WEBM)
        os.close(fd)
        self.addCleanup(os.remove, path)
        with patch("httpx.AsyncClient", self.client(calls, fail_fetch)), patch("asyncio.sleep", AsyncMock()):
            try:
                await transcription._transcribe_speechmatics(path, "melia", "eu1", "key", delete_job=delete_job)
            except RuntimeError:
                pass
        return [c for c in calls if c[0] == "DELETE"]

    async def test_deleted_when_asked(self):
        self.assertEqual(await self.run_job(True), [("DELETE", "https://eu1.asr.api.speechmatics.com/v2/jobs/job1")])

    async def test_deleted_even_when_fetching_fails(self):
        self.assertEqual(len(await self.run_job(True, fail_fetch=True)), 1)

    async def test_kept_when_not_asked(self):
        self.assertEqual(await self.run_job(False), [])


if __name__ == "__main__":
    unittest.main()
