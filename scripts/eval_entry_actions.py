"""Score the app's recording -> AI actions step on evals/entry_actions/cases.jsonl.

Each case runs through the real recording path - main.process_transcript(..., use_merged=True,
review=True): merged extraction, then entry_actions.after_extraction proposing actions in Manual
mode - for a scratch Plus user seeded from evals/entry_actions/fixture.json (deleted at the end).
Nothing is applied: the grade reads the entry's proposal and its waiting AssistantActions.

Run only through the guarded local runner (only the AI keys come from .env, local database):

    python scripts/dev/isolated_backend.py eval-entry-actions --fake oracle    # free: wiring check, ~100%
    python scripts/dev/isolated_backend.py eval-entry-actions --fake null      # free: wiring check, low
    python scripts/dev/isolated_backend.py eval-entry-actions --yes --limit 8  # paid pilot
    python scripts/dev/isolated_backend.py eval-entry-actions --yes            # paid full pass

Writes .claude/hillclimb/entry-actions/<variant>/results.jsonl, traces/<id>_rep<k>.json and
errors.jsonl, one row per (case, rep) as it finishes; a rerun skips what is already written.
Paid runs refuse to start until the person who owns the eval approves the harness once
(--approve-harness), and again whenever the runner, cases or fixture change."""

import argparse
import asyncio
import contextvars
import hashlib
import json
import math
import sys
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

EVAL = ROOT / "evals" / "entry_actions"
FLOW = ROOT / ".claude" / "hillclimb" / "entry-actions"
CASES = [json.loads(line) for line in (EVAL / "cases.jsonl").read_text().splitlines() if line.strip()]
FIXTURE = json.loads((EVAL / "fixture.json").read_text())
HARNESS = [Path(__file__), EVAL / "cases.jsonl", EVAL / "fixture.json"]
# $ per million tokens (input, output); cache writes 1.25x input, reads 0.1x
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0)}
TEXT_FIELDS = ("title", "name", "notes", "description", "occasion", "who")
CALLS: contextvars.ContextVar[list] = contextvars.ContextVar("calls")
CASE: contextvars.ContextVar[dict] = contextvars.ContextVar("case")


def _args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", default="baseline", help="output folder: baseline or v<N>")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--timeout-s", type=float, default=240.0, help="hard wall-clock ceiling per case")
    p.add_argument("--ids", default="", help="comma-separated case ids")
    p.add_argument("--limit", type=int, default=0, help="first N cases, stratified across categories")
    p.add_argument("--fake", choices=("oracle", "null", "error"), help="no model calls: scripted answers (or a failing API) to check the wiring")
    p.add_argument("--yes", action="store_true", help="really call the paid API")
    p.add_argument("--approve-harness", action="store_true", help="for the eval's owner: record the current harness")
    return p.parse_args(sys.argv[1:])


# ----- the harness gate ------------------------------------------------------------------


