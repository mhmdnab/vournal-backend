"""Backfill Person/Topic.normalizedName and collapse collisions (Step 17, Phase 1).

Runs BETWEEN migration 5 (adds the nullable column) and migration 6 (makes it
required + unique). For every row it computes the normalized key with the SAME
function the app uses at runtime (app.entities.normalize_name) — so a SQL
lower()/trim() backfill is deliberately NOT used; it wouldn't strip Arabic
tashkeel or fold letter variants, and the stored key would then disagree with what
the upsert computes, spawning duplicates.

When two rows fold to the same key (e.g. "Bilal" and "bilal", or "بلال" and the
tashkeel'd "بِلال"), it MERGES them: the row with the most moments wins (its display
name is the most-used form), the losers' join rows are repointed onto it (skipping
any the winner already has), and the loser rows are deleted — their join rows
cascade. Entries are never touched. Idempotent: safe to run more than once.
"""

import asyncio
import os
import sys
from collections import defaultdict

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, ".env"))

from prisma import Prisma  # noqa: E402

from app.entities import normalize_name  # noqa: E402


async def _backfill(db, kind: str) -> None:
    """kind is 'person' or 'topic' — identical logic over the two models."""
    if kind == "person":
        model, join, fk = db.person, db.entryperson, "personId"
    else:
        model, join, fk = db.topic, db.entrytopic, "topicId"

    rows = await model.find_many(include={"entries": True})
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        groups[(row.userId, normalize_name(row.name))].append(row)

    backfilled = merged = 0
    for (_uid, norm), members in groups.items():
        if not norm:  # a blank/whitespace-only name — leave it, nothing to key on
            continue
        # Survivor: most moments first, then stable by id. Its display name is,
        # by construction, the most-used surface form.
        members.sort(key=lambda r: (-len(r.entries or []), r.id))
        survivor, losers = members[0], members[1:]

        seen = {e.entryId for e in (survivor.entries or [])}
        for loser in losers:
            for e in loser.entries or []:
                if e.entryId not in seen:
                    await join.create(data={"entryId": e.entryId, fk: survivor.id})
                    seen.add(e.entryId)
            await model.delete(where={"id": loser.id})  # cascades this row's joins
            merged += 1

        await model.update(where={"id": survivor.id}, data={"normalizedName": norm})
        backfilled += 1

    print(f"  {kind}: {backfilled} distinct key(s) set, {merged} duplicate row(s) merged")


async def main() -> None:
    db = Prisma()
    await db.connect()
    print("Backfilling normalizedName + collapsing collisions:")
    await _backfill(db, "person")
    await _backfill(db, "topic")
    # Sanity: no NULLs and no dup keys remain (migration 6 will enforce this).
    people = await db.person.find_many()
    topics = await db.topic.find_many()
    p_null = sum(1 for p in people if not p.normalizedName)
    t_null = sum(1 for t in topics if not t.normalizedName)
    print(f"Remaining NULL normalizedName -> person: {p_null}, topic: {t_null} (want 0)")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
