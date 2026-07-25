"""Phase 3 gate — merged repair+extraction vs the current two-call pipeline.

For every case, runs:
    CURRENT:  transcript -> repair (Sonnet) -> extraction (Haiku)   [two calls]
    MERGED :  transcript -> extract_merged (Sonnet)                 [one call]
and prints the todos side by side, a restraint/fabrication check, and the exact
cost of each (measured from token usage).

Two groups of cases:
  A) the full test_extraction suite — the DISCRIMINATING + Arabizi-time cases.
     The gate here is restraint: merged must not invent a todo, a clock time, a
     person, or a date that the current pipeline (correctly) does not.
  B) the Melia raw dialect transcripts (from the Phase 2 run) — the gate here is
     that merged still RECOVERS the meaning (جدة -> "renew passport") the way the
     two-call pipeline does.

You read the todos and judge quality; the FABRICATION flag only auto-catches the
hard-gate failure — merged being LESS restrained than the current pipeline.

Requires ANTHROPIC_API_KEY. Usage:  python test_merged_extraction.py
"""

import asyncio
import os

from dotenv import load_dotenv
from anthropic import AsyncAnthropic

from test_extraction import SAMPLES, NOW
from app.extraction import (
    SYSTEM_PROMPT as EXTRACT_SYS,
    DEFAULT_MODEL as EXTRACT_DEFAULT,
    _format_now,
    _normalize,
    _strip_and_parse,
    _user_message,
)
from app.extraction_merged import MERGED_SYSTEM_PROMPT, DEFAULT_MERGED_MODEL
from app.repair import (
    SYSTEM_PROMPT as REPAIR_SYS,
    DEFAULT_REPAIR_MODEL,
    MIN_CHARS_TO_REPAIR,
    MIN_LENGTH_RATIO,
    MAX_LENGTH_RATIO,
    _extract_text,
)

PRICE_HAIKU = (1.0, 5.0)
PRICE_SONNET5 = (2.0, 10.0)  # introductory through 2026-08-31

# Melia's raw transcripts from the Phase 2 benchmark — the dialect-recovery gate.
DIALECT_CASES = [
    ("Melia raw — passport (جدة dialect error)", "ب 20 تموز، مطلوب انزل جدة باسبوري"),
    ("Melia raw — dentist", "بعد بكرة بدي انزل عند حكيم السنان."),
    ("Melia raw — code-switched", "بكرة I wanna go to the طبيب الأسنان."),
]


def _price(model: str) -> tuple[float, float]:
    m = model.lower()
    return PRICE_HAIKU if "haiku" in m else PRICE_SONNET5 if "sonnet" in m else (0.0, 0.0)


def _usd(model: str, usage) -> float:
    pin, pout = _price(model)
    return usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout


