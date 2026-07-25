"""Standalone transcription test — no DB, no server.

Transcribes every audio file in samples/ and prints, per file: detected
language, duration, and the full transcript. This is the quality gate for
Step 2 Phase 1 — read the transcripts and judge whether transcription survives
real Lebanese / mixed speech before wiring anything.

Requires OPENAI_API_KEY (read from .env). Usage:
    python test_transcription.py
"""

import asyncio
import glob
import os

from dotenv import load_dotenv

from app.transcription import SUPPORTED_FORMATS, transcribe

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "samples")


def _audio_files() -> list[str]:
    files = []
    for path in glob.glob(os.path.join(SAMPLES_DIR, "*")):
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in SUPPORTED_FORMATS and os.path.isfile(path):
            files.append(path)
    return sorted(files)


async def main() -> None:
    load_dotenv()

    files = _audio_files()
    if not files:
        print(f"No audio files found in {SAMPLES_DIR}/")
        print("Drop your recordings there (m4a/mp3/wav/webm/...) and re-run.")
        print("See samples/README.md for what to record.")
        return

    for path in files:
        print("=" * 72)
        print(f"FILE: {os.path.basename(path)}")
        print("-" * 72)
        try:
            result = await transcribe(path)
        except Exception as exc:  # noqa: BLE001 — surface any failure per-file
            print(f"ERROR: {exc}")
            print()
            continue
        print(f"language   : {result['language']}")
        print(f"durationSec: {result['durationSec']}")
        print("transcript :")
        print(result["text"])
        print()


if __name__ == "__main__":
    asyncio.run(main())
