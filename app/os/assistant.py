"""/api/assistant: the chatbot (port of vournalOS AssistantService and its controller). Off
unless the server has ANTHROPIC_API_KEY and the user turned AI on. Each message is one
request of the shared monthly AI allowance. The model acts through the tools in
assistant_tools, in the user's mode (design 2026-11-06): in Manual every change waits as a card
until the user approves it (assistant_pending); in Auto changes run at once and can be undone
for 24 hours, edits and deletes included (assistant_snapshot), but a delete that takes more
with it, a second delete in one message, or anything with a file still asks.

The request: claude-sonnet-5-5 with adaptive thinking, effort "low" or "medium" chosen per
message (assistant_effort), prompt caching, the server-side fallback beta, at most 8 tool
rounds (the last one must answer in words), and at most 160 stored turns per chat. The system
prompt and tools began as the NestJS build's (tests/vournal_os/golden/assistant_golden.mjs).
The model's replies are stored exactly as the API returned them, so the history replays unchanged.

Privacy, as before: a conversation sends the user's messages and what the tools read (their
own data) to the AI provider, and is stored only in the user's rows."""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from fastapi import Depends, HTTPException
from prisma import Json

from app.db import db
from app.os import ai, assistant_pending, assistant_tools
from app.os.assistant_effort import escalate, pick_effort
from app.os.common import dates, validators
from app.os.common.body import RuleBody
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.i18n import to_lang
from app.os.common.jsparse import _JS_SPACE, js_slice
from app.os.profile import ensure_os_profile

log = logging.getLogger("vournal.os")
router = nest_router("/assistant")

_DATA = json.loads((Path(__file__).parent / "data" / "assistant.json").read_text())
SYSTEM, TOOLS = _DATA["system"], _DATA["tools"]
MAX_ROUNDS = 8  # tool rounds per message; the last one has to answer in words
MAX_TURNS = 160  # stored turns per chat; past it the user starts a new one (history is replayed whole)
PENDING_TTL = timedelta(hours=24)
REPLY = {
    "refused": {"en": "Sorry, I can’t help with that one.", "ar": "عذرًا، لا أستطيع المساعدة في هذا."},
    "failed": {"en": "Something went wrong on my side. Please try again.", "ar": "حدث خطأ من جهتي. حاول مرة أخرى."},
}
_SPACES = re.compile(f"[{re.escape(_JS_SPACE)}]+")


MODE_LINE = {
    "manual": "Mode: manual — every change waits for the user's approval.",
    "auto": "Mode: auto — changes happen at once and can be undone.",
}


def context_block(now: str, weekday: str, tz: str, language: str, currency: str, name: str | None, mode: str | None = None) -> str:
    """Per-message facts, placed before the user's text."""
    lang = "Arabic" if language == "ar" else "English"
    who = f" User's name: {name}." if name else ""
    how = f" {MODE_LINE[mode]}" if mode in MODE_LINE else ""
    return f"<context>Now: {weekday} {now} ({tz}). App language: {lang}. Default currency: {currency}.{who}{how}</context>"


def title_from(text: str) -> str:
    t = _SPACES.sub(" ", text).strip(_JS_SPACE)
    return f"{js_slice(t, 57)}…" if len(t.encode("utf-16-le")) // 2 > 60 else t


def for_replay(content: list[dict]) -> list[dict]:
    """What to send back next time. After a mid-output fallback, thinking and tool-use blocks
    before the last `fallback` block are dropped (they belong to the model that declined)."""
    kinds = [b.get("type") for b in content]
    if "fallback" not in kinds:
        return content
    boundary = len(kinds) - 1 - kinds[::-1].index("fallback")
    return [b for n, b in enumerate(content) if n >= boundary or b.get("type") not in ("thinking", "redacted_thinking", "tool_use")]


def _last_reply_failed(history: list) -> bool:
    """Whether a tool call failed while answering the previous message (the rows after its
    visible user row)."""
    start = max((n for n, h in enumerate(history) if h.role == "user" and not h.hidden), default=len(history))
    return any(b.get("type") == "tool_result" and b.get("is_error") for h in history[start + 1:] for b in (h.content or []) if isinstance(b, dict))


def _text_of(content: list[dict]) -> str:
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip(_JS_SPACE)


