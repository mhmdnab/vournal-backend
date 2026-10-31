#!/usr/bin/env python3
"""Score the extraction pipeline on a labelled set of realistic entries.

Runs the REAL extraction functions (merged/Sonnet for spoken cases, plain/Haiku
for typed ones), then the same task guards and question rules the backend uses,
and scores dueDate / time / urgency / location / repeat / kind / questions per
language. Touches no database.

Paid runs go through the guarded local runner, which supplies only the AI keys:
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py eval --dry-run
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py eval --yes

Re-score saved outputs against edited labels without calling the API:
    .../isolated_backend.py eval --rescore evals/extraction/results/<file>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "extraction" / "cases.jsonl"
RESULTS = ROOT / "evals" / "extraction" / "results"
FIELDS = ("dueDate", "time", "urgency", "location", "recurrence", "kind")
# $ per million tokens (input, output) — Anthropic list prices.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
EST_IN, EST_OUT = 3200, 350  # per case, from production averages


# ---- scoring (pure) -------------------------------------------------------------


def _fold(text) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = "".join(ch for ch in text if not ("ً" <= ch <= "ٟ" or ch in "ٰـ"))
    return " ".join(text.split())


def _hits(expected: dict, todo: dict) -> int:
    text = _fold(todo.get("text"))
    return sum(1 for word in expected["match"] if _fold(word) in text)


def _field_ok(field: str, want, todo: dict) -> bool:
    if field == "urgency":
        if want == "ask":
            return bool(todo.get("urgencyUnclear"))
        return not todo.get("urgencyUnclear") and todo.get("urgency") == want
    if field == "location":
        got = todo.get("location")
        if want is None or got is None:
            return want is None and got is None
        return _fold(want) in _fold(got) or _fold(got) in _fold(want)
    if field == "recurrence":
        if want is None:
            return not todo.get("recurrence") and not todo.get("recurrenceUnclear")
        if want == "ask":
            return bool(todo.get("recurrenceUnclear"))
        if todo.get("recurrence") != want.get("freq"):
            return False
        days = want.get("weekdays")
        if days is not None and sorted(todo.get("recurrenceWeekdays") or []) != sorted(days):
            return False
        interval = want.get("interval")
        return interval is None or todo.get("recurrenceInterval") == interval
    return todo.get(field) == want


def score_case(case: dict, todos: list[dict]) -> dict:
    """Match each expected to-do to the extracted one sharing the most keywords,
    then score its fields. Unmatched expected to-dos score zero on every field."""
    fields = {f: [0, 0] for f in FIELDS}
    unused = list(range(len(todos)))
    missing = 0
    for expected in case["expect"]["todos"]:
        best = max(unused, key=lambda i: _hits(expected, todos[i]), default=None)
        todo = todos[best] if best is not None and _hits(expected, todos[best]) > 0 else None
        if todo is None:
            missing += 1
        else:
            unused.remove(best)
        for field in FIELDS:
            want = expected.get(field)
            if want == "*":
                continue
            fields[field][1] += 1
            if todo is not None and _field_ok(field, want, todo):
                fields[field][0] += 1
    asked = any(todo.get("questions") for todo in todos)
    return {
        "fields": fields,
        "questions": asked == case["expect"]["questions"],
        "missing": missing,
        "extra": len(unused),
        "expected": len(case["expect"]["todos"]),
    }


def summarize(results: list[dict]) -> dict:
    """{lang: {field: % correct, "questions": %, "recall": % of to-dos found}}."""
    table: dict = {}
    for lang in sorted({r["lang"] for r in results}):
        rows = [r["score"] for r in results if r["lang"] == lang]
        out = {}
        for field in FIELDS:
            ok = sum(s["fields"][field][0] for s in rows)
            total = sum(s["fields"][field][1] for s in rows)
            out[field] = round(100.0 * ok / total, 1) if total else None
        out["questions"] = round(100.0 * sum(s["questions"] for s in rows) / len(rows), 1)
        expected = sum(s["expected"] for s in rows)
        found = expected - sum(s["missing"] for s in rows)
        out["recall"] = round(100.0 * found / expected, 1) if expected else 100.0
        out["extra"] = sum(s["extra"] for s in rows)
        table[lang] = out
    return table


# ---- running (paid) ---------------------------------------------------------------


def load_cases(path: pathlib.Path, only: str | None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [c for c in cases if not only or c["lang"] == only]


def _models(typed_model: str | None) -> tuple[str, str]:
    from app.extraction import DEFAULT_MODEL
    from app.extraction_merged import DEFAULT_MERGED_MODEL

    typed = typed_model or os.environ.get("EXTRACTION_MODEL") or DEFAULT_MODEL
    spoken = os.environ.get("MERGED_MODEL") or DEFAULT_MERGED_MODEL
    return typed, spoken


def estimate(cases: list[dict], typed_model: str | None) -> float:
    typed, spoken = _models(typed_model)
    cost = 0.0
    for case in cases:
        p_in, p_out = PRICES.get(spoken if case["mode"] == "spoken" else typed, (2.0, 10.0))
        cost += (EST_IN * p_in + EST_OUT * p_out) / 1e6
    return cost


def _pipeline_todos(result: dict, transcript: str, tz, today=None) -> list[dict]:
    from app.main import _compose_start_at
    from app.task_intelligence import first_occurrence_day, normalize_metadata, questions, verified_day

    todos = []
    for item in result.get("todos", []):
        due, time = verified_day(item, transcript, today)  # same checks as process_transcript
        meta = normalize_metadata(item, transcript, tz)
        if not due and today:
            first = first_occurrence_day(meta["recurrence"], meta["recurrenceWeekdays"], today)
            due = first.isoformat() if first else None
        start = _compose_start_at(due, time, tz)
        task = SimpleNamespace(text=item["text"], dueDate=due, startAt=start,
                               dismissedQuestions=[], **meta)
        todos.append({
            "text": item["text"],
            "dueDate": due,
            "time": time,
            **{k: v for k, v in meta.items() if k != "recurrenceUntil"},
            "questions": [q["field"] for q in questions(task)],
        })
    return todos


async def _run_case(case: dict, typed_model: str | None) -> dict:

    from app.extraction import extract
    from app.extraction_merged import extract_merged

    now = datetime.fromisoformat(case["now"])
    tz = ZoneInfo(case.get("tz", "Asia/Beirut"))
    typed, spoken = _models(typed_model)
    try:
        if case["mode"] == "spoken":
            result, usage = await extract_merged(case["transcript"], now=now, model=spoken)
            model = spoken
        else:
            os.environ["EXTRACTION_MODEL"] = typed
            result, usage = await extract(case["transcript"], now=now)
            model = typed
        error = None
    except Exception as exc:  # truncated/refused/API errors are results too
        result, usage, model, error = {"todos": []}, None, typed if case["mode"] == "typed" else spoken, repr(exc)
    todos = _pipeline_todos(result, case["transcript"], tz, now.date())
    return {"id": case["id"], "lang": case["lang"], "mode": case["mode"], "model": model,
            "error": error, "raw": result, "todos": todos, "usage": usage,
            "score": score_case(case, todos)}


def _cost(rows: list[dict]) -> float:
    total = 0.0
    for row in rows:
        if row.get("usage"):
            p_in, p_out = PRICES.get(row["model"], (2.0, 10.0))
            usage = row["usage"]
            total += (
                usage["inTokens"] * p_in
                + usage.get("cacheWriteTokens", 0) * p_in * 1.25  # 5-minute cache write
                + usage.get("cacheReadTokens", 0) * p_in * 0.1  # cache read
                + usage["outTokens"] * p_out
            ) / 1e6
    return total


def print_table(table: dict) -> None:
    cols = (*FIELDS, "questions", "recall", "extra")
    print("lang  " + "  ".join(f"{c:>10}" for c in cols))
    for lang, row in table.items():
        cells = ["      -" if row[c] is None else f"{row[c]:>10}" for c in cols]
        print(f"{lang:<5} " + "  ".join(f"{c:>10}" for c in cells))


def write_review(cases: list[dict], path: pathlib.Path) -> None:
    """An owner-readable table of every case and its expected answers."""
    lines = ["# Extraction evaluation — expected answers", "",
             "Reference time: given per case (development set: Wednesday 28 October 2026, 09:00; held-out set: Saturday 31 October 2026, 19:30, Asia/Beirut).",
             "Check each expected answer; `*` means the field isn't scored, `ask` means the app should ask.", ""]
    for case in cases:
        lines += [f"## {case['id']} ({case['lang']}, {case['mode']})", "",
                  f"> {case['transcript']}", ""]
        if case.get("gloss"):
            lines += [f"*{case['gloss']}*", ""]
        todos = case["expect"]["todos"]
        if not todos:
            lines += ["Expected: **no to-dos**.", ""]
        else:
            lines += ["| to-do (keywords) | day | time | urgency | place | repeat | kind |",
                      "|---|---|---|---|---|---|---|"]
            for t in todos:
                rec = t["recurrence"]
                rec = "—" if rec is None else rec if isinstance(rec, str) else (
                    rec["freq"] + (" " + ",".join(rec["weekdays"]) if rec.get("weekdays") else ""))
                lines.append(f"| {' / '.join(t['match'])} | {t['dueDate'] or '—'} | {t['time'] or '—'} "
                             f"| {t['urgency']} | {t['location'] or '—'} | {rec} | {t['kind']} |")
            lines.append("")
        lines += [f"Should the app ask a question? **{'yes' if case['expect']['questions'] else 'no'}**", ""]
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=pathlib.Path, default=CASES)
    parser.add_argument("--only", help="en | ar | lb | az")
    parser.add_argument("--typed-model")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--rescore", type=pathlib.Path)
    parser.add_argument("--repipe", action="store_true", help="with --rescore: rerun the code guards on saved replies")
    parser.add_argument("--review", type=pathlib.Path, help="write the owner review sheet here")
    parser.add_argument("--errors-from", type=pathlib.Path,
                        help="rerun only the cases that errored in this results file, and merge into it")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases, args.only)
    merge_into = None
    if args.errors_from:
        merge_into = json.loads(args.errors_from.read_text())
        failed = {r["id"] for r in merge_into["rows"] if r["error"]}
        cases = [c for c in cases if c["id"] in failed]
    if args.review:
        write_review(cases, args.review)
        print(f"Wrote {args.review} ({len(cases)} cases)")
        return 0
    if args.rescore:
        saved = json.loads(args.rescore.read_text())
        by_id = {c["id"]: c for c in cases}
        rows = []
        for r in saved["rows"]:
            if r["id"] not in by_id:
                continue
            case = by_id[r["id"]]
            if args.repipe:  # rerun today's guards on the saved model replies (free)
                r = dict(r, todos=_pipeline_todos(r["raw"], case["transcript"],
                                                  ZoneInfo(case.get("tz", "Asia/Beirut")),
                                                  datetime.fromisoformat(case["now"]).date()))
            rows.append(dict(r, score=score_case(case, r["todos"])))
        print_table(summarize(rows))
        return 0

    estimated = estimate(cases, args.typed_model)
    print(f"{len(cases)} cases; estimated cost ${estimated:.2f}")
    if args.dry_run:
        return 0
    if not args.yes:
        print("Paid run: add --yes to call the API.")
        return 2

    async def run_all():
        return [await _run_case(case, args.typed_model) for case in cases]

    rows = asyncio.run(run_all())
    if merge_into is not None:
        fresh = {r["id"]: r for r in rows}
        rows = [fresh.get(r["id"], r) for r in merge_into["rows"]]
        args.out = args.out or args.errors_from
    table = summarize(rows)
    print_table(table)
    failed = [r["id"] for r in rows if r["error"]]
    print(f"errors: {failed or 'none'}")
    print(f"measured cost ${_cost(rows):.2f}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = args.out or RESULTS / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps({"summary": table, "cost": _cost(rows), "rows": rows},
                              ensure_ascii=False, indent=1, default=str))
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
