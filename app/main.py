"""FastAPI application entrypoint.

Routes for entries, todos, and (Phase 4) insights, on top of the Prisma client
lifecycle and a health check.
"""

import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import db as database
from app.auth import get_current_user, router as auth_router
from app.extraction import extract
from app.extraction_merged import extract_merged
from app.quota import (
    check_global_daily_cap,
    reject_if_recording_too_long,
    reserve_entry_quota,
)
from app.rate_limit import limiter
from app.storage import (
    create_signed_url,
    delete_audio,
    resolve_audio_path,
    save_audio,
)
from app.transcription import MAX_BYTES, SUPPORTED_FORMATS, looks_like_audio, transcribe
from app.schemas import (
    CreateEntryRequest,
    EntryOut,
    InsightsOut,
    Mood,
    MoodTrendPoint,
    NameCount,
    TodoListItem,
    TodoOut,
    UpdateTodoRequest,
)

# Load environment variables from a local .env file if present.
load_dotenv()

log = logging.getLogger("vournal")

db = database.db

# What to include when loading an Entry so we can assemble the full response.
_ENTRY_INCLUDE = {
    "todos": True,
    "topics": {"include": {"topic": True}},
    "people": {"include": {"person": True}},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect the Prisma client on startup, disconnect on shutdown."""
    await database.connect()
    # Reap orphaned jobs: extraction runs in in-process BackgroundTasks, which do
    # NOT survive a restart/deploy. Any entry still PENDING/PROCESSING at boot
    # belongs to a dead process — fail it so the user can retry, instead of it
    # hanging forever. (A real queue is the proper fix; this is the safety net.)
    try:
        reaped = await db.entry.update_many(
            where={"status": {"in": ["PENDING", "PROCESSING"]}},
            data={
                "status": "FAILED",
                "error": "Processing was interrupted (the server restarted). Please try again.",
                "processedAt": _now(),
            },
        )
        if reaped:
            log.warning("Reaped %d orphaned PENDING/PROCESSING entries on boot.", reaped)
    except Exception:  # noqa: BLE001 — never block startup on the reaper
        log.exception("Orphan reaper failed on boot")
    try:
        yield
    finally:
        await database.disconnect()


# API reference is OFF by default (don't expose the schema in prod). Enable in dev
# with ENABLE_DOCS=1.
_DOCS_ON = (os.environ.get("ENABLE_DOCS", "") or "").strip().lower() in {"1", "true", "yes"}
app = FastAPI(
    title="Vournal Backend",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)


# Cap request bodies. 30 MB covers a 25 MB audio upload plus multipart overhead;
# JSON routes are tiny, so this mainly bounds a hostile oversized body early.
_MAX_REQUEST_BYTES = 30 * 1024 * 1024


@app.middleware("http")
async def _limit_body_size(request, call_next):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413, content={"detail": "Request body too large."}
                )
        except ValueError:
            pass
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline hardening headers on every API response."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
    )
    return resp


# Rate limiting (Step 11): per-IP, in-memory. The default ceiling applies to every
# route via the middleware; auth routes add tighter per-route limits.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# The web frontend runs on a different origin, so the browser needs CORS. Origins
# come from CORS_ORIGINS (comma-separated) so the deployed web domain is set at
# deploy time without a code change; falls back to the dev localhost origins.
_CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
] or ["http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # includes Authorization, which the bearer token needs
)

# /auth/register, /login, /refresh, /logout, /me
app.include_router(auth_router)


@app.get("/health")
async def health():
    return {"ok": True}


# ---- Helpers -----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user_tz(tz_name: Optional[str]):
    """Resolve a stored IANA timezone name to a tzinfo, falling back to UTC for
    a missing or unrecognized value — a bad tz must never break ingestion."""
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse_due(value: Optional[str]) -> Optional[datetime]:
    """Parse an extracted dueDate string ("YYYY-MM-DD") into a datetime.

    Tolerates bad/None values by returning None rather than raising.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _compose_start_at(
    due: Optional[str], time_str: Optional[str], tz
) -> Optional[datetime]:
    """Combine an extracted dueDate ("YYYY-MM-DD") and time ("HH:MM") into a
    startAt UTC instant. Returns None unless BOTH are present and valid — an
    untimed todo (no spoken time) has no startAt.

    The extracted time is the speaker's LOCAL wall clock (the prompt is given
    their local now), so the day+time is interpreted in the user's timezone `tz`
    and converted to UTC. "2pm tomorrow" for an Asia/Beirut (UTC+3) user stores
    11:00Z. Rendering back to "2:00 PM" is the client's job.
    """
    day = _parse_due(due)
    if day is None or not time_str:
        return None
    try:
        hour, minute = (int(part) for part in time_str.split(":"))
        # dueDate only carries the calendar day; build the local wall-clock
        # datetime in the user's zone, then normalize to UTC for storage.
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        return local.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


async def _load_entry(entry_id: str):
    """Load one entry with all relations needed to assemble the response."""
    return await db.entry.find_unique(where={"id": entry_id}, include=_ENTRY_INCLUDE)


async def _owned_entry_or_404(entry_id: str, user_id: str):
    """Load an entry only if it belongs to this user; else 404.

    404 (not 403) on purpose: a user must not be able to tell whether someone
    else's entry id exists.
    """
    entry = await _load_entry(entry_id)
    if entry is None or entry.userId != user_id:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


def _to_entry_out(entry: Any) -> EntryOut:
    """Assemble an EntryOut from a Prisma entry (with relations included)."""
    topics = [et.topic.name for et in (entry.topics or []) if et.topic]
    people = [ep.person.name for ep in (entry.people or []) if ep.person]
    todos = [
        TodoOut(id=t.id, text=t.text, dueDate=t.dueDate, startAt=t.startAt, done=t.done)
        for t in (entry.todos or [])
    ]
    return EntryOut(
        id=entry.id,
        userId=entry.userId,
        source=entry.source,
        status=entry.status,
        summary=entry.summary,
        mood=Mood(
            score=entry.moodScore if entry.moodScore is not None else 3,
            label=entry.moodLabel,
            emotions=entry.emotions or [],
        ),
        todos=todos,
        topics=topics,
        people=people,
        audioUrl=entry.audioUrl,
        durationSec=entry.durationSec,
        rawTranscript=entry.rawTranscript,
        processedAt=entry.processedAt,
        error=entry.error,
        createdAt=entry.createdAt,
    )


# ---- Shared processing pipeline ----------------------------------------------


async def process_transcript(
    entry_id: str, transcript: str, use_merged: bool = False
) -> None:
    """Run extraction on a transcript and persist the results onto an entry.

    Shared by the text endpoint (synchronous) and the audio background task.
    Sets the entry to DONE on success, or FAILED + stored error on any failure,
    so an entry is never left stuck mid-processing. Idempotent enough for our
    needs: it's called once per entry.

    `use_merged` selects the extraction path (Step 10):
      - False (text): plain extraction on Haiku. Typed words have no speech-to-text
        errors, so there's nothing to repair — keep it cheap.
      - True (audio): the MERGED path — one Sonnet call that silently corrects
        dialect mistranscriptions (جدّد->جدة) AND extracts, replacing the old
        two-call repair->extract. rawTranscript is still stored exactly as heard;
        the correction happens only inside extraction.
    """
    entry = await db.entry.find_unique(where={"id": entry_id})
    if entry is None:
        return
    user_id = entry.userId

    # Resolve times against the user's own clock: extraction gets their local
    # "now" (so "tonight"/"in an hour" are right), and the spoken clock time is
    # composed into a UTC instant in their timezone below.
    user = await db.user.find_unique(where={"id": user_id})
    tz = _user_tz(user.timezone if user else None)

    extract_fn = extract_merged if use_merged else extract
    try:
        result, usage = await extract_fn(transcript, now=datetime.now(tz))
    except Exception as exc:  # noqa: BLE001 — record any failure, never crash the caller
        await db.entry.update(
            where={"id": entry_id},
            data={"status": "FAILED", "error": str(exc), "processedAt": _now()},
        )
        return

    # Store the extracted summary/mood on the entry, plus the LLM tokens it cost.
    await db.entry.update(
        where={"id": entry_id},
        data={
            "summary": result["summary"],
            "moodScore": result["mood"]["score"],
            "moodLabel": result["mood"]["label"],
            "emotions": result["mood"]["emotions"],
            "llmInTokens": usage["inTokens"] if usage else None,
            "llmOutTokens": usage["outTokens"] if usage else None,
            "status": "DONE",
            "processedAt": _now(),
        },
    )

    # Persist todos. startAt is composed from dueDate + a spoken time (if any);
    # untimed todos keep startAt = null.
    for todo in result["todos"]:
        await db.todo.create(
            data={
                "entryId": entry_id,
                "userId": user_id,
                "text": todo["text"],
                "dueDate": _parse_due(todo.get("dueDate")),
                "startAt": _compose_start_at(todo.get("dueDate"), todo.get("time"), tz),
            }
        )

    # Upsert topics (per-user unique) and link them to this entry.
    for name in result["topics"]:
        topic = await db.topic.upsert(
            where={"userId_name": {"userId": user_id, "name": name}},
            data={"create": {"userId": user_id, "name": name}, "update": {"name": name}},
        )
        await db.entrytopic.create(data={"entryId": entry_id, "topicId": topic.id})

    # Upsert people (per-user unique) and link them to this entry.
    for name in result["people"]:
        person = await db.person.upsert(
            where={"userId_name": {"userId": user_id, "name": name}},
            data={"create": {"userId": user_id, "name": name}, "update": {"name": name}},
        )
        await db.entryperson.create(data={"entryId": entry_id, "personId": person.id})


async def process_audio_entry(entry_id: str) -> None:
    """Background task: transcribe a saved audio file, then run the pipeline.

    PENDING -> PROCESSING -> transcribe -> save transcript -> process_transcript
    -> DONE (or FAILED + error). A failed transcription never leaves the entry
    stuck in PROCESSING.

    NOTE: this runs via FastAPI BackgroundTasks (in-process). For production this
    is where a real queue/worker (Celery/RQ/Arq + a separate worker process)
    would pick up the job instead.
    """
    entry = await db.entry.find_unique(where={"id": entry_id})
    if entry is None:
        return
    if not entry.audioUrl:
        await db.entry.update(
            where={"id": entry_id},
            data={"status": "FAILED", "error": "no audio file", "processedAt": _now()},
        )
        return

    await db.entry.update(where={"id": entry_id}, data={"status": "PROCESSING"})

    # Download the object to a temp file (transcribe() opens a file), then always
    # clean it up — cloud storage is the source of truth, the temp is scratch.
    try:
        audio_path = await resolve_audio_path(entry.audioUrl)
        try:
            result = await transcribe(audio_path)
        finally:
            try:
                os.remove(audio_path)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        await db.entry.update(
            where={"id": entry_id},
            data={
                "status": "FAILED",
                "error": f"transcription failed: {exc}",
                "processedAt": _now(),
            },
        )
        return

    transcript = result["text"]
    update: dict[str, Any] = {"rawTranscript": transcript}
    # Fill duration from the transcription if the client didn't provide it.
    if entry.durationSec is None and result.get("durationSec"):
        update["durationSec"] = int(round(result["durationSec"]))
    # Record the audio seconds actually sent to STT (unit-economics, Step 11).
    if result.get("durationSec") is not None:
        update["sttSeconds"] = float(result["durationSec"])
    await db.entry.update(where={"id": entry_id}, data=update)

    # Audio takes the MERGED path (Step 10): extraction itself silently corrects
    # dialect mistranscriptions (e.g. Melia hears جدّد "renew" as جدة "Jeddah")
    # and extracts in one call — replacing the old two-call repair->extract. The
    # stored rawTranscript above is untouched: the record stays exactly as heard,
    # and only the derived structure benefits from the correction.
    await process_transcript(entry_id, transcript, use_merged=True)


# ---- Entries -----------------------------------------------------------------


@app.post("/entries", response_model=EntryOut)
async def create_entry(body: CreateEntryRequest, current=Depends(get_current_user)):
    # Meter against the monthly limit (text entries cost an extraction call too).
    await reserve_entry_quota(db, current)
    # Owner is the authenticated user — never client-supplied.
    entry = await db.entry.create(
        data={
            "userId": current.id,
            "rawTranscript": body.transcript,
            "source": body.source or "TEXT",
            "audioUrl": body.audioUrl,
            "durationSec": body.durationSec,
            "status": "PROCESSING",
        }
    )

    # NOTE: the text path runs extraction synchronously for easy testing. The
    # audio path (POST /entries/audio) runs it in the background instead.
    await process_transcript(entry.id, body.transcript)

    return _to_entry_out(await _load_entry(entry.id))


@app.post("/entries/audio", response_model=EntryOut)
async def create_audio_entry(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    durationSec: Optional[float] = Form(None),
    current=Depends(get_current_user),
):
    """Ingest an audio recording. Returns a PENDING entry immediately; the
    transcript + extraction fill in via a background task. Poll GET /entries/{id}
    until status is DONE (or FAILED)."""
    user_id = current.id

    ext = os.path.splitext(file.filename or "")[1].lstrip(".").lower()
    if ext not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format '.{ext}'. Supported: {sorted(SUPPORTED_FORMATS)}",
        )

    # Cost protection — ALL before any upload/STT, keyed off the user's effective
    # plan. Order: cheapest check first (length, from the client-provided
    # duration), then the whole-app kill switch, then the per-user monthly cap.
    reject_if_recording_too_long(durationSec, current)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio file")
    if len(data) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file over the {MAX_BYTES // 1_048_576} MB limit.",
        )
    # Don't trust the declared extension/type — sniff the actual bytes.
    if not looks_like_audio(data):
        raise HTTPException(status_code=400, detail="Uploaded file is not valid audio.")

    await check_global_daily_cap(db)            # kill switch (whole app)
    await reserve_entry_quota(db, current)      # per-user monthly limit

    # Create the PENDING entry first so we have an id to name the file by.
    entry = await db.entry.create(
        data={
            "userId": user_id,
            "rawTranscript": "",
            "source": "VOICE",
            "durationSec": int(durationSec) if durationSec else None,
            "status": "PENDING",
        }
    )

    audio_url = await save_audio(entry.id, ext, data)
    await db.entry.update(where={"id": entry.id}, data={"audioUrl": audio_url})

    # Schedule transcription + extraction; return the PENDING entry immediately.
    background.add_task(process_audio_entry, entry.id)

    return _to_entry_out(await _load_entry(entry.id))


