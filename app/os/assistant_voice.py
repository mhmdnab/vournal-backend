"""/api/assistant/transcribe: the chat box's microphone (design 2026-11-06). A recording becomes
text that the dashboard puts in the box for the user to read and send; it never calls the AI and
never spends the AI allowance. Up to 2 minutes and 25 MB, 60 minutes a month (AI_VOICE_MONTHLY_MINUTES),
stopped by the app's kill switch like journal recordings. The audio is deleted as soon as it's
heard: our temporary file, and the speech provider's copy."""

import logging
import math
import os
import tempfile

from fastapi import Depends, File, HTTPException, UploadFile
from prisma import Json

from app import quota, transcription
from app.db import db
from app.os import ai
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.profile import ensure_os_profile

log = logging.getLogger("vournal.os")
router = nest_router("/assistant")

MAX_SECONDS = 120
_SLACK_SECONDS = 5  # a recording stopped at 2:00 can come out a little longer


def _extension(data: bytes) -> str:
    """The file type the bytes are (the provider reads it from the name)."""
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3" or data[0] == 0xFF:
        return "mp3"
    return "webm"


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), user=Depends(plus_user)):
    ai.throttle("assistant-voice", user.id)
    s = await ensure_os_profile(user)
    if not s.aiEnabled:
        raise nest_error(403, "Turn on AI in Settings first.")
    data = await audio.read(transcription.MAX_BYTES + 1)
    if len(data) > transcription.MAX_BYTES:
        raise nest_error(413, "That recording is too long.")
    if not transcription.looks_like_audio(data):
        raise nest_error(400, "That doesn't look like a recording.")
    try:
        await quota.check_global_daily_cap(db)
    except HTTPException as exc:
        raise nest_error(503, "Voice is paused right now. Please type instead.") from exc
    if await ai.voice_seconds_left(user.id, s.timezone) <= 0:
        raise nest_error(403, "You've used this month's voice minutes. Typing still works.")

    fd, path = tempfile.mkstemp(suffix=f".{_extension(data)}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        probed = transcription._probe_duration(path)
        if probed and probed > MAX_SECONDS + _SLACK_SECONDS:
            raise nest_error(400, "Recordings can be up to 2 minutes.")
        try:
            heard = await transcription.transcribe(path, delete_job=True)
        except Exception as exc:  # noqa: BLE001 — the provider's failure; typing still works
            log.warning("Chat voice transcription failed: %r", exc)
            raise nest_error(502, "Couldn't turn that into text. Please try again.") from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    seconds = math.ceil(heard.get("durationSec") or probed or 0)
    await db.activitylog.create(data={"userId": user.id, "action": ai.VOICE, "metadata": Json({"seconds": seconds})})
    if seconds > MAX_SECONDS + _SLACK_SECONDS:
        raise nest_error(400, "Recordings can be up to 2 minutes.")
    return {"text": (heard.get("text") or "").strip(), "voiceSecondsLeftThisMonth": await ai.voice_seconds_left(user.id, s.timezone)}
