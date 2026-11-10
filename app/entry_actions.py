"""AI actions from an app recording (design 2026-11-06).

A Plus user's entry that mentions something to act on (extraction's `wantsActions`) is read by
the dashboard assistant's tools in Manual mode: every change it wants waits as a pending action
tied to the entry, and the review card's Save runs the ones the user kept. It only runs when the
user allowed AI actions (UserSettings.aiEnabled, the same switch as the dashboard chat), and each
entry takes one request of the shared monthly AI allowance; answering its questions is free.

To-dos stay with extraction, so the step has no tool that creates tasks, events or reminders.
People the entry found but who don't exist yet are offered as "proposal-person:<index>"; Save
creates them first and swaps in their ids."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prisma import Json

from app.db import db
from app.entities import normalize_name
from app.entitlements import effective_plan
from app.os import ai, assistant, assistant_tools
from app.os.assistant_effort import escalate, pick_effort
from app.os.assistant_pending import PERSON_PLACEHOLDER
from app.os.common import dates
from app.os.common.i18n import to_lang
from app.os.profile import ensure_os_profile

log = logging.getLogger("vournal.entries")

_DATA = json.loads((Path(__file__).parent / "os" / "data" / "entry_actions.json").read_text())
SYSTEM = _DATA["system"]
ASK_USER = _DATA["ask_user"]
WITHDRAW = _DATA["withdraw_proposal"]
EXCLUDED = frozenset({"create_task", "create_event", "create_reminder"})
TOOLS_FOR_ENTRY = [t for t in assistant.TOOLS if t["name"] not in EXCLUDED] + [ASK_USER, WITHDRAW]
MAX_ANSWERS = 3
ASK_AGAIN_AFTER = timedelta(days=7)  # "Not now" on the consent sheet: offered again a week later
ASKED = "Asked the user. Stop now; their answer comes back as the next message."
WITHDRAWN = "Withdrawn: it is off the card."


async def _set_proposal(entry_id: str, **changes) -> dict:
    """Merges `changes` into the entry's proposal (None removes a key)."""
    entry = await db.entry.find_unique(where={"id": entry_id})
    proposal = {**(entry.proposal or {}), **changes}
    proposal = {k: v for k, v in proposal.items() if v is not None}
    await db.entry.update(where={"id": entry_id}, data={"proposal": Json(proposal)})
    return proposal


async def after_extraction(entry_id: str) -> None:
    """Right after a review-mode extraction: propose actions when the entry wants them."""
    entry = await db.entry.find_unique(where={"id": entry_id})
    user = await db.user.find_unique(where={"id": entry.userId})
    if effective_plan(user) != "plus" or not (entry.proposal or {}).get("result", {}).get("wantsActions"):
        return
    if ai.client() is None:
        await _set_proposal(entry_id, skipped="unavailable")
        return
    s = await ensure_os_profile(user)
    if not s.aiEnabled:
        asked = s.aiActionsAskedAt
        due = asked is None or datetime.now(timezone.utc) - asked > ASK_AGAIN_AFTER
        await _set_proposal(entry_id, skipped="consent_needed" if due else "consent_off")
        return
    await run_step(entry, spend=True)


async def _entry_block(entry, user_id: str, ctx: assistant_tools.Ctx) -> str:
    result = entry.proposal["result"]
    lines = ["<entry>", f"Transcript: {entry.rawTranscript}"]
    todos = [t["text"] for t in result.get("todos", [])]
    lines.append("To-dos already found (handled; never add them again): " + ("; ".join(todos) if todos else "none"))
    lines.append("People in this entry:" if result.get("people") else "People in this entry: none")
    for index, name in enumerate(result.get("people", [])):
        known = await db.person.find_unique(where={"userId_normalizedName": {"userId": user_id, "normalizedName": normalize_name(name)}})
        if known:
            lines.append(f"[{index}] {known.name} (id {known.id})")
        else:
            placeholder = f"{PERSON_PLACEHOLDER}{index}"
            ctx.entry_people[placeholder] = name
            lines.append(f'[{index}] {name} (new: use "{placeholder}" as their id)')
    lines.append("</entry>")
    lines.append(await _their_items(user_id))
    return "\n".join(lines)


