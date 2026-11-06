"""The app's review card (design 2026-11-06): what the card shows, and its buttons.

Save writes exactly what the user kept: the to-dos and links through the same code an older
app's entry uses, then the kept AI actions through the dashboard's own approve path, in order.
It runs once: the entry is claimed (needs_review → saving) first, and a repeated Save gets the
stored result back. AI actions from a recording are the app's alone: the dashboard's buttons
refuse them (assistant._owned_action).

Included before /entries/{entry_id}, so /entries/actions/recent isn't read as an entry id. The
entry routes live in app.main, which imports this module: it's imported back only inside calls."""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from prisma import Json
from pydantic import BaseModel, ConfigDict, Field

from app.auth import get_current_user
from app.db import db
from app.entitlements import effective_plan
from app.os import ai, assistant, assistant_pending
from app.os.assistant_pending import PERSON_PLACEHOLDER
from app.os.profile import ensure_os_profile

router = APIRouter()

_PERSON = re.compile(rf"^{re.escape(PERSON_PLACEHOLDER)}(\d+)$")
UNDO_WINDOW = timedelta(hours=24)
SAVE_TIMEOUT = timedelta(minutes=5)  # a Save still "saving" after this was cut off (a restart): it can run again
STILL_WORKING = "Still working on this entry."


# ----- what the card shows -------------------------------------------------------------------