RUNNING_TTL = timedelta(minutes=5)  # an approval still "running" after this was cut off
INTERRUPTED = "Interrupted — ask again"


def _expired(a) -> bool:
    """A waiting card lasts 24 hours, except one from an app recording, which waits as long as
    its entry does; an Undo lasts 24 hours from when the change ran."""
    if a.status == "pending" and a.entryId:
        return False
    since = (a.resolvedAt or a.createdAt) if a.status == "done" else a.createdAt
    return datetime.now(timezone.utc) - since > PENDING_TTL


def _stuck(a) -> bool:
    return a.status == "running" and datetime.now(timezone.utc) - (a.resolvedAt or a.createdAt) > RUNNING_TTL


def _details(a) -> dict | None:
    """What a waiting change will set, from its stored call (ids left out), so the user sees
    the change itself and not only the model's words about it."""
    call = a.call if isinstance(a.call, dict) else json.loads(a.call) if a.call else None
    if a.status != "pending" or not call:
        return None
    return {k: v for k, v in (call.get("input") or {}).items()
            if k not in ("id", "label") and not k.endswith("_id") and isinstance(v, (str, int, float, bool)) and v != ""}


def action_view(a) -> dict:
    status = "expired" if a.status == "pending" and _expired(a) else "failed" if _stuck(a) else a.status
    return {"id": a.id, "kind": a.kind, "entityType": a.entityType, "entityId": a.entityId, "label": a.label, "link": a.link,
            "status": status, "error": INTERRUPTED if _stuck(a) else a.error, "details": _details(a),
            "canUndo": a.status == "done" and a.undo is not None and not _expired(a)}


def _view(m, actions) -> dict:
    return {"id": m.id, "role": "assistant" if m.role == "assistant" else "user", "text": m.text or "", "createdAt": m.createdAt,
            "actions": [action_view(a) for a in actions if a.messageId == m.id]}


# history in the order it was written: two rows of one round can share a millisecond
_IN_ORDER = [{"createdAt": "asc"}, {"id": "asc"}]


# ----- conversations ---------------------------------------------------------------------


async def _owned(user_id: str, id: str):
    c = await db.assistantconversation.find_first(where={"id": id, "userId": user_id, "entryId": None})
    if c is None:
        raise nest_error(404, "Conversation not found")
    return c


async def _owned_action(user_id: str, id: str):
    a = await db.assistantaction.find_first(where={"id": id, "userId": user_id})
    if a is None:
        raise nest_error(404, "Action not found")
    return a


@router.get("/conversations")
async def list_conversations(user=Depends(plus_user)):
    # an app recording's AI actions keep their own conversation, shown only on its review card
    rows = await db.assistantconversation.find_many(where={"userId": user.id, "entryId": None}, order={"lastMessageAt": "desc"}, take=100)
    items = [{"id": c.id, "title": c.title, "channel": c.channel, "lastMessageAt": c.lastMessageAt, "createdAt": c.createdAt} for c in rows]
    return {"items": items, "total": len(items)}


@router.get("/conversations/{id}")
async def get_conversation(id: str, user=Depends(plus_user)):
    c = await _owned(user.id, id)
    messages = await db.assistantmessage.find_many(where={"conversationId": id, "hidden": False}, order=_IN_ORDER)
    actions = await db.assistantaction.find_many(where={"conversationId": id}, order=_IN_ORDER)
    turns = await db.assistantmessage.count(where={"conversationId": id})
    return {"id": c.id, "title": c.title, "channel": c.channel, "lastMessageAt": c.lastMessageAt, "full": turns >= MAX_TURNS,
            "messages": [_view(m, actions) for m in messages]}


@router.delete("/conversations/{id}")
async def remove_conversation(id: str, user=Depends(plus_user)):
    await _owned(user.id, id)
    await db.assistantconversation.delete(where={"id": id})
    return {"ok": True}


# ----- chatting --------------------------------------------------------------------------


async def _append(conversation_id: str, role: str, content: list, text: str | None, hidden: bool, tx=None):
    return await (tx or db).assistantmessage.create(data={"conversationId": conversation_id, "role": role, "content": Json(content), "text": text, "hidden": hidden})


