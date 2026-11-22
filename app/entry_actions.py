"""AI actions from an app recording (design 2026-11-06).

A Plus user's entry that mentions something to act on (extraction's `wantsActions`) is read by
the dashboard assistant's tools in Manual mode: every change it wants waits as a pending action
tied to the entry, and the review card's Save runs the ones the user kept. It only runs when the
user allowed AI actions (UserSettings.aiEnabled, the same switch as the dashboard chat), and each
entry takes one request of the shared monthly AI allowance; answering its questions is free.

To-dos stay with extraction, so the step has no tool that creates tasks, events or reminders.
People the entry found but who don't exist yet are offered as "proposal-person:<index>"; Save
creates them first and swaps in their ids. An extracted to-do that is really an existing task or
event the step changes ("push the haircut to Friday") comes off the card (_drop_todos)."""

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
from app.task_intelligence import _fold

log = logging.getLogger("vournal.entries")

_DATA = json.loads((Path(__file__).parent / "os" / "data" / "entry_actions.json").read_text())
SYSTEM = _DATA["system"]
ASK_USER = _DATA["ask_user"]
WITHDRAW = _DATA["withdraw_proposal"]
DROP_TODO = _DATA["drop_todo"]
EXCLUDED = frozenset({"create_task", "create_event", "create_reminder", "search_journal", "get_journal_entries"})
# the vournal life section tools stay in the chat, except "paid the rent" and "serviced the car" (ruling R6, design 2026-11-18)
EXCLUDED |= (assistant_tools.NEW_ADD_TOOLS | assistant_tools.NEW_CHANGE_TOOLS | {"get_section"}) - set(assistant_tools.RECORDING_ONLY)
TOOLS_FOR_ENTRY = [t for t in assistant.TOOLS if t["name"] not in EXCLUDED] + [ASK_USER, WITHDRAW, DROP_TODO]
MAX_ANSWERS = 3
ASK_AGAIN_AFTER = timedelta(days=7)  # "Not now" on the consent sheet: offered again a week later
ASKED = "Asked the user. Stop now; their answer comes back as the next message."
WITHDRAWN = "Withdrawn: it is off the card."
DROPPED = "Noted: that to-do comes off the card along with your change to the item."
LISTED_KINDS = ("task", "event")  # the existing items an extracted to-do can turn out to be


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
    if entry.proposal["result"].get("intent") == "ask" and entry.proposal.get("askable") and not entry.proposal.get("unasked"):
        return  # a question goes to the chat (design 2026-11-17): no AI actions, no AI use. Older apps can't ask, so they get them
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
    todos = [f"[{n}] {t['text']}" for n, t in enumerate(result.get("todos", []))]
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
    lines.append(await _their_items(user_id, ctx))
    return "\n".join(lines)


async def _their_items(user_id: str, ctx: assistant_tools.Ctx) -> str:
    """The user's habits, projects, goals, open tasks and nearby events, so the step can act on them
    without searching ("sent it", "push it to Thursday", "we cancelled" change one of these)."""
    habits = await db.habit.find_many(where={"userId": user_id, "isArchived": False}, order={"sortOrder": "asc"}, take=40)
    projects = await db.project.find_many(where={"userId": user_id, "workspaceId": None, "status": {"in": ["PLANNING", "ACTIVE", "ON_HOLD"]}}, take=40)
    goals = await db.goal.find_many(where={"userId": user_id, "status": {"in": ["ACTIVE", "PAUSED"]}}, take=40)

    def habit(h) -> str:
        target = f", {h.targetPerDay} {h.unit or 'times'} a day" if h.targetPerDay > 1 or h.unit else ""
        return f"- {h.name} (id {h.id}{target})"

    lines = ["<their_items>", "Habits:" if habits else "Habits: none", *map(habit, habits)]
    lines += ["Projects:" if projects else "Projects: none", *(f"- {p.name} (id {p.id}, {p.status.lower()})" for p in projects)]
    lines += ["Goals:" if goals else "Goals: none", *(f"- {g.name} (id {g.id})" for g in goals)]
    lines += await _tasks_and_events(ctx)
    lines.append("</their_items>")
    return "\n".join(lines)


async def _read(tool: str, i: dict, ctx: assistant_tools.Ctx):
    """A read tool's JSON as data, or None (refused, failed, or cut off for being too long)."""
    out = await assistant_tools.run(tool, i, ctx)
    if out.get("isError"):
        return None
    try:
        return json.loads(out["content"])
    except ValueError:
        return None


async def _open_tasks_and_events(ctx: assistant_tools.Ctx) -> tuple[list, list]:
    """Open tasks (the app's to-dos too, as list_tasks shows them) and calendar events from yesterday
    to two weeks ahead."""
    today = datetime.fromisoformat(ctx.today).date()
    tasks = ((await _read("list_tasks", {"status": "open", "limit": 30}, ctx)) or {}).get("items", [])[:30]
    agenda = await _read("get_agenda", {"from": (today - timedelta(days=1)).isoformat(),
                                        "to": (today + timedelta(days=14)).isoformat()}, ctx) or []
    return tasks, [e for e in agenda if e.get("type") == "event"][:20]


