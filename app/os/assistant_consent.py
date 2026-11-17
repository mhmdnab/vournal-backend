"""/api/assistant/journal-consent: whether the assistant may read the user's journal (design
2026-11-17, Ask vournal). Asked once, the first time someone asks vournal in the app or opens the
assistant on the website; stored on the server so both share the answer. Allowing also turns AI on,
since a chat needs it; "Not now" takes the permission back and leaves the AI switch alone."""

from datetime import datetime, timezone

from fastapi import Depends

from app.db import db
from app.os.common import validators
from app.os.common.body import RuleBody
from app.os.common.deps import plus_user
from app.os.common.http import nest_router
from app.os.profile import ensure_os_profile

router = nest_router("/assistant")


def journal_allowed(settings) -> bool:
    """True once the user allowed the assistant to read their journal."""
    return getattr(settings, "assistantJournalConsentAt", None) is not None


def _view(settings) -> dict:
    return {"allowed": journal_allowed(settings), "asked": settings.assistantJournalAskedAt is not None}


class ConsentBody(RuleBody):
    """@IsBoolean() allow"""

    RULES = (("allow", False, False, validators.bool_rules),)
    allow: object = None


@router.get("/journal-consent")
async def get_consent(user=Depends(plus_user)):
    return _view(await ensure_os_profile(user))


@router.post("/journal-consent")
async def set_consent(body: ConsentBody, user=Depends(plus_user)):
    await ensure_os_profile(user)
    now = datetime.now(timezone.utc)
    data = {"assistantJournalAskedAt": now, "assistantJournalConsentAt": now if body.allow else None}
    if body.allow:
        data["aiEnabled"] = True
    return _view(await db.usersettings.update(where={"userId": user.id}, data=data))
