"""Compare STT configs on the local samples — a dialect quality gate.

Runs every file in samples/ through a grid of (model x prompt) combinations and
prints the transcripts side by side, so *you* can judge them against what you
actually said. Nothing here is scored automatically: for dialect accuracy the
only reliable judge is the person who spoke.

Usage (from voice-journal-backend/):
    .venv/bin/python test_dialect_grid.py
"""

import asyncio
import glob
import os

from dotenv import load_dotenv

from app.transcription import SUPPORTED_FORMATS, transcribe

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "samples")

# A Lebanese-specific primer. Whisper's `prompt` biases decoding toward the
# vocabulary and *orthography* it contains, so this is written in Lebanese
# dialect spelling and seeded with the exact words that have been misheard
# before (جدّد -> جدة, طبيب الأسنان). Generic "expect Arabic" wording does much
# less than concrete dialect tokens.
LEBANESE_PRIMER = (
    "يوميات شخصية بالعامية اللبنانية. الحكي عن الشغل والصحة والأهل والمشاوير. "
    "كلمات شائعة: بكرا، مبارح، هلق، بدي، لازم، رحت، شفت، كتير، منيح، تعبان، "
    "مبسوط، زعلان، عندي موعد، طبيب الأسنان، وجع، جدّد جواز السفر، حجزت طيارة، "
    "بيت، شغل، دوام، اجتماع، صحابي، ماما، بابا."
)

# The existing opt-in primer, for comparison (see app/transcription.py).
from app.transcription import RECOMMENDED_CODE_SWITCH_PROMPT  # noqa: E402

CONFIGS = [
    ("1. turbo, no primer  (CURRENT DEFAULT)", "whisper-large-v3-turbo", None),
    ("2. large-v3, no primer", "whisper-large-v3", None),
    ("3. turbo + Lebanese primer", "whisper-large-v3-turbo", LEBANESE_PRIMER),
    ("4. large-v3 + Lebanese primer", "whisper-large-v3", LEBANESE_PRIMER),
    ("5. large-v3 + code-switch primer", "whisper-large-v3", RECOMMENDED_CODE_SWITCH_PROMPT),
]


def _audio_files() -> list[str]:
    files = [
        path
        for path in glob.glob(os.path.join(SAMPLES_DIR, "*"))
        if os.path.splitext(path)[1].lower().lstrip(".") in SUPPORTED_FORMATS
        and os.path.isfile(path)
    ]
    return sorted(files)


async def main() -> None:
    load_dotenv()
    files = _audio_files()
    if not files:
        print(f"No audio in {SAMPLES_DIR}/ — drop recordings there first.")
        return

    for path in files:
        print("=" * 78)
        print(f"FILE: {os.path.basename(path)}")
        print("=" * 78)
        for label, model, prompt in CONFIGS:
            os.environ["STT_MODEL"] = model
            try:
                result = await transcribe(path, prompt=prompt)
            except Exception as exc:  # noqa: BLE001 — one config failing shouldn't stop the grid
                print(f"\n{label}\n    ERROR: {exc}")
                continue
            print(f"\n{label}  [lang={result['language']}]")
            print(f"    {result['text']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