async def _extract(client, model, transcript):
    resp = await client.messages.create(
        model=model, max_tokens=1024, system=EXTRACT_SYS,
        messages=[{"role": "user", "content": _user_message(transcript, _format_now(NOW))}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _normalize(_strip_and_parse(text)), resp.usage


async def _repair(client, model, transcript):
    text = (transcript or "").strip()
    if len(text) < MIN_CHARS_TO_REPAIR:
        return transcript, None
    resp = await client.messages.create(
        model=model, max_tokens=2048, system=REPAIR_SYS,
        messages=[{"role": "user", "content": text}],
    )
    repaired = _extract_text(resp.content).strip()
    ratio = len(repaired) / len(text) if text else 0
    ok = bool(repaired) and MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO
    return (repaired if ok else transcript), resp.usage


async def _merged(client, model, transcript):
    resp = await client.messages.create(
        model=model, max_tokens=1024, system=MERGED_SYSTEM_PROMPT,
        thinking={"type": "disabled"},  # see app/extraction_merged.py — cost
        messages=[{"role": "user", "content": _user_message(transcript, _format_now(NOW))}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _normalize(_strip_and_parse(text)), resp.usage


def _counts(res: dict) -> dict:
    return {
        "todos": len(res["todos"]),
        "timed": sum(1 for t in res["todos"] if t["time"]),
        "dated": sum(1 for t in res["todos"] if t["dueDate"]),
        "people": len(res["people"]),
    }


def _todo_lines(res: dict) -> list[str]:
    if not res["todos"]:
        return ["    (none)"]
    out = []
    for t in res["todos"]:
        when = []
        if t["dueDate"]:
            when.append(t["dueDate"])
        if t["time"]:
            when.append(t["time"])
        out.append(f"    - {t['text']}" + (f"   [{' '.join(when)}]" if when else "   [no date/time]"))
    return out


async def _run_group(client, models, name, cases):
    extract_model, repair_model, merged_model = models
    print("#" * 84)
    print(f"# GROUP {name}")
    print("#" * 84)

    cur_total = mer_total = 0.0
    fabrications = 0

    for title, transcript in cases:
        repaired, ur = await _repair(client, repair_model, transcript)
        cur, ue = await _extract(client, extract_model, repaired)
        mer, um = await _merged(client, merged_model, transcript)

        cur_cost = (_usd(repair_model, ur) if ur else 0.0) + _usd(extract_model, ue)
        mer_cost = _usd(merged_model, um)
        cur_total += cur_cost
        mer_total += mer_cost

        cc, mc = _counts(cur), _counts(mer)
        # Hard gate: merged is LESS restrained than current (invented a todo, a
        # clock time, or a person the current pipeline didn't).
        fabricated = (
            mc["todos"] > cc["todos"]
            or mc["timed"] > cc["timed"]
            or mc["people"] > cc["people"]
        )
        if fabricated:
            fabrications += 1
        verdict = "⚠ FABRICATION (merged less restrained)" if fabricated else (
            "ok — restraint preserved" if cc == mc else "differs (not a fabrication)"
        )

        print("=" * 84)
        print(f"CASE: {title}")
        print(f"  transcript: {transcript}")
        print("  CURRENT (repair->extract) todos:")
        print("\n".join(_todo_lines(cur)))
        print(f"    summary: {cur['summary']}")
        print(f"    people: {cur['people']}   topics: {cur['topics']}")
        print("  MERGED (one call) todos:")
        print("\n".join(_todo_lines(mer)))
        print(f"    summary: {mer['summary']}")
        print(f"    people: {mer['people']}   topics: {mer['topics']}")
        print(f"  counts  current={cc}  merged={mc}")
        print(f"  VERDICT: {verdict}")
        print(f"  cost    current=${cur_cost:.5f}   merged=${mer_cost:.5f}   "
              f"delta=${mer_cost - cur_cost:+.5f}")

    print("-" * 84)
    print(f"GROUP {name} TOTALS: current=${cur_total:.5f}  merged=${mer_total:.5f}  "
          f"delta=${mer_total - cur_total:+.5f}  fabrications={fabrications}")
    return cur_total, mer_total, fabrications


async def main() -> None:
    load_dotenv()
    extract_model = os.environ.get("EXTRACTION_MODEL") or EXTRACT_DEFAULT
    repair_model = os.environ.get("REPAIR_MODEL") or DEFAULT_REPAIR_MODEL
    merged_model = os.environ.get("MERGED_MODEL") or DEFAULT_MERGED_MODEL
    models = (extract_model, repair_model, merged_model)

    print(f"current: repair={repair_model} -> extract={extract_model}")
    print(f"merged : {merged_model}")
    print(f"fixed now: {_format_now(NOW)}")

    client = AsyncAnthropic()
    ca, ma, fa = await _run_group(client, models, "A — restraint suite (test_extraction)", SAMPLES)
    cb, mb, fb = await _run_group(client, models, "B — dialect recovery (Melia raw)", DIALECT_CASES)

    print("=" * 84)
    print("OVERALL")
    print(f"  current total: ${ca + cb:.5f}")
    print(f"  merged  total: ${ma + mb:.5f}")
    print(f"  delta        : ${(ma + mb) - (ca + cb):+.5f}")
    print(f"  TOTAL FABRICATIONS (hard gate — must be 0): {fa + fb}")


if __name__ == "__main__":
    asyncio.run(main())
