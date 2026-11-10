"""Import from other AI chats (spec 2026-11-10-ai-chat-import-design).

The person asks ChatGPT, Claude or another AI to write everything they planned there as one
Markdown file (the dashboard's Copy prompt), then uploads it here. The file is cut into parts
at its top-level headings and fed to the assistant one part at a time, as turns of one new
conversation, in the background; a last turn asks for the report (added, already there,
couldn't add). Each part spends one assistant message from the monthly allowance, refused up
front when the allowance can't cover them all. An import adds and updates; it never deletes.
The person's mode applies: in Manual the changes wait as cards, and one Approve all covers them.
"""

import asyncio
import logging
import re

from fastapi import Depends, Request
from starlette.datastructures import UploadFile

from app.db import db
from app.os import ai
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.profile import ensure_os_profile

log = logging.getLogger("vournal.os")
router = nest_router("/assistant/import")

PART_LIMIT = 12_000  # the chat's own message limit
MAX_BYTES = 200_000
MAX_PARTS = 40
ALLOWED = (".md", ".markdown", ".txt")

# One import at a time per person; the status of each import's conversation while it runs
# (in memory: one backend instance, and a restart only loses the progress counter).
_running: set[str] = set()
_status: dict[str, dict] = {}

INSTRUCTION = """[Import from my other AI chats: part {i} of {n} of my file "{name}".]
{added_so_far}
Add everything in this part to my vournal: projects (with their tasks), tasks, goals, money (accounts with their balances, budgets, debts, bills and subscriptions), habits, events, people and notes.
Rules for this import:
- Only add what the text below says. Copy amounts, dates and names exactly as written; if a date isn't clear, leave it out.
- Before adding something, check with search or the list tools whether it's already in my vournal; if it is, skip it.
- Never delete anything.
- Then reply briefly: what you added from this part, what you skipped because it was already there, and anything you couldn't add (quote it).

{part}"""

REPORT = (
    "I imported my file \"{name}\" into vournal in {n} parts. Here is what you replied after each part:\n\n"
    "{replies}\n\nNow give me the import report in three short lists: what you added, what you skipped "
    "because it was already there, and what you couldn't add (quote each one, so I can add it by hand). "
    "Use only what the replies above say."
)
_REPLY_CHARS = 1_500  # of each part's reply, in the report request


def _is_top_heading(line: str) -> bool:
    return line.startswith("# ") or line.rstrip() == "#"


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    """Lines packed into chunks of at most `limit` characters, cut only between lines (a single
    longer line is cut at the limit)."""
    out, current, size = [], [], 0
    for line in lines:
        while len(line) > limit:
            if current:
                out.append("\n".join(current))
                current, size = [], 0
            out.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            out.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        out.append("\n".join(current))
    return out


def split_parts(text: str, limit: int = PART_LIMIT) -> list[str]:
    """The file in parts of at most `limit` characters: whole top-level sections together
    where they fit, a long section on its own line boundaries."""
    text = text.strip()
    if not text:
        return []
    sections: list[list[str]] = []
    for line in text.split("\n"):
        if _is_top_heading(line) or not sections:
            sections.append([line])
        else:
            sections[-1].append(line)
    parts: list[str] = []
    can_join = False  # the last part holds whole sections (not a cut-up long one)
    for section in sections:
        body = "\n".join(section)
        if len(body) > limit:
            parts.extend(_chunk_lines(section, limit))
            can_join = False
        elif can_join and len(parts[-1]) + 1 + len(body) <= limit:
            parts[-1] = f"{parts[-1]}\n{body}"
        else:
            parts.append(body)
            can_join = True
    return parts