def _log_error(exc: Exception) -> None:
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        log.error("Assistant request rejected: check ANTHROPIC_API_KEY")
    elif isinstance(exc, anthropic.RateLimitError):
        log.warning("Assistant: AI provider rate limit reached")
    elif isinstance(exc, anthropic.APIStatusError):
        log.warning("Assistant request failed (%s): %s", exc.status_code, exc.message)
    elif isinstance(exc, anthropic.APIConnectionError):
        log.warning("Assistant request failed (network): %s", exc)
    else:
        log.warning("Assistant failed: %s", exc)


async def _call(client, round_: int, messages: list, effort: str, system: str = SYSTEM, tools: list = TOOLS) -> dict:
    """One round, as the API returned it (stored and replayed unchanged)."""
    raw = await client.beta.messages.with_raw_response.create(
        model=ai.MODEL,
        max_tokens=8000,
        betas=[ai.FALLBACK_BETA],
        fallbacks="default",
        cache_control={"type": "ephemeral"},
        system=system,
        tools=tools,
        **({"tool_choice": {"type": "none"}} if round_ == MAX_ROUNDS else {}),
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        messages=messages,
        timeout=120,
    )
    return raw.http_response.json()


async def send(user, text: str, conversation_id: str | None = None) -> dict:
    s = await ensure_os_profile(user)
    lang = to_lang(s.language)
    left = await ai.remaining(user.id, s.timezone)

    def fail(state: str, conversation=None) -> dict:
        return {"status": state, "conversation": conversation, "messages": [], "remainingThisMonth": left}

    client = ai.client()
    if client is None:
        return fail("not_configured")
    if not s.aiEnabled:
        return fail("disabled")
    if left <= 0:
        return fail("limit")

    if conversation_id:
        conversation = await _owned(user.id, conversation_id)
    else:
        conversation = await db.assistantconversation.create(data={"userId": user.id, "channel": "APP", "title": title_from(text)})
    if not conversation.title:
        conversation = await db.assistantconversation.update(where={"id": conversation.id}, data={"title": title_from(text)})
    conv = {"id": conversation.id, "title": conversation.title}
    history = await db.assistantmessage.find_many(where={"conversationId": conv["id"]}, order=_IN_ORDER)
    if len(history) >= MAX_TURNS:
        return fail("too_long", conv)

    spent = await ai.spend(user.id, s.timezone, ai.CHAT)  # before the call, so a burst can't run past the allowance
    if spent is None:
        left = 0
        return fail("limit", conv)
    left = spent

    tz = dates.valid_zone(s.timezone)
    now = datetime.now(timezone.utc)
    today = assistant_tools.local_day(now, tz)
    mode = s.assistantMode if s.assistantMode in MODE_LINE else "manual"
    ctx = assistant_tools.Ctx(user, tz, today, mode)
    weekday = ai.WEEKDAYS[(datetime.fromisoformat(f"{today}T12:00:00+00:00").weekday() + 1) % 7]
    block = context_block(f"{today} {assistant_tools.local_time(now, tz)}", weekday, s.timezone, lang, s.defaultCurrency, s.name, mode)
    first = [{"type": "text", "text": f"{block}\n{text}"}]
    user_row = await _append(conv["id"], "user", first, text, False)
    messages = [{"role": h.role, "content": h.content} for h in history] + [{"role": "user", "content": first}]

    drafts: list[dict] = []
    reply = None
    failed = False
    effort = pick_effort(text, _last_reply_failed(history))
    try:
        for round_ in range(1, MAX_ROUNDS + 1):
            res = await _call(client, round_, messages, effort)
            if res.get("stop_reason") == "refusal":
                reply = {"content": [{"type": "text", "text": REPLY["refused"][lang]}], "text": REPLY["refused"][lang]}
                break
            content = for_replay(res.get("content") or [])
            uses = [b for b in res.get("content") or [] if b.get("type") == "tool_use"]
            if res.get("stop_reason") != "tool_use" or not uses:
                # a reply cut off mid tool call (max_tokens) keeps no unanswered call: replayed, it
                # would fail every later message in the chat (a fix; NestJS stored it as it was)
                kept = [b for b in content if b.get("type") != "tool_use"]
                if not any(b.get("type") == "text" and b.get("text", "").strip(_JS_SPACE) for b in kept) and uses:
                    kept = [{"type": "text", "text": REPLY["failed"][lang]}]
                reply = {"content": kept, "text": _text_of(kept) or REPLY["failed"][lang]}
                break
            results = []
            ctx.round_deletes = sum(1 for use in uses if use["name"] == "delete_item")
            for use in uses:
                out = await assistant_tools.run(use["name"], use.get("input"), ctx)
                if out.get("action"):
                    drafts.append({"id": out["action"].get("id") or uuid.uuid4().hex, **out["action"], "position": len(drafts)})
                results.append({"type": "tool_result", "tool_use_id": use["id"], "content": out["content"], **({"is_error": True} if out.get("isError") else {})})
            # a round is stored whole, so the history always pairs each tool call with its result
            async with db.tx() as tx:
                await _append(conv["id"], "assistant", content, None, True, tx)
                await _append(conv["id"], "user", results, None, True, tx)
            messages += [{"role": "assistant", "content": content}, {"role": "user", "content": results}]
            effort = escalate(effort, len(uses), any(r.get("is_error") for r in results))
    except Exception as exc:  # noqa: BLE001 — the user gets a short reply; the history stays valid
        _log_error(exc)
        failed = True

    # a failed call still gets a (short) reply, so the history stays a valid conversation
    final = reply if reply and not failed else {"content": [{"type": "text", "text": REPLY["failed"][lang]}], "text": REPLY["failed"][lang]}
    reply_row = await _append(conv["id"], "assistant", final["content"], final["text"], False)
    actions = await _save_actions(user.id, conv["id"], reply_row.id, drafts)
    await db.assistantconversation.update(where={"id": conv["id"]}, data={"lastMessageAt": datetime.now(timezone.utc)})
    return {
        "status": "unavailable" if failed else "ok",
        "conversation": conv,
        "messages": [_view(user_row, []), {**_view(reply_row, []), "actions": [action_view(a) for a in actions]}],
        "remainingThisMonth": left,
    }


