"""Context-aware repair of speech-to-text output.

Whisper hears well but reasons poorly: it picks the more common of two
similar-sounding words with no sense of whether the sentence still means
anything. Measured on samples/, "مطلوب انزل جدّد باسبوري" (I need to renew my
passport) came back as "جدة" — Jeddah, the city — from BOTH whisper-large-v3-turbo
and whisper-large-v3, producing a todo about travelling to Saudi Arabia.

An LLM reading only the broken text recovers the right word, because it knows
which reading makes sense. Model choice matters here: claude-haiku-4-5 failed
this exact case, claude-sonnet-5 fixed it, so REPAIR_MODEL defaults to Sonnet
and is deliberately NOT tied to EXTRACTION_MODEL.

Two rules this module holds to:

1. **The repaired text is never persisted as the user's words.** Entry.rawTranscript
   keeps what was actually transcribed; only extraction reads the repaired copy.
   A journal that quietly rewrites what you said is not a journal.
2. **Failure is always silent and safe.** Any error, any suspicious output, and
   the original transcript is returned unchanged. Repair is an enhancement; it
   must never be able to break ingestion.
"""

from __future__ import annotations

import os

from anthropic import AsyncAnthropic

DEFAULT_REPAIR_MODEL = "claude-sonnet-5"

# Below this, there isn't enough context to reason about — and short utterances
# are where an over-eager model is most likely to invent something.
MIN_CHARS_TO_REPAIR = 12

# A repair should be roughly the same length as its input. Anything far outside
# this is the model explaining itself, refusing, or hallucinating extra content.
MIN_LENGTH_RATIO = 0.5
MAX_LENGTH_RATIO = 1.6

SYSTEM_PROMPT = """You correct speech-to-text transcripts of casual spoken \
Lebanese Arabic, which may mix in English.

The transcriber mishears dialect words as similar-sounding but nonsensical ones, \
especially verbs and proper nouns. Your only job is to restore what the speaker \
almost certainly said.

Rules:
- Change a word ONLY when it makes no sense in context. If the transcript already \
reads sensibly, return it EXACTLY as given.
- Never translate. Keep Arabic in Arabic and English in English, including \
mid-sentence code-switching.
- Never add, remove, summarise, or reorder content. Do not add commentary, \
explanation, or quotation marks.
- Preserve the speaker's casual register — do not formalise dialect into MSA.

Return only the corrected transcript."""


def _is_enabled() -> bool:
    return (os.environ.get("REPAIR_ENABLED", "1").strip().lower()
            not in {"0", "false", "no", ""})


def _extract_text(content) -> str:
    """Pull the text block out of a response.

    Reasoning-capable models emit a thinking block first, so indexing content[0]
    is not safe — take the first block that is actually text.
    """
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


async def repair_transcript(transcript: str) -> str:
    """Return a context-corrected transcript, or the original if anything is off.

    Never raises: callers can use the result unconditionally.
    """
    text = (transcript or "").strip()

    if not _is_enabled() or len(text) < MIN_CHARS_TO_REPAIR:
        return transcript

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return transcript

    model = os.environ.get("REPAIR_MODEL") or DEFAULT_REPAIR_MODEL

    try:
        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        repaired = _extract_text(response.content).strip()
    except Exception:  # noqa: BLE001 — repair is optional; ingestion is not
        return transcript

    if not repaired:
        return transcript

    # Length sanity check: a repair rewrites words, it doesn't change the size of
    # the utterance. Anything else means we got something other than a transcript.
    ratio = len(repaired) / len(text)
    if ratio < MIN_LENGTH_RATIO or ratio > MAX_LENGTH_RATIO:
        return transcript

    return repaired