async def _their_items(user_id: str) -> str:
    """The user's habits, projects and goals, so the step can act on them without searching."""
    habits = await db.habit.find_many(where={"userId": user_id, "isArchived": False}, order={"sortOrder": "asc"}, take=40)
    projects = await db.project.find_many(where={"userId": user_id, "workspaceId": None, "status": {"in": ["PLANNING", "ACTIVE", "ON_HOLD"]}}, take=40)
    goals = await db.goal.find_many(where={"userId": user_id, "status": {"in": ["ACTIVE", "PAUSED"]}}, take=40)

    def habit(h) -> str:
        target = f", {h.targetPerDay} {h.unit or 'times'} a day" if h.targetPerDay > 1 or h.unit else ""
        return f"- {h.name} (id {h.id}{target})"

    lines = ["<their_items>", "Habits:" if habits else "Habits: none", *map(habit, habits)]
    lines += ["Projects:" if projects else "Projects: none", *(f"- {p.name} (id {p.id}, {p.status.lower()})" for p in projects)]
    lines += ["Goals:" if goals else "Goals: none", *(f"- {g.name} (id {g.id})" for g in goals)]
    lines.append("</their_items>")
    return "\n".join(lines)


def _merged(messages: list[dict]) -> list[dict]:
    """Consecutive turns of one role as one turn (an answer follows the question's tool result)."""
    out: list[dict] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": [*out[-1]["content"], *m["content"]]}
        else:
            out.append({"role": m["role"], "content": list(m["content"])})
    return out


def _same(call: dict) -> str:
    """What makes two proposals the same change: the tool and its input."""
    return json.dumps({"name": call.get("name"), "input": call.get("input")}, sort_keys=True)


def _answer_text(answer: dict) -> str:
    return (answer.get("choice") or answer.get("text") or "").strip()


