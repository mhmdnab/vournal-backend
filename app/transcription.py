"""Speech-to-text transcription behind a thin provider seam.

`transcribe(audio_path)` returns {"text", "language", "durationSec"}.

The default provider is Groq (whisper-large-v3-turbo): the same Whisper family
we validate quality on, a stronger multilingual model than OpenAI's whisper-1,
and ~9x cheaper (~$0.04/hr vs $0.36/hr). Groq exposes an OpenAI-compatible API,
so both providers run through the same `openai` SDK client — only the base_url,
API key, and model differ. OpenAI is kept as an alternate provider; Groq /
Deepgram / AssemblyAI-style swaps happen here, not in the callers.

Provider + model come from the environment (STT_PROVIDER / STT_MODEL).

Verified against the Groq and OpenAI speech-to-text docs (2026):
  - Groq base_url https://api.groq.com/openai/v1, key GROQ_API_KEY, models
    whisper-large-v3-turbo / whisper-large-v3, formats json/verbose_json/text,
    25 MB free tier.
  - OpenAI whisper-1 supports verbose_json (language + duration); the gpt-4o-*
    transcribe models return text only.
  - Whisper models (any host) return language + duration via verbose_json; we
    fall back to a local mutagen probe for duration when it's absent.

Cost note: Groq whisper-large-v3-turbo ~= $0.0007/min (~$0.04/hr);
OpenAI whisper-1 ~= $0.006/min; OpenAI gpt-4o-mini-transcribe ~= $0.003/min.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from openai import AsyncOpenAI

# Formats accepted across the supported providers. m4a is primary (Expo records m4a).
SUPPORTED_FORMATS = {"m4a", "mp3", "wav", "webm", "mp4", "mpga", "mpeg", "flac", "ogg"}

# Hard size limit. Groq free tier and OpenAI both cap at 25 MB; chunking longer
# audio is a later step.
MAX_BYTES = 25 * 1024 * 1024  # 25 MB


def looks_like_audio(data: bytes) -> bool:
    """Magic-byte check on the actual bytes — the client-declared filename/type is
    not trusted (the mobile part carries a client-set name). True if the leading
    bytes match a known audio container/codec; False rejects the upload."""
    if len(data) < 12:
        return False
    b = data
    return (
        b[4:8] == b"ftyp"                      # MP4 / M4A (ISO base media)
        or b[:4] == b"\x1a\x45\xdf\xa3"        # WebM / Matroska (EBML)
        or b[:4] == b"OggS"                    # Ogg
        or (b[:4] == b"RIFF" and b[8:12] == b"WAVE")  # WAV
        or b[:4] == b"fLaC"                    # FLAC
        or b[:3] == b"ID3"                     # MP3 with an ID3 tag
        or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)  # MPEG audio frame sync
    )

# Speechmatics Melia is the default STT as of Step 10 — it does native single-pass
# code-switching (English + Lebanese in one utterance) that Whisper can only fake
# with a blunt primer. Groq stays reachable via STT_PROVIDER=groq (the seam).
DEFAULT_PROVIDER = "speechmatics"

# Code-switch note: Whisper collapses mixed speech toward one language (it
# translated "بكرا i wanna go to the طبيب الأسنان" into MSA and dropped the
# English). Seeding the `prompt` below with this bilingual primer recovers
# verbatim mixed transcription — BUT it's a blunt lever: an English-leaning
# primer also pulls some *pure-Arabic* utterances into broken English. So it's
# OPT-IN, not the default. Default behavior (no primer) keeps pure English and
# pure Arabic clean; code-switched entries normalize to one language but their
# *meaning* is preserved, so the extractor still produces the right todos.
# Enable per-deployment with STT_PROMPT (or pass prompt= to transcribe()).
RECOMMENDED_CODE_SWITCH_PROMPT = (
    "Casual personal voice journal. The speaker may mix Lebanese Arabic and "
    "English in the same sentence. Transcribe exactly what is said — keep "
    "English words in English and Arabic words in Arabic, do not translate. "
    "For example: بكرا I have a meeting at work, وبعدها بدي go to the gym."
)

# The default primer, and the reason the note above is no longer the whole story.
#
# Measured on samples/ (see test_dialect_grid.py), this beat every alternative:
#
#   "مطلوب انزل جدّد باسبوري" (I need to go renew my passport)
#     no primer, turbo      -> جدة   (Jeddah, the city)  WRONG
#     no primer, large-v3   -> جدة                       WRONG  <- model didn't fix it
#     code-switch primer    -> جدة                       WRONG
#     THIS primer           -> جدّد  (renew)             RIGHT
#
#   "بكرا i wanna go to the طبيب الأسنان" (code-switched)
#     Lebanese-only primer  -> normalized to Arabic, English dropped
#     THIS primer           -> "بكرا I wanna go to the طبيب الأسنان."  verbatim
#
# Why it works where the English-only primer didn't: Whisper's prompt biases
# both vocabulary *and orthography*, so seeding actual Lebanese dialect spelling
# ("بدي", "هلق", "جدّد") anchors decoding in the dialect, while the bilingual
# example sentence separately licenses mid-sentence language switching. Pure
# English wording only did the latter, which is why it used to drag Arabic
# entries toward English.
#
# CAUTION when extending this: Whisper only reads the LAST ~224 tokens of the
# prompt, and Arabic tokenizes expensively. This is already near that ceiling —
# adding to the front will silently push the dialect vocabulary out of the
# window. Re-run test_dialect_grid.py after any edit.
RECOMMENDED_LEBANESE_PROMPT = (
    "يوميات شخصية بالعامية اللبنانية. الحكي عن الشغل والصحة والأهل والمشاوير. "
    "كلمات شائعة: بكرا، مبارح، هلق، بدي، لازم، رحت، شفت، كتير، منيح، تعبان، "
    "مبسوط، زعلان، عندي موعد، طبيب الأسنان، وجع، جدّد جواز السفر، حجزت طيارة، "
    "بيت، شغل، دوام، اجتماع، صحابي، ماما، بابا."
    " The speaker may mix Lebanese Arabic and English in the same sentence. "
    "Transcribe exactly what is said — keep English words in English and Arabic "
    "words in Arabic, do not translate. For example: بكرا I have a meeting at "
    "work, وبعدها بدي go to the gym."
)

# Per-provider wiring. Because Groq is OpenAI-compatible, both go through
# AsyncOpenAI — only base_url / key env / default model change.
PROVIDER_CONFIG: dict[str, dict[str, Optional[str]]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "default_model": "whisper-large-v3-turbo",
    },
    "openai": {
        "base_url": None,  # default OpenAI endpoint
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "whisper-1",
    },
}

# ---- Speechmatics (Step 10) --------------------------------------------------
#
# NOT OpenAI-compatible — this is the "non-compatible provider branches here"
# seam the code always anticipated. Speechmatics is a BATCH API: submit a job,
# poll until done, fetch the transcript. Verified against the Speechmatics docs
# (2026): auth is "Authorization: Bearer <key>"; regional REST bases are
# {eu1,us1}.asr.api.speechmatics.com/v2; POST /jobs takes multipart `data_file`
# + a `config` JSON; GET /jobs/{id} reports job.status (running -> done|rejected)
# and job.duration; GET /jobs/{id}/transcript?format=txt returns plain text, and
# ?format=json-v2 returns per-word `language` labels (how we tell whether it
# actually kept both languages in a code-switched clip).
#
# Melia-1 is the multilingual model that claims native single-pass code-switching.
# It REQUIRES language="multi" (rejects "auto"), and — unlike Whisper on Groq — it
# has NO custom-vocabulary / prompt-biasing lever, so there is no Lebanese primer
# to apply here: it's evaluated exactly as it hears. language_hints only nudges;
# it still labels any other language it hears.
SPEECHMATICS_DEFAULT_MODEL = "melia-1"
SPEECHMATICS_DEFAULT_REGION = "eu1"  # eu1 | us1
SPEECHMATICS_LANGUAGE_HINTS = ["en", "ar"]  # our domain; not a hard restriction
_SPEECHMATICS_TERMINAL = {"done", "rejected", "deleted", "expired"}


async def _transcribe_speechmatics(
    audio_path: str, model: str, region: str, api_key: str
) -> dict[str, Any]:
    """Batch transcription via Speechmatics: submit -> poll -> fetch.

    Returns the same {"text", "language", "durationSec"} contract as the
    OpenAI-compatible path. `language` is the set of languages Melia actually
    labelled (e.g. "en, ar" for a code-switched clip), which is the signal we
    care about for the benchmark.
    """
    import asyncio
    import json

    import httpx

    base = f"https://{region}.asr.api.speechmatics.com/v2"
    headers = {"Authorization": f"Bearer {api_key}"}
    config = {
        "type": "transcription",
        "transcription_config": {
            "model": model,
            "language": "multi",
            "language_hints": SPEECHMATICS_LANGUAGE_HINTS,
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Submit.
        with open(audio_path, "rb") as f:
            submit = await client.post(
                f"{base}/jobs",
                headers=headers,
                data={"config": json.dumps(config)},
                files={"data_file": (os.path.basename(audio_path), f, "application/octet-stream")},
            )
        submit.raise_for_status()
        job_id = submit.json()["id"]

        # Poll (batch jobs are usually seconds for short clips).
        status: Optional[str] = None
        duration: Optional[float] = None
        for _ in range(150):  # ~5 min ceiling at 2s
            await asyncio.sleep(2)
            poll = await client.get(f"{base}/jobs/{job_id}", headers=headers)
            poll.raise_for_status()
            job = poll.json().get("job", {})
            status = job.get("status")
            if job.get("duration") is not None:
                duration = float(job["duration"])
            if status in _SPEECHMATICS_TERMINAL:
                break
        if status != "done":
            raise RuntimeError(
                f"Speechmatics job {job_id} ended with status '{status}'."
            )

        # Plain-text transcript for reading.
        txt = await client.get(
            f"{base}/jobs/{job_id}/transcript",
            headers=headers,
            params={"format": "txt"},
        )
        txt.raise_for_status()
        text = (txt.text or "").strip()

        # json-v2 for the per-word language labels (the code-switch signal).
        language: Optional[str] = None
        detail = await client.get(
            f"{base}/jobs/{job_id}/transcript",
            headers=headers,
            params={"format": "json-v2"},
        )
        if detail.status_code == 200:
            data = detail.json()
            langs: list[str] = []
            for item in data.get("results", []):
                alt = (item.get("alternatives") or [{}])[0]
                lang = alt.get("language")
                if lang and lang not in langs:
                    langs.append(lang)
            if langs:
                language = ", ".join(langs)
            if duration is None:
                dur = (data.get("job") or {}).get("duration")
                duration = float(dur) if dur is not None else None

    return {"text": text, "language": language, "durationSec": duration}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def _supports_verbose_json(model: str) -> bool:
    # Whisper models return language + duration via verbose_json; the OpenAI
    # gpt-4o-* transcribe models only support json/text.
    return "whisper" in model.lower()


def _probe_duration(path: str) -> Optional[float]:
    """Best-effort local duration probe (seconds). Tolerates failure -> None."""
    try:
        from mutagen import File as MutagenFile

        media = MutagenFile(path)
        if media is not None and media.info is not None:
            length = getattr(media.info, "length", None)
            if length:
                return float(length)
    except Exception:
        pass
    return None


async def transcribe(
    audio_path: str,
    prompt: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    """Transcribe an audio file to text.

    Args:
        audio_path: path to a local audio file.
        prompt: Whisper vocabulary/style hint (OpenAI-compatible providers only;
            Speechmatics has no such lever and ignores it). Defaults to the
            STT_PROMPT env var, which itself defaults to RECOMMENDED_LEBANESE_PROMPT
            — the measured-best primer for Lebanese dialect + code-switching. Pass
            a string to override per call, or set STT_PROMPT="" for no primer.
        provider: override STT_PROVIDER for this call (the benchmark passes both
            "groq" and "speechmatics"). Defaults to the env / DEFAULT_PROVIDER.
        model: override the model for this call. Defaults per provider.

    Returns:
        {"text": str, "language": str | None, "durationSec": float | None}

    Raises:
        ValueError: unsupported format, over the size limit, or missing API key.
        NotImplementedError: an unknown provider.
    """
    ext = _ext(audio_path)
    if ext not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported audio format '.{ext}'. Supported: {sorted(SUPPORTED_FORMATS)}"
        )

    size = os.path.getsize(audio_path)
    if size > MAX_BYTES:
        # Chunking of long audio is a later step; for now, reject clearly.
        raise ValueError(
            f"Audio file is {size / 1_048_576:.1f} MB, over the "
            f"{MAX_BYTES // 1_048_576} MB provider limit. Chunking is a later step."
        )

    provider = (provider or os.environ.get("STT_PROVIDER") or DEFAULT_PROVIDER).lower()

    # Speechmatics is a batch API, not OpenAI-compatible — its own branch.
    if provider == "speechmatics":
        api_key = os.environ.get("SPEECHMATICS_API_KEY")
        if not api_key:
            raise ValueError(
                "SPEECHMATICS_API_KEY is not set (required for STT_PROVIDER 'speechmatics')."
            )
        # STT_MODEL is Whisper-specific; don't leak it into Speechmatics. Use the
        # explicit arg, else the Melia default.
        sm_model = model or SPEECHMATICS_DEFAULT_MODEL
        region = os.environ.get("SPEECHMATICS_REGION") or SPEECHMATICS_DEFAULT_REGION
        return await _transcribe_speechmatics(audio_path, sm_model, region, api_key)

    cfg = PROVIDER_CONFIG.get(provider)
    if cfg is None:
        raise NotImplementedError(
            f"STT_PROVIDER '{provider}' is not implemented. "
            f"Options: {sorted([*PROVIDER_CONFIG, 'speechmatics'])}"
        )

    model = model or os.environ.get("STT_MODEL") or cfg["default_model"]

    api_key = os.environ.get(cfg["api_key_env"] or "")
    if not api_key:
        raise ValueError(
            f"{cfg['api_key_env']} is not set (required for STT_PROVIDER '{provider}')."
        )

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if cfg["base_url"]:
        client_kwargs["base_url"] = cfg["base_url"]
    client = AsyncOpenAI(**client_kwargs)

    response_format = "verbose_json" if _supports_verbose_json(model) else "json"

    # Lebanese dialect primer is now the DEFAULT (see RECOMMENDED_LEBANESE_PROMPT
    # for the measurements). Override with STT_PROMPT, or set STT_PROMPT="" to
    # transcribe with no primer at all.
    if prompt is None:
        prompt = os.environ.get("STT_PROMPT", RECOMMENDED_LEBANESE_PROMPT)

    create_kwargs: dict[str, Any] = {
        "model": model,
        "response_format": response_format,
    }
    if prompt:
        create_kwargs["prompt"] = prompt

    # Do NOT force a language — let the model auto-detect so English / Arabic /
    # code-switched entries all work.
    with open(audio_path, "rb") as f:
        resp = await client.audio.transcriptions.create(file=f, **create_kwargs)

    text = (getattr(resp, "text", "") or "").strip()
    language = getattr(resp, "language", None)

    duration = getattr(resp, "duration", None)
    duration = float(duration) if duration is not None else _probe_duration(audio_path)

    return {"text": text, "language": language, "durationSec": duration}
