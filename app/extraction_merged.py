"""Experimental (Step 10, Phase 3): merged dialect-repair + extraction in ONE call.

The two-call audio pipeline is:
    transcript -> repair (Sonnet, outputs a FULL transcript) -> extraction (Haiku, JSON)
The repair step's full-transcript output is the cost driver — output tokens bill
at 5x input, and repair emits a whole transcript. This merges the two: a single
model call that silently corrects obvious dialect mistranscriptions in its own
reasoning and outputs ONLY the extraction JSON. The long transcript output — the
expensive part — disappears.

The risk being measured is RESTRAINT. Repair is a *generative* mode, and folding
it into extraction could erode the discipline the extraction prompt depends on
(never invent a todo, never fabricate a clock time, never turn a past event into
a task). So this reuses the production extraction system prompt VERBATIM and only
APPENDS a correction instruction that explicitly re-subordinates itself to every
restraint rule above it.

Model: repair needs Sonnet-level reasoning — claude-haiku-4-5 failed the
جدّد->جدة case (see app/repair.py) — so this defaults to Sonnet 5, not Haiku.

Thinking is DISABLED. Sonnet 5 emits an extended-thinking block by default, and
those reasoning tokens bill as output ($10/M) — measured, they DOUBLE the call's
output and make the merged path *more* expensive than the two-call pipeline it's
meant to replace. Disabling thinking makes merged ~22% cheaper than two-call on a
realistic entry, and — measured on the جدّد->جدة case — the correction survives
without the reasoning (Sonnet catches the mishearing directly). So: thinking off.

Tradeoff: there is NO repaired-transcript artifact to inspect afterwards. The
correction happens inside the model; only the JSON comes out. Debugging a bad
extraction can't show the corrected text it came from.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.extraction import (
    SYSTEM_PROMPT as _EXTRACT_SYS,
    MAX_TOKENS,
    _empty_result,
    _format_now,
    _normalize,
    _strip_and_parse,
    _user_message,
    usage_of,
)

DEFAULT_MERGED_MODEL = "claude-sonnet-5"

# Appended AFTER the verbatim extraction prompt (recency reinforces both the new
# behavior and the restraint it must not break).
_REPAIR_ADDENDUM = """

SPEECH-TO-TEXT CORRECTION (read carefully):
The transcript is raw speech-to-text output and may contain mistranscriptions. \
Speech-to-text frequently mishears spoken Lebanese dialect words — especially \
verbs and proper nouns — as similar-sounding but contextually wrong words. For \
example the verb "جدّد" (to renew) is often misheard as the city name "جدة" \
(Jeddah). Before extracting, silently infer the word the speaker almost certainly \
meant from context, and extract from that corrected understanding.

This is the ONLY inference you are permitted, and it does NOT relax any rule \
above. Correcting a misheard word may change WHICH word was meant; it must never \
ADD meaning that was not spoken. Every restraint still holds exactly: never \
invent a todo, never set a clock time that was not spoken ("tonight" is a day, \
not a time), never turn a past event into a task, never add a person or a date \
that was not said.

Output only the JSON object, nothing else."""

MERGED_SYSTEM_PROMPT = _EXTRACT_SYS + _REPAIR_ADDENDUM


async def extract_merged(
    transcript: str,
    now: Optional[datetime] = None,
    model: Optional[str] = None,
) -> tuple[dict[str, Any], Optional[dict[str, int]]]:
    """Repair-then-extract in a single model call. Returns (result, usage) — same
    contract as extraction.extract(); never raises on a malformed response."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not transcript or not transcript.strip():
        return _empty_result(), None

    client = AsyncAnthropic()
    model = model or os.environ.get("MERGED_MODEL") or DEFAULT_MERGED_MODEL

    response = await client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=MERGED_SYSTEM_PROMPT,
        # Off by default on Sonnet 5 it is NOT — disable it explicitly; thinking
        # tokens bill as output and defeat the whole point (see module docstring).
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": _user_message(transcript, _format_now(now))}],
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    return _normalize(_strip_and_parse(text)), usage_of(response)