@app.get("/entries", response_model=List[EntryOut])
async def list_entries(limit: int = 20, current=Depends(get_current_user)):
    entries = await db.entry.find_many(
        where={"userId": current.id},
        order={"createdAt": "desc"},
        take=limit,
        include=_ENTRY_INCLUDE,
    )
    return [_to_entry_out(e) for e in entries]


@app.get("/entries/{entry_id}", response_model=EntryOut)
async def get_entry(entry_id: str, current=Depends(get_current_user)):
    return _to_entry_out(await _owned_entry_or_404(entry_id, current.id))


@app.delete("/entries/{entry_id}", status_code=204)
async def delete_entry(entry_id: str, current=Depends(get_current_user)):
    """Delete an entry, its recording, and its todos/topic-and-person links.

    The DB delete cascades Todo + EntryTopic + EntryPerson (schema onDelete).
    The shared Topic/Person rows are user-level and may still be used by other
    entries, so they're deliberately left — orphans are harmless.
    """
    entry = await _owned_entry_or_404(entry_id, current.id)
    # Remove the recording first (best-effort — a missing file is fine).
    if entry.audioUrl:
        await delete_audio(entry.audioUrl)
    await db.entry.delete(where={"id": entry_id})


@app.get("/entries/{entry_id}/audio")
async def get_entry_audio(entry_id: str, current=Depends(get_current_user)):
    """Mint a short-lived signed URL for an entry's recording.

    Authenticated and ownership-checked (404 for non-owners — a user must not be
    able to tell whether someone else's entry exists). Returns {"url": ...}; the
    CLIENT fetches the audio bytes directly from Supabase Storage with that URL,
    so we never proxy (and never pay egress for) the audio.

    This closes the old auth gap: the previous route streamed bytes with no token
    because <audio> can't send an Authorization header. Now an authenticated call
    mints the URL and the media element loads *that*.
    """
    entry = await _owned_entry_or_404(entry_id, current.id)
    if not entry.audioUrl:
        raise HTTPException(status_code=404, detail="Entry has no audio")

    url = await create_signed_url(entry.audioUrl)
    if url is None:
        raise HTTPException(status_code=404, detail="Audio is unavailable")
    return {"url": url}