async def _save_actions(user_id: str, conversation_id: str, message_id: str, drafts: list[dict], entry_id: str | None = None) -> list:
    """An app recording's set (entry_id) replaces what was waiting for that entry before."""
    if not drafts and not entry_id:
        return []
    async with db.tx() as tx:
        if entry_id:
            await tx.assistantaction.update_many(where={"entryId": entry_id, "status": "pending"},
                                                 data={"status": "cancelled", "resolvedAt": datetime.now(timezone.utc)})
        rows = []
        for d in drafts:
            data = {"id": d["id"], "conversationId": conversation_id, "userId": user_id, "messageId": message_id, "kind": d["kind"],
                    "entityType": d["entityType"], "entityId": d["entityId"], "label": js_slice(d["label"], 300), "link": d["link"],
                    "status": d["status"], "position": d.get("position", 0), **({"entryId": entry_id} if entry_id else {})}
            if d.get("undo"):
                data["undo"] = Json(d["undo"])
            if d.get("call"):
                data["call"] = Json(d["call"])
            rows.append(await tx.assistantaction.create(data=data))
        return rows


# ----- buttons on actions ----------------------------------------------------------------


async def _resolve(id: str, state: str, from_states=("pending", "done")):
    # conditional on the current status, so a double click can't run it twice
    count = await db.assistantaction.update_many(where={"id": id, "status": {"in": list(from_states)}}, data={"status": state, "resolvedAt": datetime.now(timezone.utc)})
    if count != 1:
        raise nest_error(400, "Already handled")
    return await db.assistantaction.find_unique(where={"id": id})


async def _note(conversation_id: str, text: str) -> None:
    """A hidden note for the model about what the user did with a button."""
    await _append(conversation_id, "user", [{"type": "text", "text": text}], None, True)
    await db.assistantconversation.update(where={"id": conversation_id}, data={"lastMessageAt": datetime.now(timezone.utc)})


async def _tool_ctx(user):
    s = await ensure_os_profile(user)
    tz = dates.valid_zone(s.timezone)
    return assistant_tools.Ctx(user, tz, assistant_tools.local_day(datetime.now(timezone.utc), tz))


EXPIRED = "This request expired — ask again"


