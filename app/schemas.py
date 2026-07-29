"""Pydantic request/response models for the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator

# Deliberately permissive — real validation is the DB unique constraint + login.
# Using our own regex avoids pulling in pydantic's EmailStr / email-validator dep.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _clean_timezone(v: Optional[str]) -> Optional[str]:
    """Coerce an IANA timezone name to a valid one, or None. Never raises — an
    unrecognized value from a client just falls back to UTC server-side, which is
    better than rejecting a registration over a bad tz string."""
    if not isinstance(v, str) or not v.strip():
        return None
    name = v.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


# ---- Auth --------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str
    # IANA timezone (e.g. "Asia/Beirut"), sent automatically by the client from
    # the device. Optional; invalid/absent falls back to UTC.
    timezone: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Enter a valid email address.")
        return v

    @field_validator("password")
    @classmethod
    def _valid_password(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LEN:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: Optional[str]) -> Optional[str]:
        return _clean_timezone(v)


class UpdateMeRequest(BaseModel):
    """Update mutable profile fields. Only timezone for now."""

    timezone: Optional[str] = None

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: Optional[str]) -> Optional[str]:
        return _clean_timezone(v)


class DeleteAccountRequest(BaseModel):
    """Confirm irreversible account deletion by re-entering the password."""

    password: str


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str
    newPassword: str

    @field_validator("newPassword")
    @classmethod
    def _valid_password(cls, v: str) -> str:
        if len(v) < MIN_PASSWORD_LEN:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refreshToken: str


class LogoutRequest(BaseModel):
    refreshToken: str


class TokenResponse(BaseModel):
    accessToken: str
    refreshToken: str
    tokenType: str = "bearer"


class UsageOut(BaseModel):
    """Live entitlement usage for the plan/usage surface — all derived from the
    effective plan (app.quota.usage_snapshot), so it matches what's enforced."""
    monthlyLimit: int
    monthlyUsed: int
    maxRecordingSeconds: int
    resetAt: str  # ISO8601 Z — start of next calendar month (when the count rolls)


class UserOut(BaseModel):
    id: str
    email: str
    timezone: Optional[str] = None
    # The EFFECTIVE plan (via app.entitlements.effective_plan) — what the user is
    # entitled to right now, so the clients can gate. planExpiresAt is null when
    # the effective plan is free (nothing to show).
    plan: str = "free"
    planExpiresAt: Optional[datetime] = None
    # Live usage/limits for the plan surface (null only on older serializations).
    usage: Optional[UsageOut] = None
    createdAt: datetime


# ---- Requests ----------------------------------------------------------------


class CreateEntryRequest(BaseModel):
    transcript: str
    source: Optional[Literal["TEXT", "VOICE"]] = None
    audioUrl: Optional[str] = None
    durationSec: Optional[int] = None


class UpdateTodoRequest(BaseModel):
    done: bool


# ---- Responses ---------------------------------------------------------------


class Mood(BaseModel):
    score: int
    label: Optional[str] = None
    emotions: List[str] = []


class TodoOut(BaseModel):
    id: str
    text: str
    dueDate: Optional[datetime] = None
    startAt: Optional[datetime] = None  # dueDate + a spoken time; null = untimed
    done: bool


class TodoListItem(BaseModel):
    """A todo in the standalone list — carries its source entry for linking."""

    id: str
    text: str
    dueDate: Optional[datetime] = None
    startAt: Optional[datetime] = None
    done: bool
    entryId: str


ThreadKind = Literal["people", "topics", "places"]


class EntityChip(BaseModel):
    """A tappable person/topic/place on an entry (Step 17, Phase 4). Carries the
    entity id + kind so the chip can open its thread; `ambiguous` is true when this
    surface form is shared by 2+ entities of the kind, so the client can offer a
    disambiguation sheet instead of navigating."""
    id: str
    kind: ThreadKind
    name: str
    ambiguous: bool = False


class EntryOut(BaseModel):
    id: str
    userId: str
    source: str
    status: str
    summary: Optional[str] = None
    mood: Mood
    todos: List[TodoOut] = []
    topics: List[EntityChip] = []
    people: List[EntityChip] = []
    places: List[EntityChip] = []
    audioUrl: Optional[str] = None
    durationSec: Optional[int] = None
    rawTranscript: str
    processedAt: Optional[datetime] = None
    error: Optional[str] = None
    createdAt: datetime


# ---- Insights ----------------------------------------------------------------


class MoodTrendPoint(BaseModel):
    date: str  # "YYYY-MM-DD" (UTC)
    avgScore: float
    count: int


class NameCount(BaseModel):
    name: str
    count: int


class InsightsOut(BaseModel):
    userId: str
    days: int
    entryCount: int
    moodTrend: List[MoodTrendPoint] = []
    topTopics: List[NameCount] = []
    topPeople: List[NameCount] = []
    openTodos: int


# ---- Threads (the life index, Step 17) ---------------------------------------
# (ThreadKind is defined above, near EntityChip.)


class ThreadSummary(BaseModel):
    """One row of the life index — a person/topic/place and the shape of its arc."""
    id: str
    kind: ThreadKind
    name: str
    momentCount: int
    firstDate: datetime
    lastDate: datetime
    # Mood scores over time (chronological, nulls dropped), capped for a sparkline.
    moodSeries: List[int] = []


class ThreadMoment(BaseModel):
    """One appearance of a thread — an entry it was mentioned in."""
    entryId: str
    date: datetime
    moodScore: Optional[int] = None
    summary: str


class ThreadDetail(BaseModel):
    """One thread opened up: its span, average mood, and ordered moments."""
    id: str
    kind: ThreadKind
    name: str
    momentCount: int
    firstDate: datetime
    lastDate: datetime
    averageMood: Optional[float] = None
    moments: List[ThreadMoment] = []


# ---- Correction (Step 17, Phase 4) ------------------------------------------


class DisambiguationCandidate(BaseModel):
    """One entity a shared surface form could mean — with context that actually
    distinguishes it (so "Which Karim?" is answerable)."""
    id: str
    name: str
    momentCount: int
    topTopics: List[str] = []
    lastMoment: Optional[datetime] = None


class DisambiguationOut(BaseModel):
    kind: ThreadKind
    name: str  # the shared surface form
    candidates: List[DisambiguationCandidate] = []
    suggestedName: str  # a distinct default for "Someone new" (e.g. "Karim · work")


class MergeRequest(BaseModel):
    intoId: str  # absorb this thread INTO intoId; both surface forms survive


class MoveMomentRequest(BaseModel):
    """Reassign one moment (entry) off this thread onto another. Exactly one of
    toId (an existing thread) or toName (create/find one) — with an optional alias
    to attach (the surface form being split off, so ambiguity forms correctly)."""
    toId: Optional[str] = None
    toName: Optional[str] = None
    alias: Optional[str] = None
