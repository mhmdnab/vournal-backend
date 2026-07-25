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


class UserOut(BaseModel):
    id: str
    email: str
    timezone: Optional[str] = None
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


class EntryOut(BaseModel):
    id: str
    userId: str
    source: str
    status: str
    summary: Optional[str] = None
    mood: Mood
    todos: List[TodoOut] = []
    topics: List[str] = []
    people: List[str] = []
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