async def _read(request: Request) -> tuple[str, str]:
    form = await request.form(max_files=1)
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        raise nest_error(400, "Choose a .md file to import")
    name = upload.filename.rsplit("/", 1)[-1][:120]
    if not name.lower().endswith(ALLOWED):
        raise nest_error(400, "Import a Markdown (.md) or text (.txt) file")
    data = await upload.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise nest_error(400, "The file is too large (200 KB at most)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise nest_error(400, "The file must be plain UTF-8 text") from None
    if not text.strip():
        raise nest_error(400, "The file is empty")
    return name, text


def _added_line(added: list[str]) -> str:
    if not added:
        return ""
    shown = added[-80:]  # the latest, so a big file doesn't grow every request
    return "Already added by earlier parts of this import (don't add these again): " + "; ".join(shown) + "\n"


async def _run(user, conversation_id: str, name: str, parts: list[str]) -> None:
    """Each part is its own request (the history isn't replayed): what earlier parts added is
    passed along instead, so nothing is added twice. The report is made from the parts' replies."""
    from app.os import assistant

    state = _status[conversation_id]
    added: list[str] = []
    replies: list[str] = []
    try:
        for i, part in enumerate(parts, start=1):
            try:
                res = await assistant.send(
                    user, INSTRUCTION.format(i=i, n=len(parts), name=name, part=part, added_so_far=_added_line(added)),
                    conversation_id, display=f"{name} — part {i} of {len(parts)}", importing=True,
                )
                if res.get("status") != "ok":
                    state["failed"] += 1
                reply = res["messages"][1] if len(res.get("messages") or []) > 1 else None
                if reply:
                    added += [a["label"] for a in reply.get("actions") or [] if a.get("kind") == "create" and a.get("status") != "failed"]
                    replies.append(f"Part {i}: {reply['text'][:_REPLY_CHARS]}")
                else:
                    replies.append(f"Part {i}: (this part couldn't be read: {res.get('status')})")
            except Exception:  # noqa: BLE001 — one part never stops the rest
                log.exception("import part %s failed", i)
                state["failed"] += 1
                replies.append(f"Part {i}: (this part couldn't be read)")
            state["done"] = i
        try:
            report = await assistant.send(
                user, REPORT.format(name=name, n=len(parts), replies="\n\n".join(replies)), conversation_id,
                display="Import report", importing=True,
            )
            if report.get("status") != "ok":
                state["reportFailed"] = True
        except Exception:  # noqa: BLE001
            log.exception("import report failed")
            state["reportFailed"] = True
    finally:
        state["status"] = "done"
        _running.discard(user.id)


def import_running(conversation_id: str) -> bool:
    state = _status.get(conversation_id)
    return bool(state and state["status"] == "running")


_tasks: set[asyncio.Task] = set()  # kept, so a running import isn't garbage-collected


def _refused(message: str, state: str):
    """403 with the chat's own states (as `state`), so the dashboard says the same things."""
    from fastapi import HTTPException

    return HTTPException(403, detail={"message": message, "state": state})


@router.post("", status_code=202)
async def start(request: Request, user=Depends(plus_user)):
    name, text = await _read(request)
    parts = split_parts(text, PART_LIMIT)
    if len(parts) > MAX_PARTS:
        raise nest_error(400, f"The file is too long to import at once ({MAX_PARTS} parts at most)")
    if user.id in _running:
        raise nest_error(409, "An import is already running. Wait for it to finish.")
    # claimed before anything is awaited, so two uploads at once can't both start
    _running.add(user.id)
    try:
        ai.throttle("assistant-import", user.id)
        s = await ensure_os_profile(user)
        if ai.client() is None:
            raise _refused("The assistant isn't available right now.", "not_configured")
        if not s.aiEnabled:
            raise _refused("Turn on the AI assistant in Settings → AI first.", "disabled")
        if await ai.remaining(user.id, s.timezone) < len(parts) + 1:
            raise _refused("Not enough assistant messages left this month for this file.", "limit")
        conversation = await db.assistantconversation.create(
            data={"userId": user.id, "channel": "APP", "title": f"Import: {name}"[:80]}
        )
    except BaseException:
        _running.discard(user.id)
        raise
    _forget_finished()
    _status[conversation.id] = {"userId": user.id, "status": "running", "done": 0, "total": len(parts), "failed": 0}
    task = asyncio.create_task(_run(user, conversation.id, name, parts))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"conversationId": conversation.id, "parts": len(parts)}


def _forget_finished() -> None:
    """Finished imports' counters go once there are many (their chats keep everything)."""
    finished = [k for k, v in _status.items() if v["status"] != "running"]
    for k in finished[:-200]:
        _status.pop(k, None)


async def _owned_conversation(user_id: str, conversation_id: str):
    row = await db.assistantconversation.find_first(where={"id": conversation_id, "userId": user_id})
    if row is None:
        raise nest_error(404, "Conversation not found")
    return row


_PART_LABEL = re.compile(r" — part (\d+) of (\d+)$")


async def _from_the_chat(conversation_id: str) -> dict:
    """No live state (a restart, or another server): read it from the chat. A report means it
    finished; parts without one mean it was cut off."""
    rows = await db.assistantmessage.find_many(where={"conversationId": conversation_id, "role": "user", "hidden": False})
    texts = [r.text or "" for r in rows]
    if "Import report" in texts:
        return {"status": "done", "done": 0, "total": 0, "failed": 0}
    parts = [m for m in (_PART_LABEL.search(t) for t in texts) if m]
    if not parts:
        return {"status": "done", "done": 0, "total": 0, "failed": 0}
    done = max(int(m.group(1)) for m in parts)
    total = max(int(m.group(2)) for m in parts)
    return {"status": "interrupted", "done": done, "total": total, "failed": 0}


@router.get("/{conversation_id}")
async def status(conversation_id: str, user=Depends(plus_user)):
    await _owned_conversation(user.id, conversation_id)
    state = _status.get(conversation_id) or await _from_the_chat(conversation_id)
    return {**{k: state[k] for k in ("status", "done", "total", "failed")}, "reportFailed": bool(state.get("reportFailed"))}


@router.post("/{conversation_id}/approve-all")
async def approve_all(conversation_id: str, user=Depends(plus_user)):
    """Every waiting card of the import, in the order they were made."""
    from fastapi import HTTPException

    from app.os import assistant

    await _owned_conversation(user.id, conversation_id)
    rows = await db.assistantaction.find_many(
        # the import's own additions (a delete asked for later in the same chat keeps its own card)
        where={"conversationId": conversation_id, "userId": user.id, "status": "pending", "entryId": None, "kind": {"not": "delete"}},
        order=[{"createdAt": "asc"}, {"position": "asc"}],
    )
    if not rows:
        raise nest_error(400, "Nothing to approve")
    ctx = await assistant._tool_ctx(user)
    views = []
    for a in rows:
        try:
            views.append(await assistant._approve(user, a, ctx))
        except HTTPException:
            views.append(assistant.action_view(await db.assistantaction.find_unique(where={"id": a.id})))
    return views
