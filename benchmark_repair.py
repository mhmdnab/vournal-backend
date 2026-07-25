"""Phase 2 — does the repair pass still earn its keep? (no DB, no server)

For every file in samples/, transcribes with the winning STT (Speechmatics Melia)
and then runs extraction BOTH ways on that one transcript:
    A)  raw transcript            -> extraction
    B)  raw -> repair (Sonnet 5)  -> extraction
and prints the two extracted JSONs side by side, whether the repaired transcript
even changed, a structural diff flag, and the exact cost of each path (measured
from real token usage).

What matters is whether repair changes the EXTRACTED RESULT in a way that matters
— a todo recovered/lost, a date/time fixed/broken, a name corrected. Cosmetic
transcript changes that yield identical JSON mean repair isn't earning its keep.
You judge; this prints the evidence.

Requires SPEECHMATICS_API_KEY + ANTHROPIC_API_KEY. Usage:
    python benchmark_repair.py
"""

import asyncio
import glob
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from anthropic import AsyncAnthropic

from app.transcription import SUPPORTED_FORMATS, transcribe
from app.extraction import (
    SYSTEM_PROMPT as EXTRACT_SYS,
    DEFAULT_MODEL as EXTRACT_DEFAULT,
    _format_now,
    _normalize,
    _strip_and_parse,
    _user_message,
)
from app.repair import (
    SYSTEM_PROMPT as REPAIR_SYS,
    DEFAULT_REPAIR_MODEL,
    MIN_CHARS_TO_REPAIR,
    MIN_LENGTH_RATIO,
    MAX_LENGTH_RATIO,
    _extract_text,
)

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "samples")

# Fixed local "now" so relative dates ("bukra", "the 20th") resolve reproducibly.
NOW = datetime(2026, 7, 20, 14, 30, tzinfo=ZoneInfo("Asia/Beirut"))

# $ per million tokens (input, output).
#   Haiku 4.5:  $1 / $5
#   Sonnet 5:   $2 / $10 introductory (through 2026-08-31); $3 / $15 after.
PRICE_HAIKU = (1.0, 5.0)
PRICE_SONNET5 = (2.0, 10.0)
MELIA_PER_HR = 0.129


def _price(model: str) -> tuple[float, float]:
    m = model.lower()
    if "haiku" in m:
        return PRICE_HAIKU
    if "sonnet" in m:
        return PRICE_SONNET5
    return (0.0, 0.0)


def _usd(model: str, usage) -> float:
    pin, pout = _price(model)
    return usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout


def _audio_files() -> list[str]:
    out = []
    for path in glob.glob(os.path.join(SAMPLES_DIR, "*")):
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in SUPPORTED_FORMATS and os.path.isfile(path):
            out.append(path)
    return sorted(out)


async def _extract(client, model, transcript):
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=EXTRACT_SYS,
        messages=[{"role": "user", "content": _user_message(transcript, _format_now(NOW))}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _normalize(_strip_and_parse(text)), resp.usage


async def _repair(client, model, transcript):
    """Mirror app.repair.repair_transcript, but capture usage. Returns
    (final_text, usage_or_None, changed)."""
    text = (transcript or "").strip()
    if len(text) < MIN_CHARS_TO_REPAIR:
        return transcript, None, False
    resp = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=REPAIR_SYS,
        messages=[{"role": "user", "content": text}],
    )
    repaired = _extract_text(resp.content).strip()
    ratio = len(repaired) / len(text) if text else 0
    ok = bool(repaired) and MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO
    final = repaired if ok else transcript
    return final, resp.usage, final.strip() != text


def _structural(result: dict) -> dict:
    """The parts of an extraction that would change a calendar/to-do — summary
    (prose) deliberately excluded from the equality check."""
    return {
        "mood_score": result["mood"]["score"],
        "todos": result["todos"],
        "topics": result["topics"],
        "people": result["people"],
    }


async def main() -> None:
    load_dotenv()
    extract_model = os.environ.get("EXTRACTION_MODEL") or EXTRACT_DEFAULT
    repair_model = os.environ.get("REPAIR_MODEL") or DEFAULT_REPAIR_MODEL
    client = AsyncAnthropic()

    files = _audio_files()
    if not files:
        print(f"No audio files in {SAMPLES_DIR}/")
        return

    print(f"STT: Speechmatics Melia   extraction: {extract_model}   repair: {repair_model}")
    print(f"fixed now: {_format_now(NOW)}")

    total_a = total_b = 0.0

    for path in files:
        print("=" * 80)
        print(f"FILE: {os.path.basename(path)}")

        res = await transcribe(path, provider="speechmatics")
        raw = res["text"]
        melia_cost = (res["durationSec"] or 0) / 3600.0 * MELIA_PER_HR

        json_a, ua = await _extract(client, extract_model, raw)
        repaired, ur, changed = await _repair(client, repair_model, raw)
        json_b, ub = await _extract(client, extract_model, repaired)

        cost_a = melia_cost + _usd(extract_model, ua)
        cost_repair = _usd(repair_model, ur) if ur else 0.0
        cost_b = melia_cost + cost_repair + _usd(extract_model, ub)
        total_a += cost_a
        total_b += cost_b

        print("-" * 80)
        print("RAW transcript (Melia):")
        print(f"    {raw}")
        print("REPAIRED transcript:" + ("" if changed else "   (UNCHANGED)"))
        print(f"    {repaired}")

        same = _structural(json_a) == _structural(json_b)
        print("-" * 80)
        print(f"STRUCTURAL EXTRACTION: {'IDENTICAL' if same else 'DIFFERS'}")

        print("\n[A] raw -> extraction:")
        print(json.dumps(json_a, indent=2, ensure_ascii=False))
        print("\n[B] raw -> repair -> extraction:")
        print(json.dumps(json_b, indent=2, ensure_ascii=False))

        print("-" * 80)
        print(f"cost A (melia+extract)        : ${cost_a:.5f}")
        print(f"cost B (melia+repair+extract) : ${cost_b:.5f}   (repair alone: ${cost_repair:.5f})")
        print()

    print("=" * 80)
    print("RUN TOTALS")
    print(f"  Path A (no repair) : ${total_a:.5f}")
    print(f"  Path B (repair)    : ${total_b:.5f}")
    print(f"  repair adds        : ${total_b - total_a:.5f} over {len(files)} files")


if __name__ == "__main__":
    asyncio.run(main())