async def run_step(entry, *, spend: bool, answer: dict | None = None) -> None:
    """Proposes the entry's actions (again, with the user's answer). A run that finishes adds its
    proposals to the card and takes off only the earlier ones it withdrew; a run that fails leaves
    the card as it was."""
    user = await db.user.find_unique(where={"id": entry.userId})
    s = await ensure_os_profile(user)
    client = ai.client()
    if client is None:
        await _set_proposal(entry.id, skipped="unavailable")
        return
    if spend and await ai.spend(user.id, s.timezone, ai.ENTRY) is None:
        await _set_proposal(entry.id, skipped="limit")
        return

    conversation = await db.assistantconversation.find_first(where={"entryId": entry.id})
    if conversation is None:
        conversation = await db.assistantconversation.create(
            data={"userId": user.id, "channel": "APP", "title": "Recording", "entryId": entry.id})
    history = await db.assistantmessage.find_many(where={"conversationId": conversation.id}, order=assistant._IN_ORDER)

    tz = dates.valid_zone(s.timezone)
    now = datetime.now(timezone.utc)
    today = assistant_tools.local_day(now, tz)
    ctx = assistant_tools.Ctx(user, tz, today, "manual")
    block = await _entry_block(entry, user.id, ctx)  # also fills ctx.entry_people
    new_rows: list[dict] = []
    if not history:
        weekday = ai.WEEKDAYS[(datetime.fromisoformat(f"{today}T12:00:00+00:00").weekday() + 1) % 7]
        context = assistant.context_block(f"{today} {assistant_tools.local_time(now, tz)}", weekday, s.timezone, to_lang(s.language),
                                          s.defaultCurrency, s.name)
        new_rows.append({"role": "user", "content": [{"type": "text", "text": f"{context}\n{block}"}], "text": None})
    if answer:
        new_rows.append({"role": "user", "content": [{"type": "text", "text": f"Answer: {_answer_text(answer)}"}], "text": _answer_text(answer)})
    for row in new_rows:
        await assistant._append(conversation.id, row["role"], row["content"], row["text"], True)
    messages = _merged([{"role": h.role, "content": h.content} for h in history] + [{"role": r["role"], "content": r["content"]} for r in new_rows])

    drafts: list[dict] = []
    first_position = await db.assistantaction.count(where={"entryId": entry.id})  # after what the card already has
    withdrawn: set[str] = set()
    question = None
    last_row = None
    effort = pick_effort(entry.rawTranscript or "", False)
    try:
        for round_ in range(1, assistant.MAX_ROUNDS + 1):
            res = await assistant._call(client, round_, messages, effort, SYSTEM, TOOLS_FOR_ENTRY)
            content = assistant.for_replay(res.get("content") or [])
            uses = [b for b in res.get("content") or [] if b.get("type") == "tool_use"]
            if res.get("stop_reason") != "tool_use" or not uses:
                kept = [b for b in content if b.get("type") != "tool_use"] or [{"type": "text", "text": "Done."}]
                last_row = await assistant._append(conversation.id, "assistant", kept, assistant._text_of(kept) or None, True)
                break
            results = []
            ctx.round_deletes = sum(1 for use in uses if use["name"] == "delete_item")
            for use in uses:
                if use["name"] == "withdraw_proposal":
                    target = str((use.get("input") or {}).get("id") or "").removeprefix("pending:")
                    withdrawn.add(target)
                    results.append({"type": "tool_result", "tool_use_id": use["id"], "content": WITHDRAWN})
                    continue
                if use["name"] == "ask_user":
                    i = use.get("input") if isinstance(use.get("input"), dict) else {}
                    choices = [c.strip() for c in i.get("choices") or [] if isinstance(c, str) and c.strip()][:4]
                    question = {"id": uuid.uuid4().hex, "question": str(i.get("question") or "").strip(), "choices": choices,
                                "allowText": i.get("allowText") is not False or not choices}
                    results.append({"type": "tool_result", "tool_use_id": use["id"], "content": ASKED})
                    continue
                out = await assistant_tools.run(use["name"], use.get("input"), ctx)
                if out.get("action"):
                    drafts.append({"id": out["action"].get("id") or uuid.uuid4().hex, **out["action"], "position": first_position + len(drafts)})
                results.append({"type": "tool_result", "tool_use_id": use["id"], "content": out["content"], **({"is_error": True} if out.get("isError") else {})})
            async with db.tx() as tx:
                await assistant._append(conversation.id, "assistant", content, None, True, tx)
                last_row = await assistant._append(conversation.id, "user", results, None, True, tx)
            messages += [{"role": "assistant", "content": content}, {"role": "user", "content": results}]
            if question:
                break
            effort = escalate(effort, len(uses), any(r.get("is_error") for r in results))
    except Exception as exc:  # noqa: BLE001 — the entry and its to-dos still reach the card
        assistant._log_error(exc)
        waiting = await db.assistantaction.count(where={"entryId": entry.id, "status": "pending"})
        if not waiting:
            await _set_proposal(entry.id, skipped="unavailable")
        return

    # the same change proposed again (same tool, same input) isn't added twice
    waiting = await db.assistantaction.find_many(where={"entryId": entry.id, "status": "pending"})
    kept = {_same(a.call) for a in waiting if a.call and a.id not in withdrawn}
    # a draft it withdrew in this same run never reaches the card
    drafts = [d for d in drafts if d["id"] not in withdrawn and (not d.get("call") or _same(d["call"]) not in kept)]
    await assistant._save_actions(user.id, conversation.id, last_row.id, drafts, entry_id=entry.id, withdrawn=withdrawn)
    answers = (entry.proposal or {}).get("answers", 0) + (1 if answer else 0)
    await _set_proposal(entry.id, question=question, answers=answers or None, skipped=None)
    await db.assistantconversation.update(where={"id": conversation.id}, data={"lastMessageAt": datetime.now(timezone.utc)})