async def _approve(user, a, ctx) -> dict:
    """Runs a waiting card once: it's claimed (pending → running) before anything runs, so two
    clicks or two tabs can't both run it."""
    if a.status != "pending":
        raise nest_error(400, "Nothing to approve" if a.status not in ("running", "done") else "Already handled")
    if _expired(a):
        raise nest_error(400, EXPIRED)
    await _resolve(a.id, "running", ("pending",))
    try:
        out = await assistant_pending.run_approved(a, ctx)
    except Exception as exc:  # noqa: BLE001 — never leave a card running
        log.warning("Approving %s failed: %r", a.id, exc)
        out = {"error": "Something went wrong while doing that"}
    if "error" in out:
        data = {"status": "failed", "error": js_slice(out["error"], 300)}
        note = f'[The user approved "{a.label}" but it failed: {out["error"]}]'
    else:
        r = out["action"]
        data = {"status": "done", "entityId": r.get("entityId") or a.entityId, "label": js_slice(r.get("label") or a.label, 300),
                "link": r.get("link") or a.link}
        if r.get("undo"):
            data["undo"] = Json(r["undo"])
        note = f'[The user approved: "{data["label"]}" — done.]'
    done = await db.assistantaction.update(where={"id": a.id}, data={**data, "resolvedAt": datetime.now(timezone.utc)})
    await _note(a.conversationId, note)
    return action_view(done)


@router.post("/actions/{id}/approve")
async def approve(id: str, user=Depends(plus_user)):
    a = await _owned_action(user.id, id)
    return await _approve(user, a, await _tool_ctx(user))


@router.post("/actions/{id}/confirm")
async def confirm(id: str, user=Depends(plus_user)):
    """The older name for approve (deletes only, before the modes)."""
    return await approve(id, user)


@router.post("/messages/{message_id}/approve-all")
async def approve_all(message_id: str, user=Depends(plus_user)):
    """Every waiting card under one reply, in the order the changes were made; one failing
    doesn't stop the rest."""
    rows = await db.assistantaction.find_many(where={"messageId": message_id, "userId": user.id, "status": "pending"},
                                              order=[{"position": "asc"}, {"createdAt": "asc"}])
    if not rows:
        raise nest_error(400, "Nothing to approve")
    ctx = await _tool_ctx(user)
    views = []
    for a in rows:
        try:
            views.append(await _approve(user, a, ctx))
        except HTTPException:
            views.append(action_view(await db.assistantaction.find_unique(where={"id": a.id})))
    return views


@router.post("/actions/{id}/cancel")
async def cancel(id: str, user=Depends(plus_user)):
    a = await _owned_action(user.id, id)
    if a.status != "pending" and not _stuck(a):
        raise nest_error(400, "Nothing to cancel")
    if a.status == "pending" and _expired(a):
        raise nest_error(400, EXPIRED)
    done = await _resolve(a.id, "cancelled", ("pending", "running"))
    await _note(a.conversationId, f'[The user cancelled deleting "{a.label}". It was kept.]')
    return action_view(done)


@router.post("/actions/{id}/undo")
async def undo(id: str, user=Depends(plus_user)):
    a = await _owned_action(user.id, id)
    if a.status != "done" or not a.undo or _expired(a):
        raise nest_error(400, "This cannot be undone")
    try:
        await assistant_tools.reverse(user, a.undo, await _tool_ctx(user))
    except assistant_tools.ToolError as exc:  # changed or deleted since: say so, change nothing
        raise nest_error(400, str(exc)) from exc
    done = await _resolve(a.id, "undone", ("done",))
    await _note(a.conversationId, f'[The user pressed Undo: "{a.label}" ({a.kind}) was reversed.]')
    return action_view(done)


class ChatBody(RuleBody):
    """@Trim() @IsString() @MinLength(1) @MaxLength(4000) message; optional conversationId."""

    RULES = (
        ("message", False, True, lambda v, f: (
            ([f"{f} must be shorter than or equal to 4000 characters"] if len(v) > 4000 else []) + ([f"{f} must be longer than or equal to 1 characters"] if not v else [])
            if isinstance(v, str) else
            [f"{f} must be shorter than or equal to 4000 characters", f"{f} must be longer than or equal to 1 characters", f"{f} must be a string"])),
        ("conversationId", True, False, validators.string_rules),
    )
    message: object = None
    conversationId: object = None


@router.post("/chat")
async def chat(body: ChatBody, user=Depends(plus_user)):
    """One message → the assistant's reply (after any tool calls)."""
    ai.throttle("assistant-chat", user.id)
    return await send(user, body.message, body.conversationId)