@app.delete("/entries/{entry_id}/audio", response_model=EntryOut)
async def delete_entry_audio(entry_id: str, current=Depends(get_current_user)):
    """Remove just the recording, keeping the written entry.

    A real privacy affordance for a voice product: you can strip the audio and
    keep the journal text. Returns the updated entry (audioUrl now null).
    """
    entry = await _owned_entry_or_404(entry_id, current.id)
    if entry.audioUrl:
        await delete_audio(entry.audioUrl)
        await db.entry.update(where={"id": entry_id}, data={"audioUrl": None})
    return _to_entry_out(await _load_entry(entry_id))


# ---- Todos -------------------------------------------------------------------


def _todo_sort_key(todo) -> tuple:
    """Order todos soonest-first. A todo's "when" is its startAt (timed) or its
    dueDate (day-only); dated todos come first in chronological order, undated
    ones last (ordered by when they were captured)."""
    when = todo.startAt or todo.dueDate
    if when is not None:
        return (0, when, todo.createdAt)
    return (1, todo.createdAt)


@app.get("/todos", response_model=List[TodoListItem])
async def list_todos(done: Optional[bool] = None, current=Depends(get_current_user)):
    """The user's todos across every entry, soonest-first.

    `?done=true|false` filters by completion; omit it for everything. Timed items
    (startAt) and dated items (dueDate) come first in chronological order, then
    undated ones. Each carries its source entryId so the client can link back.
    """
    where: dict[str, Any] = {"userId": current.id}
    if done is not None:
        where["done"] = done
    todos = await db.todo.find_many(where=where)
    todos.sort(key=_todo_sort_key)
    return [
        TodoListItem(
            id=t.id,
            text=t.text,
            dueDate=t.dueDate,
            startAt=t.startAt,
            done=t.done,
            entryId=t.entryId,
        )
        for t in todos
    ]


