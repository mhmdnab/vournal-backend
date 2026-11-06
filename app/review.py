"""The app's review card (design 2026-11-06): which entries the lists may show.

A new app asks for review mode; its entry then waits as a proposal ("needs_review") until the
user saves it. Older apps never ask, so their entries keep reviewState null and list as always.
An entry saved with "Save as journal entry" off keeps its to-dos but leaves the journal."""

from datetime import datetime, timedelta, timezone

from app.db import db
from app.storage import delete_audio

# Prisma `where` fragments: listable entries, and those the journal shows
LISTED = {"OR": [{"reviewState": None}, {"reviewState": "saved"}]}
IN_JOURNAL = {**LISTED, "inJournal": True}
# the same for raw SQL, on an "Entry" aliased e
LISTED_SQL = '(e."reviewState" IS NULL OR e."reviewState" = \'saved\') AND e."inJournal"'


async def discard_stale(older_than: timedelta = timedelta(days=7)) -> int:
    """Entries left on the review card for 7 days are thrown away, with their recordings (the
    design's "Needs review" expiry). Saved and older apps' entries are never touched."""
    cutoff = datetime.now(timezone.utc) - older_than
    stale = await db.entry.find_many(where={"reviewState": "needs_review", "createdAt": {"lt": cutoff}})
    for entry in stale:
        if entry.audioUrl:
            await delete_audio(entry.audioUrl)
        await db.entry.delete(where={"id": entry.id})
    return len(stale)
