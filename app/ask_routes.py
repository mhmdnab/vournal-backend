"""Ask vournal from the Record tab (design 2026-11-17).

A recording whose extraction says the speaker asked vournal something ("ask", or "both" with a
diary part) is handed to the assistant as the first message of a new chat, always as approve
cards. On an answer, a pure question is hidden ("asked") rather than deleted, so "Save as a journal
entry instead" (/unask) can bring back the real recording; it gives its monthly entry back, and the
7-day cleanup deletes it with "Needs review" (review.discard_stale). A "both" entry keeps waiting
for review. Nothing changes until an answer arrives: no permission, no allowance or an error leave
the entry waiting, so the app turns it into a journal entry instead.

One request per entry at a time, across instances: the entry is claimed ("asking", with the
time) the way Save claims it, so a second tap gets 409. A claim older than ASK_TIMEOUT was cut off
by a restart and is released, so the entry shows under "Needs review" again."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from prisma import Json
from pydantic import BaseModel, ConfigDict, StrictBool

from app import quota
from app.auth import get_current_user
from app.db import db
from app.entitlements import effective_plan
from app.os import assistant
from app.os.assistant_consent import journal_allowed
from app.os.profile import ensure_os_profile
from app.rate_limit import rate_limited
from app.review_routes import STILL_WORKING, _owned, entry_out

log = logging.getLogger("vournal")
router = APIRouter()

HANDLED = "This entry was already handled."
ASK_TIMEOUT = timedelta(minutes=5)
ASK_LIMIT, ASK_WINDOW = 10, 600  # questions per person per 10 minutes: each one is a paid answer


def _without_claim(proposal: dict) -> dict:
    return {k: v for k, v in proposal.items() if k != "askStartedAt"}


async def release_stale_asks(user_id: str) -> None:
    """Questions whose answer was cut off (a restart mid-request) wait on the review card again."""
    now = datetime.now(timezone.utc)
    for e in await db.entry.find_many(where={"userId": user_id, "reviewState": "asking"}):
        started = (e.proposal or {}).get("askStartedAt")
        if not started or now - datetime.fromisoformat(started) >= ASK_TIMEOUT:
            await db.entry.update_many(where={"id": e.id, "reviewState": "asking"},
                                       data={"reviewState": "needs_review", "proposal": Json(_without_claim(e.proposal or {}))})


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: StrictBool = False  # "Ask vournal instead" on the review card: ask whatever extraction said


@router.post("/entries/{entry_id}/ask")
async def ask(entry_id: str, body: AskBody | None = None, current=Depends(get_current_user)):
    force = bool(body and body.force)
    await release_stale_asks(current.id)
    entry = await _owned(entry_id, current.id)
    if entry.status != "DONE":
        raise HTTPException(status_code=409, detail=STILL_WORKING)
    proposal = dict(entry.proposal or {})
    if entry.reviewState != "needs_review" or proposal.get("conversationId"):
        raise HTTPException(status_code=409, detail=HANDLED)
    if effective_plan(current) != "plus":
        raise HTTPException(status_code=403, detail={"code": "plus_required", "message": "Asking vournal questions is part of vournal Plus."})
    intent = (proposal.get("result") or {}).get("intent") or "journal"
    if not force and intent not in ("ask", "both"):
        raise HTTPException(status_code=400, detail="This entry isn't a question.")
    if not journal_allowed(await ensure_os_profile(current)):
        return {"status": "consent_needed"}
    if rate_limited("ask", current.id, ASK_LIMIT, ASK_WINDOW):
        raise HTTPException(status_code=429, detail={"code": "too_many_asks", "message": "Too many questions in a row. Try again in a few minutes."})

    claimed = await db.entry.update_many(
        where={"id": entry.id, "reviewState": "needs_review"},
        data={"reviewState": "asking", "proposal": Json({**proposal, "askStartedAt": datetime.now(timezone.utc).isoformat()})},
    )
    if not claimed:
        raise HTTPException(status_code=409, detail=HANDLED)  # a second tap, here or on another instance

    async def release() -> None:
        await db.entry.update_many(where={"id": entry.id, "reviewState": "asking"},
                                   data={"reviewState": "needs_review", "proposal": Json(proposal)})

    try:
        result = await assistant.send(current, entry.rawTranscript, always_ask=True)
    except BaseException:
        await release()
        raise
    if result.get("status") != "ok":
        await release()
        return result  # the entry keeps waiting: the app shows it as a journal entry with a reason
    hide = force or intent == "ask"
    proposal["conversationId"] = result["conversation"]["id"]
    await db.entry.update(where={"id": entry.id}, data={"reviewState": "asked" if hide else "needs_review", "proposal": Json(proposal)})
    if hide:
        await quota.release_entry_quota(db, current.id, entry.createdAt)  # a question isn't a journal entry
    return {**result, "entryState": "asked" if hide else "needs_review"}


@router.post("/entries/{entry_id}/unask")
async def unask(entry_id: str, current=Depends(get_current_user)):
    """"Save as a journal entry instead": the hidden question becomes an entry waiting for review
    again, counted again, and gets the AI actions step a journal entry gets."""
    from app import entry_actions

    entry = await _owned(entry_id, current.id)
    if entry.reviewState != "asked":
        raise HTTPException(status_code=409, detail="Only a question can become a journal entry again.")
    await quota.reserve_entry_quota(db, current)  # 429 at the monthly limit: it stays a question
    proposal = {**(entry.proposal or {}), "unasked": True}
    changed = await db.entry.update_many(where={"id": entry.id, "reviewState": "asked"},
                                         data={"reviewState": "needs_review", "proposal": Json(proposal)})
    if not changed:
        await quota.release_entry_quota(db, current.id)
        raise HTTPException(status_code=409, detail=HANDLED)
    # Only a question skipped the AI actions step; an entry asked with "Ask vournal instead" already
    # had it, and running it again would spend twice and repeat the cards.
    if ((entry.proposal or {}).get("result") or {}).get("intent") == "ask":
        try:
            await entry_actions.after_extraction(entry.id)
        except Exception:  # noqa: BLE001 — the entry still reaches the card
            log.exception("AI actions step failed for unasked entry %s", entry.id)
    return await entry_out(entry.id, current.id)