@app.patch("/todos/{todo_id}", response_model=TodoOut)
async def update_todo(
    todo_id: str, body: UpdateTodoRequest, current=Depends(get_current_user)
):
    existing = await db.todo.find_unique(where={"id": todo_id})
    if existing is None or existing.userId != current.id:
        raise HTTPException(status_code=404, detail="Todo not found")
    todo = await db.todo.update(where={"id": todo_id}, data={"done": body.done})
    return TodoOut(
        id=todo.id,
        text=todo.text,
        dueDate=todo.dueDate,
        startAt=todo.startAt,
        done=todo.done,
    )


@app.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(todo_id: str, current=Depends(get_current_user)):
    existing = await db.todo.find_unique(where={"id": todo_id})
    if existing is None or existing.userId != current.id:
        raise HTTPException(status_code=404, detail="Todo not found")
    await db.todo.delete(where={"id": todo_id})


# ---- Insights ----------------------------------------------------------------


@app.get("/insights", response_model=InsightsOut)
async def insights(days: int = 14, current=Depends(get_current_user)):
    user_id = current.id
    since = _now() - timedelta(days=days)

    entries = await db.entry.find_many(
        where={"userId": user_id, "createdAt": {"gte": since}},
        include={
            "topics": {"include": {"topic": True}},
            "people": {"include": {"person": True}},
        },
        order={"createdAt": "asc"},
    )

    # moodTrend: average moodScore per day in the user's LOCAL timezone, over
    # entries that have a score — so a late-night entry lands on the day it felt
    # like, not the next UTC day.
    tz = _user_tz(current.timezone)
    day_scores: dict[str, list[int]] = defaultdict(list)
    for e in entries:
        if e.moodScore is not None:
            local_day = e.createdAt.astimezone(tz).date().isoformat()
            day_scores[local_day].append(e.moodScore)
    mood_trend = [
        MoodTrendPoint(
            date=day,
            avgScore=round(sum(scores) / len(scores), 2),
            count=len(scores),
        )
        for day, scores in sorted(day_scores.items())
    ]

    # topTopics / topPeople: occurrence counts across the window, sorted desc.
    topic_counts: dict[str, int] = defaultdict(int)
    people_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        for et in e.topics or []:
            if et.topic:
                topic_counts[et.topic.name] += 1
        for ep in e.people or []:
            if ep.person:
                people_counts[ep.person.name] += 1

    def _ranked(counts: dict[str, int]) -> list[NameCount]:
        # Sort by count desc, then name asc for stable ties.
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [NameCount(name=name, count=count) for name, count in ordered]

    # openTodos: all of the user's incomplete todos (not limited to the window).
    open_todos = await db.todo.count(where={"userId": user_id, "done": False})

    return InsightsOut(
        userId=user_id,
        days=days,
        entryCount=len(entries),
        moodTrend=mood_trend,
        topTopics=_ranked(topic_counts),
        topPeople=_ranked(people_counts),
        openTodos=open_todos,
    )
