"""Entity resolution — keeping people/topics from fragmenting (Step 17, Phase 1).

Threads are only as trustworthy as the entities behind them: if "bilal", "Bilal",
and "بِلال" become three people, every thread splits and every count lies. Two
layers keep one real person as one record:

  Layer 1 — deterministic normalization (here). A `normalizedName` folds case,
    whitespace, and Arabic orthography variants; `(userId, normalizedName)` is the
    unique key, so the same surface form can never make two rows. This runs for
    free on every upsert and can't merge across scripts (بلال vs Bilal normalize
    differently) — that's Layer 2's job.

  Layer 2 — the model reuses known names. At extraction time the user's existing
    people/topics are fed to the model as a "known list" so it maps بلال onto an
    existing `Bilal`. See app/extraction.py; the list comes from `known_names()`.

Merges/splits (Phase 4) reassign the join rows and never touch an entry — entries
are the source of truth, threads are a view over the join tables.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from prisma.errors import UniqueViolationError

# Arabic marks stripped before comparison, by codepoint (explicit escapes so the
# class can't accidentally sweep in adjacent characters): U+0640 tatweel/kashida
# (the letter-stretching filler), U+064B–U+065F tashkeel/harakat + extended
# combining marks, U+0670 superscript alef, U+06D6–U+06ED Quranic annotation marks.
# The range stops at U+065F on purpose — U+0660+ are the Arabic-Indic digits.
_ARABIC_MARKS = re.compile(r"[ـً-ٰٟۖ-ۭ]")

# Fold Arabic letter variants to one canonical form so spelling choices don't split
# a name. (Deliberately NOT folding teh-marbuta/heh or Latin accents — that risks
# merging genuinely different names; the manual merge in Phase 4 handles the rest.)
_ARABIC_FOLD = {
    "أ": "ا",  # أ  alef-hamza-above -> ا
    "إ": "ا",  # إ  alef-hamza-below -> ا
    "آ": "ا",  # آ  alef-madda       -> ا
    "ى": "ي",  # ى  alef-maksura     -> ي
    "ؤ": "و",  # ؤ  waw-hamza        -> و
    "ئ": "ي",  # ئ  yeh-hamza        -> ي
}
_ARABIC_FOLD_TABLE = str.maketrans(_ARABIC_FOLD)

_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Fold a display name to its comparison key.

    NFKC -> lowercase -> strip Arabic marks/tatweel -> fold Arabic letter variants
    -> collapse internal whitespace -> trim. Deterministic and idempotent; the
    result is only ever compared, never shown.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = s.lower()
    s = _ARABIC_MARKS.sub("", s)
    s = s.translate(_ARABIC_FOLD_TABLE)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


async def known_names(db, user_id: str, limit: int = 40) -> dict[str, list[str]]:
    """The user's most relevant existing people/topics, as display names, for the
    extraction known-list (Layer 2). Ranked by frequency (moment count) with
    recency as the tiebreak, capped so the prompt can't grow unbounded. One grouped
    query per kind — no N+1.
    """
    people = await db.query_raw(
        '''SELECT p."name" AS name, COUNT(ep."entryId") AS cnt, MAX(e."createdAt") AS last
           FROM "Person" p
           JOIN "EntryPerson" ep ON ep."personId" = p."id"
           JOIN "Entry" e ON e."id" = ep."entryId"
           WHERE p."userId" = $1
           GROUP BY p."id"
           ORDER BY cnt DESC, last DESC
           LIMIT $2''',
        user_id,
        limit,
    )
    topics = await db.query_raw(
        '''SELECT t."name" AS name, COUNT(et."entryId") AS cnt, MAX(e."createdAt") AS last
           FROM "Topic" t
           JOIN "EntryTopic" et ON et."topicId" = t."id"
           JOIN "Entry" e ON e."id" = et."entryId"
           WHERE t."userId" = $1
           GROUP BY t."id"
           ORDER BY cnt DESC, last DESC
           LIMIT $2''',
        user_id,
        limit,
    )
    places = await db.query_raw(
        '''SELECT pl."name" AS name, COUNT(ep."entryId") AS cnt, MAX(e."createdAt") AS last
           FROM "Place" pl
           JOIN "EntryPlace" ep ON ep."placeId" = pl."id"
           JOIN "Entry" e ON e."id" = ep."entryId"
           WHERE pl."userId" = $1
           GROUP BY pl."id"
           ORDER BY cnt DESC, last DESC
           LIMIT $2''',
        user_id,
        limit,
    )
    return {
        "people": [r["name"] for r in people],
        "topics": [r["name"] for r in topics],
        "places": [r["name"] for r in places],
    }


async def ambiguous_norm_sets(db, user_id: str) -> dict[str, set[str]]:
    """For each kind, the normalized surface forms that 2+ distinct entities answer
    to — via canonical name OR an alias (Step 17, Phase 4). A chip whose entity's
    normalizedName is in this set reads as ambiguous ("which one did you mean?").
    Usually empty; only a split/alias collision (two people both known as "Karim")
    puts a form in here."""
    out: dict[str, set[str]] = {}
    for kind, model in (("people", db.person), ("topics", db.topic), ("places", db.place)):
        rows = await model.find_many(where={"userId": user_id})
        owners: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            forms = {r.normalizedName}
            for alias in r.aliases or []:
                norm = normalize_name(alias)
                if norm:
                    forms.add(norm)
            for form in forms:
                owners[form].add(r.id)
        out[kind] = {form for form, ids in owners.items() if len(ids) >= 2}
    return out


async def link_person(db, user_id: str, entry_id: str, display_name: str) -> None:
    """Upsert a person by its normalized key (Layer 1) and link it to the entry.
    Keeps the existing display name on a match — the canonical form is whatever was
    stored first (or the form the model echoes back from the known list)."""
    norm = normalize_name(display_name)
    if not norm:
        return
    person = await db.person.upsert(
        where={"userId_normalizedName": {"userId": user_id, "normalizedName": norm}},
        data={
            "create": {
                "userId": user_id,
                "name": display_name.strip(),
                "normalizedName": norm,
            },
            "update": {"normalizedName": norm},  # no-op that preserves the display name
        },
    )
    try:
        await db.entryperson.create(data={"entryId": entry_id, "personId": person.id})
    except UniqueViolationError:
        pass  # two spellings in one entry folded to the same person — already linked


async def link_topic(db, user_id: str, entry_id: str, display_name: str) -> None:
    """Upsert a topic by its normalized key (Layer 1) and link it to the entry."""
    norm = normalize_name(display_name)
    if not norm:
        return
    topic = await db.topic.upsert(
        where={"userId_normalizedName": {"userId": user_id, "normalizedName": norm}},
        data={
            "create": {
                "userId": user_id,
                "name": display_name.strip(),
                "normalizedName": norm,
            },
            "update": {"normalizedName": norm},
        },
    )
    try:
        await db.entrytopic.create(data={"entryId": entry_id, "topicId": topic.id})
    except UniqueViolationError:
        pass  # two spellings in one entry folded to the same topic — already linked


async def link_place(db, user_id: str, entry_id: str, display_name: str) -> None:
    """Upsert a place by its normalized key (Layer 1) and link it to the entry."""
    norm = normalize_name(display_name)
    if not norm:
        return
    place = await db.place.upsert(
        where={"userId_normalizedName": {"userId": user_id, "normalizedName": norm}},
        data={
            "create": {
                "userId": user_id,
                "name": display_name.strip(),
                "normalizedName": norm,
            },
            "update": {"normalizedName": norm},
        },
    )
    try:
        await db.entryplace.create(data={"entryId": entry_id, "placeId": place.id})
    except UniqueViolationError:
        pass  # two spellings in one entry folded to the same place — already linked
