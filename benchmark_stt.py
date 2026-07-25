"""Standalone STT benchmark — no DB, no server.

Runs every audio file in samples/ through BOTH the current provider (Groq
whisper-large-v3-turbo, WITH the Lebanese primer we ship) and the candidate
(Speechmatics Melia, melia-1 — which has no primer/vocab lever, so it's judged
exactly as it hears). Prints, per file and per provider: the FULL transcript,
detected/labelled language, wall-clock latency, and estimated cost — then a
per-provider cost total for the run.

Read the transcripts and judge dialect / code-switch quality yourself. This
prints the evidence; it does not score.

Requires GROQ_API_KEY and SPEECHMATICS_API_KEY in .env. Usage:
    python benchmark_stt.py
"""

import asyncio
import glob
import os
import time

from dotenv import load_dotenv

from app.transcription import SUPPORTED_FORMATS, transcribe

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "samples")

# (provider, label, $/hr, minimum billed seconds per request)
#   Groq: ~$0.04/hr, 10-second minimum per request.
#   Speechmatics Melia: ~$0.129/hr batch (no documented per-request minimum).
PROVIDERS = [
    ("groq", "Groq whisper-large-v3-turbo  (+ Lebanese primer — what we ship)", 0.04, 10.0),
    ("speechmatics", "Speechmatics Melia  (melia-1, no primer)", 0.129, 0.0),
]


def _audio_files() -> list[str]:
    files = []
    for path in glob.glob(os.path.join(SAMPLES_DIR, "*")):
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in SUPPORTED_FORMATS and os.path.isfile(path):
            files.append(path)
    return sorted(files)


def _cost(rate_per_hr: float, min_sec: float, duration: float | None) -> float | None:
    if duration is None:
        return None
    return max(duration, min_sec) / 3600.0 * rate_per_hr


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(pad + line for line in (text or "").splitlines()) or f"{pad}(empty)"


async def main() -> None:
    load_dotenv()

    files = _audio_files()
    if not files:
        print(f"No audio files found in {SAMPLES_DIR}/")
        print("Drop your recordings there (m4a/mp3/wav/...) and re-run.")
        return

    totals = {p[0]: 0.0 for p in PROVIDERS}

    for path in files:
        print("=" * 78)
        print(f"FILE: {os.path.basename(path)}")

        for provider, label, rate, min_sec in PROVIDERS:
            print("-" * 78)
            print(label)
            t0 = time.perf_counter()
            try:
                res = await transcribe(path, provider=provider)
            except Exception as exc:  # noqa: BLE001 — surface any failure per-file
                print(f"  ERROR: {exc}")
                continue
            dt = time.perf_counter() - t0
            cost = _cost(rate, min_sec, res["durationSec"])
            if cost is not None:
                totals[provider] += cost

            print(f"  language   : {res['language']}")
            print(f"  durationSec: {res['durationSec']}")
            print(f"  latency    : {dt:.1f}s")
            print(f"  est. cost  : {('$%.5f' % cost) if cost is not None else 'n/a'}")
            print("  transcript :")
            print(_indent(res["text"]))
        print()

    print("=" * 78)
    print("RUN COST TOTALS")
    for provider, label, _rate, _min in PROVIDERS:
        print(f"  {label}")
        print(f"    total: ${totals[provider]:.5f}  over {len(files)} files")


if __name__ == "__main__":
    asyncio.run(main())
