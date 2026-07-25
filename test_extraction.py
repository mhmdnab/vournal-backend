"""Standalone extraction test — no database, no server required.

Runs extract() on a few hardcoded sample transcripts and pretty-prints the JSON
so extraction quality can be read directly. Requires ANTHROPIC_API_KEY.

Usage:
    python test_extraction.py
"""

import asyncio
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.extraction import extract

# Fixed "now" so relative dates ("tomorrow") AND times ("in an hour") are
# reproducible when reading output. 2026-07-20 is a Monday, 14:30 UTC — which
# makes "tonight", "in an hour", and "next Monday" all easy to eyeball.
NOW = datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc)

SAMPLES: list[tuple[str, str]] = [
    (
        "English ramble, two todos + a date",
        "Okay so today was, uh, kind of all over the place. I finally shipped the "
        "onboarding flow at work which felt great, honestly relieved more than "
        "anything. Um, I need to call the dentist tomorrow to reschedule that "
        "cleaning, and I really should email Sarah back about the apartment lease "
        "before she gives it to someone else. Anyway. Good day I think.",
    ),
    (
        "Timed appointment (tomorrow at 2pm)",
        "Ugh don't let me forget I have a doctor at 2pm tomorrow, and I still need "
        "to pick up the prescription before that.",
    ),
    (
        "Timed meeting (next Monday at 10)",
        "Big week coming up. I've got a meeting with Sarah next Monday at 10 to go "
        "over the roadmap, and I should prep the slides for it beforehand.",
    ),
    (
        "Time-only, no am/pm + tonight",
        "Remind me to call mom tonight, and I need to take the trash out at 8.",
    ),
    (
        "Untimed errand, no date at all",
        "I just want to relax honestly. Oh, I need to buy groceries at some point, "
        "we're out of basically everything.",
    ),
    (
        "Arabizi timed appointment",
        "el youm ken tawil bass mnee7. bukra el sa3a 3 3andi maw3ad 3and el doktor, "
        "w lezem 7ki ma3 Karim bi khsous el shghel. ba3d sa3a lezem rou7 3al gym.",
    ),
    (
        "Low-mood entry",
        "I don't know. Everything just feels heavy right now. I barely slept again, "
        "like maybe three hours, and I keep snapping at people I care about. I feel "
        "like I'm falling behind on everything and I can't catch up. I just want to "
        "disappear for a while.",
    ),
    (
        "Lebanese Arabizi entry",
        "yiii 3am b7es 7ali ta3ban ktir hal youm, ma nemet mnee7 w 3andi deadline "
        "bokra lal shghel. lezem 7ki ma3 Karim bi khsous el project w lezem rou7 3al "
        "gym bala ma ensa. bass mabsout enno finally 5allaset el taqrir.",
    ),
    (
        "Mixed English + Arabizi",
        "Honestly hal week was so stressful, kello shghel w meetings. I need to book "
        "my flight to see mama next month, w lezem ballesh workout routine. 3am 7awel "
        "bass sa3b.",
    ),
]


async def main() -> None:
    load_dotenv()
    print(f"CURRENT DATE AND TIME (fixed): {NOW.strftime('%Y-%m-%d %H:%M (%A, UTC)')}")
    for title, transcript in SAMPLES:
        print("=" * 72)
        print(f"SAMPLE: {title}")
        print("-" * 72)
        print("TRANSCRIPT:")
        print(transcript)
        print("-" * 72)
        result, _usage = await extract(transcript, now=NOW)
        print("EXTRACTION:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    asyncio.run(main())
