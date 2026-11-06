"""Pydantic request/response models for the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

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
    # Sent by apps that can show the code step. Build 4 phones don't, and get tokens.
    confirmEmail: bool = False

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
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LEN} characters."
            )
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
    """Confirm irreversible account deletion: the password, or the word DELETE
    for an account that has no password (Apple or Google sign-in)."""

    password: Optional[str] = None
    confirmWord: Optional[str] = None


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
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LEN} characters."
            )
        return v


class LoginRequest(BaseModel):
    email: str
    password: str
    confirmEmail: bool = False


class RefreshRequest(BaseModel):
    refreshToken: str


class LogoutRequest(BaseModel):
    refreshToken: str


class TokenResponse(BaseModel):
    accessToken: str
    refreshToken: str
    tokenType: str = "bearer"


class AuthChallengeOut(BaseModel):
    challengeToken: str
    step: Literal["verify_email", "link_identity"]
    expiresAt: datetime
    # Answering this ends the account's password and sessions: the app says so first.
    clearsPassword: bool = False


class VerifyChallengeRequest(BaseModel):
    challengeToken: str
    code: str

    @field_validator("code")
    @classmethod
    def _bound_code(cls, value: str) -> str:
        if len(value) > 100:
            raise ValueError("Code too long")
        return value.strip()


class ConfirmEmailRequest(BaseModel):
    code: str = Field(max_length=100)


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
    emailConfirmed: bool = False
    hasPassword: bool = True


# ---- Requests ----------------------------------------------------------------


class CreateEntryRequest(BaseModel):
    transcript: str
    source: Optional[Literal["TEXT", "VOICE"]] = None
    audioUrl: Optional[str] = None
    durationSec: Optional[int] = None
    # the review card (design 2026-11-06): new apps ask for it; older ones never send it
    review: bool = False


class UpdateTodoRequest(BaseModel):
    done: bool
    # Build 5 apps ask a repeating to-do to move to its next day. Without it (build 4
    # phones), done means done.
    advance: bool = False


class TaskDetailsRequest(BaseModel):
    date: Optional[str] = None
    time: Optional[str] = None
    urgency: Optional[int] = Field(default=None, ge=0, le=3)
    category: Optional[
        Literal[
            "work", "health", "family", "finance", "personal", "learning", "errands"
        ]
    ] = None
    location: Optional[str] = Field(default=None, max_length=200)
    locationLatitude: Optional[float] = Field(
        default=None, ge=-90, le=90, allow_inf_nan=False
    )
    locationLongitude: Optional[float] = Field(
        default=None, ge=-180, le=180, allow_inf_nan=False
    )
    recurrence: Optional[Literal["daily", "weekly", "monthly", "yearly"]] = None
    recurrenceInterval: Optional[int] = Field(default=None, ge=1, le=365)
    recurrenceWeekdays: Optional[
        List[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]]
    ] = None
    recurrenceUntil: Optional[datetime] = None
    calendarRequested: Optional[bool] = None
    remindersRequested: Optional[bool] = None
    dismiss: List[Literal["date", "time", "urgency", "recurrence"]] = []

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v is not None:
            from datetime import date

            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError("Use YYYY-MM-DD")
            date.fromisoformat(v)
        return v

    @field_validator("time")
    @classmethod
    def _time(cls, v):
        if v is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("Use HH:MM (24 hour)")
        return v

    @field_validator("recurrenceUntil")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("recurrenceUntil needs a timezone")
        return v

    @model_validator(mode="after")
    def _no_null_values(self):
        nullable = {
            "date",
            "time",
            "location",
            "locationLatitude",
            "locationLongitude",
            "recurrence",
            "recurrenceUntil",
        }
        for name in self.model_fields_set - nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        coordinates = {"locationLatitude", "locationLongitude"}
        supplied = self.model_fields_set & coordinates
        if supplied and supplied != coordinates:
            raise ValueError("Provide both location coordinates together")
        if (self.locationLatitude is None) != (self.locationLongitude is None):
            raise ValueError("Provide or clear both location coordinates together")
        return self


# ---- Responses ---------------------------------------------------------------


class Mood(BaseModel):
    score: int
    label: Optional[str] = None
    emotions: List[str] = []


class TaskMetadata(BaseModel):
    kind: str = "task"
    urgency: int = 0
    category: str = "personal"
    location: Optional[str] = None
    locationLatitude: Optional[float] = None
    locationLongitude: Optional[float] = None
    recurrence: Optional[str] = None
    recurrenceInterval: int = 1
    recurrenceWeekdays: List[str] = []
    recurrenceUntil: Optional[datetime] = None
    followUps: List[dict] = []


class TodoOut(TaskMetadata):
    id: str
    text: str
    dueDate: Optional[datetime] = None
    startAt: Optional[datetime] = None  # dueDate + a spoken time; null = untimed
    done: bool


class TodoListItem(TaskMetadata):
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
    # the review card (design 2026-11-06); older apps ignore these
    reviewState: Optional[str] = None
    inJournal: bool = True
    review: Optional[dict] = None


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