async def on_their_list(user) -> list[str]:
    """For extraction: the open tasks and events this step would see, as "title (when)" lines, so a
    to-do already there isn't extracted again. Empty when the step can't run for the user (not Plus,
    AI actions off): a move or a "done" would then change nothing, so extraction keeps its to-do."""
    if effective_plan(user) != "plus" or ai.client() is None:
        return []
    s = await ensure_os_profile(user)
    if not s.aiEnabled:
        return []
    tz = dates.valid_zone(s.timezone)
    ctx = assistant_tools.Ctx(user, tz, assistant_tools.local_day(datetime.now(timezone.utc), tz), "manual")
    tasks, events = await _open_tasks_and_events(ctx)
    return ([t["title"] + (f" (due {t['due']})" if t.get("due") else "") for t in tasks]
            + [f"{e['title']} ({e['start']}{', all day' if e.get('allDay') else ''})" for e in events])


async def _tasks_and_events(ctx: assistant_tools.Ctx) -> list[str]:
    """The open tasks and events (see _open_tasks_and_events) with ids and when; an event that has
    ended says so."""
    tasks, events = await _open_tasks_and_events(ctx)
    now = f"{ctx.today} {assistant_tools.local_time(datetime.now(timezone.utc), ctx.tz)}"

    def task(t) -> str:
        return f"- {t['title']} (id {t['id']}{', due ' + t['due'] if t.get('due') else ''})"

    def event(e) -> str:
        if e.get("allDay"):
            return f"- {e['title']} (id {e['id']}, {e['start']}, all day)"
        end = e["end"][11:] if e["end"][:10] == e["start"][:10] else e["end"]
        return f"- {e['title']} (id {e['id']}, {e['start']} to {end}{', already over' if e['end'] < now else ''})"

    return ["Open tasks:" if tasks else "Open tasks: none", *map(task, tasks),
            "Events, yesterday to two weeks ahead:" if events else "Events, yesterday to two weeks ahead: none", *map(event, events)]


async def _drop_todos(entry_id: str, drafts: list[dict], named: list[tuple[int, str]], ctx: assistant_tools.Ctx) -> None:
    """Takes off the card extraction's to-dos that are an existing task or event this run changes
    (moves, finishes, cancels): the ones the step named with drop_todo for that item, and any whose
    text is that item's title. A drop needs the change itself, so a to-do is never lost to a guess.

    Only before the card is first shown (the entry isn't DONE yet: the app shows the card and Save
    accepts it only once it is). After that the app holds the to-dos by index and keeps the user's
    ticks and edits across a re-run (an answer, consent, "unask"), so they never change then."""
    changed = {d["entityId"] for d in drafts if d.get("entityType") in LISTED_KINDS and d.get("entityId")}
    if not changed:
        return
    entry = await db.entry.find_unique(where={"id": entry_id})
    proposal = entry.proposal or {}
    result = proposal.get("result") or {}
    todos = result.get("todos") or []
    if entry.status == "DONE" or not todos:
        return
    drop = {n for n, item in named if item in changed and 0 <= n < len(todos)}
    tasks, events = await _open_tasks_and_events(ctx)
    titles = {_fold(x["title"]) for x in [*tasks, *events] if x.get("id") in changed and x.get("title")}
    drop |= {n for n, t in enumerate(todos) if _fold(str(t.get("text") or "")) in titles}
    if not drop:
        return
    await _set_proposal(entry_id, result={**result, "todos": [t for n, t in enumerate(todos) if n not in drop]},
                        droppedTodos=[*(proposal.get("droppedTodos") or []), *(todos[n] for n in sorted(drop))])


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
    ctx.recording = True  # the section tools it may use are limited to paying and maintenance done
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
    named: list[tuple[int, str]] = []  # drop_todo: (to-do number, the existing item's id)
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
                if use["name"] == "drop_todo":
                    i = use.get("input") if isinstance(use.get("input"), dict) else {}
                    if isinstance(i.get("todo"), int) and isinstance(i.get("item_id"), str):
                        named.append((i["todo"], i["item_id"]))
                    results.append({"type": "tool_result", "tool_use_id": use["id"], "content": DROPPED})
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
    try:
        await _drop_todos(entry.id, drafts, named, ctx)
    except Exception:  # noqa: BLE001 — the card keeps the to-do, as before
        log.exception("Dropping duplicate to-dos failed for entry %s", entry.id)
    answers = (entry.proposal or {}).get("answers", 0) + (1 if answer else 0)
    await _set_proposal(entry.id, question=question, answers=answers or None, skipped=None)
    await db.assistantconversation.update(where={"id": conversation.id}, data={"lastMessageAt": datetime.now(timezone.utc)})