def _harness_sha() -> str:
    h = hashlib.sha256()
    for path in HARNESS:
        h.update(path.name.encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


def _gate(approve: bool) -> None:
    mark = FLOW / ".harness_sha"
    sha = _harness_sha()
    if approve:
        FLOW.mkdir(parents=True, exist_ok=True)
        mark.write_text(sha + "\n")
        print(f"harness approved ({sha[:12]})")
        return
    if not mark.exists() or mark.read_text().strip() != sha:
        sys.exit("The runner, cases or fixture changed since the harness was last approved. "
                 "Review them, then run once with --approve-harness.")


# ----- dates and text --------------------------------------------------------------------


def _fold(text) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = "".join(ch for ch in text if not ("ً" <= ch <= "ٟ" or ch in "ٰـ"))
    return " ".join(text.split())


def _day_token(token: str, today: date) -> str:
    """{today} / {yesterday} / {today-N} -> YYYY-MM-DD; anything else unchanged."""
    if token == "{today}":
        return today.isoformat()
    if token == "{yesterday}":
        return (today - timedelta(days=1)).isoformat()
    if token.startswith("{today-") and token.endswith("}"):
        return (today - timedelta(days=int(token[7:-1]))).isoformat()
    return token


def _date_ok(expected: str, got, today: date) -> bool:
    if not isinstance(got, str):
        return False
    for option in expected.split("|"):
        option = _day_token(option, today)
        if option.startswith("*"):
            if got[4:] == option[1:]:
                return True
        elif got[:10] == option:
            return True
    return False


# ----- grading ---------------------------------------------------------------------------


def _proposed(actions: list, people: list[str]) -> list[dict]:
    """The waiting changes as {tool, input}; a delete carries its target as input."""
    out = []
    for a in actions:
        call = a.call if isinstance(a.call, dict) else None
        if call:
            out.append({"tool": call["name"], "input": call.get("input") or {}, "id": a.id})
        elif a.kind == "delete":
            out.append({"tool": "delete_item", "input": {"type": a.entityType, "id": a.entityId}, "id": a.id})
    for p in out:
        p["people"] = people
    return out


def _ref_ok(expected: str, got, refs: dict, people: list[str]) -> bool:
    for option in expected.split("|"):
        if option.startswith("@") and got == refs.get(option):
            return True
        if option.startswith("new:") and isinstance(got, str) and got.startswith("proposal-person:"):
            index = int(got.split(":", 1)[1])
            if index < len(people) and _fold(people[index]) == _fold(option[4:]):
                return True
    return False


def _matches(exp: dict, prop: dict, refs: dict, today: date) -> bool:
    if prop["tool"] != exp["tool"]:
        return False
    got = prop["input"]
    for key, want in exp.get("fields", {}).items():
        value = got.get(key)
        if isinstance(want, bool):
            if value is not want:
                return False
        elif isinstance(want, (int, float)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or abs(value - want) > 0.01:
                return False
        elif isinstance(want, str) and (want.startswith("@") or want.startswith("new:")):
            if not _ref_ok(want, value, refs, prop["people"]):
                return False
        elif key in ("date", "deadline", "birthday", "due_date"):
            if not _date_ok(want, value, today):
                return False
        elif key == "currency":
            if _fold(value) != _fold(want):
                return False
        elif _fold(value) != _fold(want):
            return False
    # unstated: today's date, the user's currency
    if "date" not in exp.get("fields", {}) and got.get("date") not in (None, "", today.isoformat()) and prop["tool"] in ("add_transaction", "log_habit"):
        return False
    if prop["tool"] == "add_transaction" and "currency" not in exp.get("fields", {}) and got.get("currency") not in (None, "", FIXTURE["currency"]):
        return False
    if exp.get("title"):
        text = _fold(" ".join(str(got.get(k) or "") for k in TEXT_FIELDS))
        if not any(_fold(t) in text for t in exp["title"]):
            return False
    return True


def grade(case: dict, proposed: list[dict], asked: bool, wants: bool, refs: dict, today: date) -> tuple[dict, dict]:
    expect = case["expect"]
    left = list(proposed)
    matched, missing = [], []
    for exp in expect["actions"]:
        hit = next((p for p in left if any(_matches(e, p, refs, today) for e in [exp, *exp.get("alt", [])])), None)
        if hit:
            left.remove(hit)
            matched.append(hit)
        else:
            missing.append(exp)
    extra = [p for p in left if not any(_matches(o, p, refs, today) for o in expect.get("optional", []))]
    want_q = expect["question"]
    asked_ok = asked if want_q == "yes" else (not asked) if want_q == "no" else True
    n_exp = len(expect["actions"])
    recall = len(matched) / n_exp if n_exp else 1.0
    counted = len(matched) + len(extra)
    precision = len(matched) / counted if counted else 1.0
    ok = not missing and not extra and asked_ok
    scores = {"pass": 1.0 if ok else 0.0, "recall": recall, "precision": precision,
              "gate": 1.0 if wants == expect["wantsActions"] else 0.0, "asked": 1.0 if asked_ok else 0.0}
    detail = {"missing": missing, "extra": [{"tool": p["tool"], "input": p["input"]} for p in extra]}
    return scores, detail


# ----- the scripted model (--fake) ------------------------------------------------------


def _oracle_input(exp: dict, refs: dict, people: list[str], today: date) -> dict:
    out = {}
    for key, want in exp.get("fields", {}).items():
        if isinstance(want, str) and "|" in want:
            want = want.split("|")[0]
        if isinstance(want, str) and want.startswith("@"):
            want = refs[want]
        elif isinstance(want, str) and want.startswith("new:"):
            want = f"proposal-person:{[_fold(p) for p in people].index(_fold(want[4:]))}"
        elif isinstance(want, str) and key in ("date", "deadline", "birthday"):
            want = _day_token(want, today).replace("*", str(today.year))
        out[key] = want
    title_key = {"add_transaction": "title", "add_gift": "title", "add_important_date": "title", "update_person": "notes",
                 "log_food": "name"}.get(exp["tool"], "name")
    if exp.get("title"):
        out.setdefault(title_key, exp["title"][0])
    if exp["tool"] == "log_food":
        out.setdefault("calories", 300)
    if exp["tool"] == "create_debt":
        out.setdefault("who", "someone")
    if exp["tool"] == "delete_item":
        out.setdefault("label", "it")
    return out


def _fake_people(case: dict) -> list[str]:
    names = []
    for exp in case["expect"]["actions"]:
        for value in exp.get("fields", {}).values():
            if isinstance(value, str):
                for option in value.split("|"):
                    if option.startswith("@person:"):
                        names.append(option.split(":", 1)[1])
                        break
                    if option.startswith("new:"):
                        names.append(option[4:])
                        break
    return list(dict.fromkeys(names))


def _fake_extract(mode: str):
    async def extract(transcript, now=None, known=None, **_):
        case = CASE.get()
        result = {"summary": transcript[:200], "mood": {"score": 3, "label": "neutral", "emotions": []}, "todos": [],
                  "topics": [], "people": _fake_people(case), "places": [],
                  "wantsActions": case["expect"]["wantsActions"] if mode == "oracle" else bool(case["expect"]["actions"]),
                  "journalSuggested": True}
        return result, {"inTokens": 0, "outTokens": 0}
    return extract


def _fake_call(mode: str, refs: dict, today: date):
    async def call(client, round_, messages, effort, system=None, tools=None):
        from app.os import ai

        case = CASE.get()
        if mode == "error":
            raise RuntimeError("scripted API failure")
        def reply(content, stop):
            return {"content": content, "stop_reason": stop, "model": ai.MODEL, "usage": {"input_tokens": 0, "output_tokens": 0}}
        if mode == "null" or round_ > 1:
            return reply([{"type": "text", "text": "Done."}], "end_turn")
        people = _fake_people(case)
        uses = [{"type": "tool_use", "id": f"toolu_{n}", "name": e["tool"], "input": _oracle_input(e, refs, people, today)}
                for n, e in enumerate(case["expect"]["actions"])]
        if case["expect"]["question"] == "yes":
            uses.append({"type": "tool_use", "id": "toolu_q", "name": "ask_user", "input": {"question": "Which one?", "allowText": True}})
        return reply(uses, "tool_use") if uses else reply([{"type": "text", "text": "Nothing to do."}], "end_turn")
    return call


# ----- recording what the real model did ------------------------------------------------


def _recording_call(original):
    async def call(*args, **kwargs):
        res = await original(*args, **kwargs)
        CALLS.get().append({"step": "actions", "model": res.get("model"), "usage": res.get("usage") or {},
                            "stop_reason": res.get("stop_reason"), "effort": args[3] if len(args) > 3 else kwargs.get("effort")})
        return res
    return call


def _recording_model_call(original):
    async def call(client, **request):
        response = await original(client, **request)
        usage = getattr(response, "usage", None)
        CALLS.get().append({"step": "extraction", "model": getattr(response, "model", None), "stop_reason": getattr(response, "stop_reason", None),
                            "usage": {k: getattr(usage, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}})
        return response
    return call


def _cost(calls: list) -> float:
    total = 0.0
    for c in calls:
        price_in, price_out = PRICES.get(c.get("model") or "", (0.0, 0.0))
        u = c.get("usage") or {}
        total += (u.get("input_tokens", 0) * price_in + u.get("output_tokens", 0) * price_out
                  + u.get("cache_creation_input_tokens", 0) * price_in * 1.25 + u.get("cache_read_input_tokens", 0) * price_in * 0.1) / 1e6
    return round(total, 6)


def _trace(system: str, extraction: dict, history: list) -> list:
    turns = [{"role": "system", "content": system}]
    turns.append({"role": "assistant", "name": "extraction", "content": "[extraction]\n" + json.dumps(extraction, ensure_ascii=False, indent=2)})
    for h in history:
        for block in h.content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                turns.append({"role": h.role, "content": block["text"]})
            elif kind == "tool_use":
                turns.append({"role": "tool_call", "name": block.get("name"), "content": json.dumps(block.get("input"), ensure_ascii=False, indent=2)})
            elif kind == "tool_result":
                content = block.get("content")
                turns.append({"role": "tool_result", "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
    return turns


# ----- seeding ---------------------------------------------------------------------------


async def _seed(db) -> tuple:
    from app import security
    from app.entities import normalize_name
    from app.os import assistant_tools as tools
    from app.os.profile import ensure_os_profile

    user = await db.user.create(data={"email": f"eval-entry-actions-{uuid.uuid4().hex}@example.test", "plan": "plus",
                                      "passwordHash": security.hash_password(uuid.uuid4().hex), "timezone": FIXTURE["timezone"]})
    await ensure_os_profile(user)
    await db.usersettings.update(where={"userId": user.id}, data={"timezone": FIXTURE["timezone"], "defaultCurrency": FIXTURE["currency"],
                                                                  "aiEnabled": True, "assistantMode": "manual"})
    tz = FIXTURE["timezone"]
    today = datetime.now(ZoneInfo(tz)).date()
    ctx = tools.Ctx(user, tz, today.isoformat(), "auto")
    refs: dict[str, str] = {}

    async def make(tool: str, data: dict) -> str:
        out = await tools.run(tool, data, ctx)
        if not out.get("action"):
            raise RuntimeError(f"seeding {tool} failed: {out['content']}")
        return out["action"]["entityId"]

    seed_entry = await db.entry.create(data={"userId": user.id, "rawTranscript": "seed", "summary": "seed", "status": "DONE", "reviewState": "saved"})
    for p in FIXTURE["people"]:
        row = await db.person.create(data={"userId": user.id, "name": p["name"], "normalizedName": normalize_name(p["name"]),
                                           "relationship": p.get("relationship"), "notes": p.get("notes")})
        refs[f"@person:{p['name']}"] = row.id
        await db.entryperson.create(data={"entryId": seed_entry.id, "personId": row.id})
    for h in FIXTURE["habits"]:
        refs[f"@habit:{h['name']}"] = await make("create_habit", {"name": h["name"], **({"target_per_day": h["targetPerDay"]} if "targetPerDay" in h else {}),
                                                                   **({"unit": h["unit"]} if "unit" in h else {})})
    for p in FIXTURE["projects"]:
        refs[f"@project:{p['name']}"] = await make("create_project", {"name": p["name"]})
    for g in FIXTURE["goals"]:
        refs[f"@goal:{g['name']}"] = await make("create_goal", {"name": g["name"]})
    for t in FIXTURE["transactions"]:
        refs[f"@transaction:{t['key']}"] = await make("add_transaction", {"type": t["type"].lower(), "amount": t["amount"], "title": t["title"],
                                                                        "date": (today - timedelta(days=t["daysAgo"])).isoformat()})
    for d in FIXTURE["debts"]:
        refs[f"@debt:{d['key']}"] = await make("create_debt", {"direction": d["direction"].lower(), "who": d["person"], "amount": d["amount"],
                                                              "title": d["title"], "person_id": refs[f"@person:{d['person']}"]})
    return user, refs, today


# ----- running ---------------------------------------------------------------------------


def _pick(args) -> list[dict]:
    cases = CASES
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        by_tag: dict[str, list] = {}
        for c in cases:
            by_tag.setdefault(c["tags"][0], []).append(c)
        picked, i = [], 0
        while len(picked) < min(args.limit, len(cases)):
            for group in by_tag.values():
                if i < len(group) and len(picked) < args.limit:
                    picked.append(group[i])
            i += 1
        cases = picked
    return cases


async def _run_one(db, case: dict, rep: int, user, refs: dict, today: date, out: Path, timeout: float):
    from app import main
    from app.entry_actions import SYSTEM

    calls: list = []
    CALLS.set(calls)
    CASE.set(case)
    entry = await db.entry.create(data={"userId": user.id, "rawTranscript": case["text"], "source": "VOICE", "status": "PROCESSING"})
    started = time.monotonic()
    try:
        await asyncio.wait_for(main.process_transcript(entry.id, case["text"], use_merged=True, review=True), timeout)
    except asyncio.TimeoutError:
        return {"error": "timeout", "calls": calls}
    latency = round(time.monotonic() - started, 2)
    entry = await db.entry.find_unique(where={"id": entry.id})
    proposal = entry.proposal or {}
    if entry.status == "FAILED":
        return {"error": f"extraction failed: {entry.error}", "calls": calls}
    if proposal.get("skipped") in ("unavailable", "limit"):
        return {"error": f"actions step {proposal['skipped']}", "calls": calls}
    result = proposal.get("result") or {}
    people = result.get("people", [])
    waiting = await db.assistantaction.find_many(where={"entryId": entry.id, "status": "pending"}, order={"position": "asc"})
    proposed = _proposed(waiting, people)
    asked = bool(proposal.get("question"))
    wants = bool(result.get("wantsActions"))
    scores, detail = grade(case, proposed, asked, wants, refs, today)
    conversation = await db.assistantconversation.find_first(where={"entryId": entry.id})
    history = await db.assistantmessage.find_many(where={"conversationId": conversation.id}, order=[{"createdAt": "asc"}, {"id": "asc"}]) if conversation else []
    truncated = any(c.get("stop_reason") == "max_tokens" for c in calls)
    usage = {k: sum((c.get("usage") or {}).get(k, 0) or 0 for c in calls)
             for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    tool_calls = sum(1 for h in history if h.role == "assistant" for b in (h.content or []) if isinstance(b, dict) and b.get("type") == "tool_use")
    models = [c.get("model") for c in calls if c.get("step") == "actions"]
    row = {
        "prompt_id": case["id"], "rep": rep, "prompt": case["text"], "tags": case["tags"],
        "status": "truncated" if truncated else "ok", "stop_reason": (calls[-1].get("stop_reason") if calls else None),
        "grade": scores, "latency_s": latency, "tool_calls": tool_calls, "questions": 1 if asked else 0,
        "model": models[0] if models else (calls[0].get("model") if calls else None), "usage": usage,
        "meta": {"cost_usd": _cost(calls), "calls": calls, "refs": refs, "today": today.isoformat(), "wantsActions": wants, "question": proposal.get("question"),
                 "proposed": [{"tool": p["tool"], "input": p["input"]} for p in proposed], "people": people, **detail},
    }
    (out / "traces").mkdir(parents=True, exist_ok=True)
    (out / "traces" / f"{case['id']}_rep{rep}.json").write_text(json.dumps(_trace(SYSTEM, result, history), ensure_ascii=False, indent=1))
    return row


def _wilson(k: float, n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p, z = k / n, 1.96
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def _write_state() -> None:
    state = FLOW / "_state.json"
    if state.exists():
        return
    state.write_text(json.dumps({
        "metrics": [
            {"id": "pass", "label": "Pass", "kind": "binary"},
            {"id": "recall", "label": "Found", "kind": "float", "scale": 1},
            {"id": "precision", "label": "No extras", "kind": "float", "scale": 1},
            {"id": "gate", "label": "Needs-actions", "kind": "binary"},
            {"id": "asked", "label": "Asked right", "kind": "binary"},
        ],
        "perf_fields": [{"id": "cost_usd", "label": "Cost", "unit": "$"}, {"id": "latency_s", "label": "Time", "unit": "s"},
                        {"id": "tool_calls", "label": "Tool calls"}, {"id": "questions", "label": "Questions"}],
        "harness_paths": [str(p.relative_to(ROOT)) for p in HARNESS],
    }, indent=2) + "\n")


async def main() -> None:
    args = _args()
    if args.approve_harness:
        _gate(True)
        return
    if not args.fake and not args.yes:
        sys.exit("This calls the paid API: add --yes (or use --fake oracle|null for a free wiring check).")
    if args.yes:
        _gate(False)
    variant = args.variant if not args.fake else f"fake-{args.fake}"
    out = FLOW / variant
    out.mkdir(parents=True, exist_ok=True)
    _write_state()
    results, errors = out / "results.jsonl", out / "errors.jsonl"
    done = set()
    if results.exists():
        for line in results.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["prompt_id"], r["rep"]))

    from app import extraction_merged
    from app.db import db
    from app.os import ai, assistant

    patches = [patch.object(ai, "MONTHLY_LIMIT", 10**9)]
    await db.connect()
    try:
        user, refs, today = await _seed(db)
    except Exception:
        await db.user.delete_many(where={"email": {"startswith": "eval-entry-actions-"}})
        await db.disconnect()
        raise
    if args.fake:
        patches += [patch("app.main.extract_merged", _fake_extract(args.fake)), patch.object(ai, "_client", object()),
                    patch.object(assistant, "_call", _fake_call(args.fake, refs, today))]
    else:
        patches += [patch.object(assistant, "_call", _recording_call(assistant._call)),
                    patch.object(extraction_merged, "call_model", _recording_model_call(extraction_merged.call_model))]
        if ai.client() is None:
            sys.exit("No ANTHROPIC_API_KEY: run through isolated_backend.py eval-entry-actions.")
    for p in patches:
        p.start()

    todo = [(c, rep) for c in _pick(args) for rep in range(args.reps) if (c["id"], rep) not in done]
    print(f"{len(todo)} runs to do ({len(done)} already written) -> {out.relative_to(ROOT)}", flush=True)
    gate = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    started = time.monotonic()

    async def one(case, rep):
        async with gate:
            try:
                row = await _run_one(db, case, rep, user, refs, today, out, args.timeout_s)
            except Exception as exc:  # noqa: BLE001 — a harness failure is recorded, never scored
                row = {"error": f"{type(exc).__name__}: {exc}", "calls": CALLS.get([])}
            async with lock:
                if "error" in row:
                    with errors.open("a") as f:
                        f.write(json.dumps({"prompt_id": case["id"], "rep": rep, "failure": row["error"], "calls": row.get("calls")}, ensure_ascii=False) + "\n")
                    print(f"  ERROR {case['id']}: {row['error']}", flush=True)
                else:
                    with results.open("a") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    mark = "pass" if row["grade"]["pass"] else "FAIL"
                    print(f"  {mark} {case['id']}  ${row['meta']['cost_usd']:.4f}  {row['latency_s']}s", flush=True)

    try:
        await asyncio.gather(*(one(c, rep) for c, rep in todo))
    finally:
        for p in patches:
            p.stop()
        await db.user.delete(where={"id": user.id})
        await db.disconnect()

    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    k = sum(r["grade"]["pass"] for r in ok)
    lo, hi = _wilson(k, len(ok))
    mean = lambda key: sum(r["grade"][key] for r in ok) / len(ok) if ok else 0.0  # noqa: E731
    cost = sum(r["meta"]["cost_usd"] for r in rows)
    n_err = len(errors.read_text().splitlines()) if errors.exists() else 0
    print(f"\n{variant}: pass {k:.0f}/{len(ok)} = {k / len(ok) if ok else 0:.1%} (95% CI {lo:.1%}-{hi:.1%}) · found {mean('recall'):.1%} · "
          f"no extras {mean('precision'):.1%} · needs-actions {mean('gate'):.1%} · asked right {mean('asked'):.1%}")
    print(f"{len(rows) - len(ok)} truncated · {n_err} errors · cost ${cost:.4f} · {time.monotonic() - started:.0f}s wall-clock")


if __name__ == "__main__":
    asyncio.run(main())