def _people_in(value) -> set[int]:
    if isinstance(value, str):
        m = _PERSON.match(value)
        return {int(m[1])} if m else set()
    if isinstance(value, dict):
        return set().union(*map(_people_in, value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*map(_people_in, value)) if value else set()
    return set()


def review_action(a) -> dict:
    """The dashboard's card view, plus what the review card needs: the tool, the item it points
    at, and which of the entry's new people it needs."""
    call = a.call if isinstance(a.call, dict) else None
    name = call["name"] if call else "delete_item" if a.kind == "delete" else None
    field = assistant_pending._TARGET.get(name, (None,))[0] if name else None
    target = (call.get("input") or {}).get(field) if call and field else a.entityId if a.kind == "delete" else None
    before = call.get("before") if call and a.status == "pending" else None
    return {**assistant.action_view(a), "tool": name, "target": target, "before": before,
            "dependsOnPeople": sorted(_people_in(call.get("input")) if call else ()), "entryId": a.entryId}


async def review_block(entry) -> Optional[dict]:
    """The card: what extraction found, the AI's actions, its question. None for an entry that
    never had a card (an older app's) and has no AI actions."""
    proposal = entry.proposal or {}
    if entry.reviewState is None and not proposal:
        return None
    result = proposal.get("result") or {}
    waiting = entry.reviewState in ("needs_review", "saving")
    statuses = ["pending"] if waiting else ["done", "failed", "undone", "running"]
    rows = await db.assistantaction.find_many(where={"entryId": entry.id, "status": {"in": statuses}}, order=[{"position": "asc"}, {"createdAt": "asc"}])
    return {
        "proposal": {
            "todos": [{"index": n, "text": t.get("text"), "dueDate": t.get("dueDate")} for n, t in enumerate(result.get("todos", []))],
            "people": [{"index": n, "name": v} for n, v in enumerate(result.get("people", []))],
            "topics": [{"index": n, "name": v} for n, v in enumerate(result.get("topics", []))],
            "places": [{"index": n, "name": v} for n, v in enumerate(result.get("places", []))],
            "journalSuggested": proposal.get("journalSuggested", True),
        },
        "actions": [review_action(a) for a in rows],
        "question": proposal.get("question") if waiting else None,
        "skipped": proposal.get("skipped") if waiting else None,
        "answersLeft": max(0, _max_answers() - proposal.get("answers", 0)),
    }


def _max_answers() -> int:
    from app import entry_actions

    return entry_actions.MAX_ANSWERS


async def entry_out(entry_id: str, user_id: str):
    """EntryOut with its card, for the routes that render one entry."""
    from app import main
    from app.entities import ambiguous_norm_sets

    entry = await main._load_entry(entry_id)
    out = main._to_entry_out(entry, await ambiguous_norm_sets(db, user_id))
    out.review = await review_block(entry)
    return out


async def _owned(entry_id: str, user_id: str):
    entry = await db.entry.find_first(where={"id": entry_id, "userId": user_id})
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


def _require_plus(user) -> None:
    if effective_plan(user) != "plus":
        raise HTTPException(status_code=403, detail={"code": "plus_required", "message": "AI actions are part of vournal Plus."})


# ----- the AI's question, and running it after consent -------------------------------------


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Optional[str] = Field(default=None, max_length=200)
    text: Optional[str] = Field(default=None, max_length=1000)


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: Optional[Answer] = None


@router.post("/entries/{entry_id}/propose")
async def propose(entry_id: str, body: ProposeBody, current=Depends(get_current_user)):
    """With an answer: the AI carries on from its question (free, at most MAX_ANSWERS). Without:
    the step runs now that the user allowed AI actions (one request of the allowance)."""
    from app import entry_actions

    _require_plus(current)
    ai.throttle("entry-propose", current.id)
    entry = await _owned(entry_id, current.id)
    if entry.reviewState != "needs_review":
        raise HTTPException(status_code=409, detail="This entry was already saved.")
    if entry.status != "DONE":
        raise HTTPException(status_code=409, detail=STILL_WORKING)
    if not (await ensure_os_profile(current)).aiEnabled:
        raise HTTPException(status_code=400, detail="Turn on AI actions first.")
    proposal = entry.proposal or {}
    answer = body.answer.model_dump() if body.answer and (body.answer.choice or body.answer.text or "").strip() else None
    if answer:
        if proposal.get("answers", 0) >= entry_actions.MAX_ANSWERS:
            raise HTTPException(status_code=400, detail="No more questions on this entry")
        await entry_actions.run_step(entry, spend=False, answer=answer)
    elif proposal.get("skipped") in ("consent_needed", "consent_off"):
        await entry_actions.run_step(entry, spend=True)
    else:
        raise HTTPException(status_code=400, detail="Nothing to run")
    return await entry_out(entry_id, current.id)


# ----- Save and Discard ---------------------------------------------------------------------


class Kept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    keep: bool = True


class KeptTodo(Kept):
    text: Optional[str] = Field(default=None, max_length=500)


class KeptAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    keep: bool = True
    input: Optional[dict] = None


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inJournal: bool = True
    todos: list[KeptTodo] = []
    people: list[Kept] = []
    topics: list[Kept] = []
    places: list[Kept] = []
    actions: list[KeptAction] = []


def _keep(items: list[Kept], count: int) -> set[int]:
    """Indexes kept: everything the body didn't untick."""
    dropped = {k.index for k in items if not k.keep}
    return {n for n in range(count) if n not in dropped}


def _swap_people(value, people: dict[int, str]):
    """Placeholders for the entry's new people → their ids; KeyError for one the user dropped."""
    if isinstance(value, str):
        m = _PERSON.match(value)
        return people[int(m[1])] if m else value
    if isinstance(value, dict):
        return {k: _swap_people(v, people) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap_people(v, people) for v in value]
    return value


@router.post("/entries/{entry_id}/save")
async def save(entry_id: str, body: SaveBody, current=Depends(get_current_user)):
    from app import main

    entry = await _owned(entry_id, current.id)
    if entry.reviewState == "saved":
        return await entry_out(entry_id, current.id)  # a repeated Save: the result it already had
    if entry.status != "DONE":
        raise HTTPException(status_code=409, detail=STILL_WORKING)
    if entry.reviewState == "saving":
        started = (entry.proposal or {}).get("savingAt")
        if started and datetime.now(timezone.utc) - datetime.fromisoformat(started) < SAVE_TIMEOUT:
            raise HTTPException(status_code=409, detail="Still saving")
        # cut off part-way: it can run again (what it already made isn't made twice)
        await db.entry.update_many(where={"id": entry.id, "reviewState": "saving"}, data={"reviewState": "needs_review"})
        entry = await _owned(entry_id, current.id)
    if entry.reviewState != "needs_review":
        raise HTTPException(status_code=409, detail="This entry has nothing to review.")

    waiting = await db.assistantaction.find_many(where={"entryId": entry.id, "status": "pending"}, order=[{"position": "asc"}, {"createdAt": "asc"}])
    choices = {a.id: a for a in body.actions}
    calls: dict[str, dict] = {}
    for a in waiting:  # every edit is checked before anything is written
        chosen = choices.get(a.id)
        if chosen is not None and not chosen.keep:
            continue
        call = a.call if isinstance(a.call, dict) else None
        if call and chosen is not None and chosen.input:
            merged = {**(call.get("input") or {}), **chosen.input}
            problem = assistant_pending.check_input(assistant_pending.SCHEMAS[call["name"]], merged)
            if problem:
                raise HTTPException(status_code=400, detail={"field": problem.split(" ")[0], "message": problem})
            call = {**call, "input": merged}
        calls[a.id] = call

    claimed = await db.entry.update_many(
        where={"id": entry.id, "reviewState": "needs_review"},
        data={"reviewState": "saving", "proposal": Json({**(entry.proposal or {}), "savingAt": datetime.now(timezone.utc).isoformat()})},
    )
    if claimed != 1:
        again = await _owned(entry_id, current.id)
        if again.reviewState == "saved":
            return await entry_out(entry_id, current.id)
        raise HTTPException(status_code=409, detail="Still saving")

    try:
        proposal = entry.proposal or {}
        result = proposal.get("result") or {"todos": [], "topics": [], "people": [], "places": []}
        edited = {t.index: t.text for t in body.todos if t.keep and t.text}
        # a Save that ran part-way before already made the to-dos (links are made once anyway)
        made = await db.todo.count(where={"entryId": entry.id})
        keep = {
            "todos": {} if made else {n: edited.get(n) for n in _keep(body.todos, len(result.get("todos", [])))},
            "people": _keep(body.people, len(result.get("people", []))),
            "topics": _keep(body.topics, len(result.get("topics", []))),
            "places": _keep(body.places, len(result.get("places", []))),
        }
        now = datetime.fromisoformat(proposal["now"]) if proposal.get("now") else datetime.now(timezone.utc)
        people = await main.apply_extraction(entry.id, result, entry.rawTranscript, use_merged=bool(proposal.get("useMerged")), now=now, keep=keep)

        ctx = await assistant._tool_ctx(current)
        outcome: dict[str, str] = {}
        for a in waiting:
            if a.id not in calls:
                await db.assistantaction.update(where={"id": a.id}, data={"status": "cancelled", "resolvedAt": datetime.now(timezone.utc)})
                continue
            call = calls[a.id]
            try:
                swapped = _swap_people(call["input"], people) if call else None
            except KeyError:  # it needs a person the user unticked
                await db.assistantaction.update(where={"id": a.id}, data={"status": "cancelled", "resolvedAt": datetime.now(timezone.utc)})
                continue
            if call:
                a = await db.assistantaction.update(where={"id": a.id}, data={"call": Json({**call, "input": swapped})})
            try:
                outcome[a.id] = (await assistant._approve(current, a, ctx))["status"]
            except HTTPException:
                outcome[a.id] = (await db.assistantaction.find_unique(where={"id": a.id})).status
    except Exception:
        # nothing half-claimed: the card can be saved again (what was created stays created)
        await db.entry.update(where={"id": entry.id}, data={"reviewState": "needs_review"})
        raise

    await db.entry.update(where={"id": entry.id}, data={
        "reviewState": "saved", "inJournal": body.inJournal,
        "proposal": Json({**{k: v for k, v in proposal.items() if k != "savingAt"}, "saveResult": outcome}),
    })
    return await entry_out(entry_id, current.id)


@router.post("/entries/{entry_id}/discard", status_code=204)
async def discard(entry_id: str, current=Depends(get_current_user)):
    """Throws an unsaved entry away, with its recording and its waiting AI actions. The month's
    entry count isn't given back: the recording was already transcribed and read."""
    from app.storage import delete_audio

    entry = await _owned(entry_id, current.id)
    if entry.reviewState != "needs_review":
        raise HTTPException(status_code=409, detail="Only an unsaved entry can be discarded.")
    if entry.status != "DONE":
        raise HTTPException(status_code=409, detail=STILL_WORKING)
    if entry.audioUrl:
        await delete_audio(entry.audioUrl)
    await db.entry.delete(where={"id": entry.id})
    return Response(status_code=204)


# ----- Undo, and the last day's AI actions ---------------------------------------------------


@router.post("/entries/{entry_id}/actions/{action_id}/undo")
async def undo(entry_id: str, action_id: str, current=Depends(get_current_user)):
    from app.os import assistant_tools

    a = await db.assistantaction.find_first(where={"id": action_id, "entryId": entry_id, "userId": current.id})
    if a is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if a.status != "done" or not a.undo or assistant._expired(a):
        raise HTTPException(status_code=400, detail="This cannot be undone")
    try:
        await assistant_tools.reverse(current, a.undo, await assistant._tool_ctx(current))
    except assistant_tools.ToolError as exc:  # changed or deleted since: say so, change nothing
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    count = await db.assistantaction.update_many(where={"id": a.id, "status": "done"}, data={"status": "undone", "resolvedAt": datetime.now(timezone.utc)})
    if count != 1:
        raise HTTPException(status_code=400, detail="Already handled")
    return review_action(await db.assistantaction.find_unique(where={"id": a.id}))


@router.get("/entries/actions/recent")
async def recent(current=Depends(get_current_user)):
    """What AI actions did from recordings in the last 24 hours, newest first, for Undo."""
    rows = await db.assistantaction.find_many(
        where={"userId": current.id, "entryId": {"not": None}, "status": "done",
               "resolvedAt": {"gte": datetime.now(timezone.utc) - UNDO_WINDOW}},
        order={"resolvedAt": "desc"}, take=100)
    return [review_action(a) for a in rows if a.undo is not None]


# ----- AI actions consent (the dashboard's "AI on" switch) -----------------------------------


class AiActionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("/me/ai-actions")
async def get_ai_actions(current=Depends(get_current_user)):
    _require_plus(current)
    return {"enabled": (await ensure_os_profile(current)).aiEnabled}


@router.post("/me/ai-actions")
async def set_ai_actions(body: AiActionsBody, current=Depends(get_current_user)):
    _require_plus(current)
    await ensure_os_profile(current)
    s = await db.usersettings.update(where={"userId": current.id},
                                     data={"aiEnabled": body.enabled, "aiActionsAskedAt": datetime.now(timezone.utc)})
    return {"enabled": s.aiEnabled}
