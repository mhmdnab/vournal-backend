"""The life index — browsable threads over people/topics/places (Step 17, Phase 2).

Every person, topic, and place the user has mentioned is a thread. The index
(GET /threads?kind=…) is the list, each with its span, moment count, and a mood
sparkline; the detail (GET /threads/{kind}/{id}) opens one into its ordered
moments. Everything is user-scoped, and the join tables are the source of truth —
entries are read-only here. Queries are grouped and hit the reverse indexes on the
join FKs (Step 17 migration 7), so there is no N+1.
"""

from __future__ import annotations

from statistics import mean

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user
from app.db import db
from app.entities import normalize_name
from app.schemas import (
    DisambiguationCandidate,
    DisambiguationOut,
    MergeRequest,
    MoveMomentRequest,
    ThreadDetail,
    ThreadKind,
    ThreadMoment,
    ThreadSummary,
)

router = APIRouter(prefix="/threads", tags=["threads"])

# kind -> (entity table, join table, join FK). A FIXED set, so splicing these
# identifiers into SQL is safe — `kind` is validated by the ThreadKind Literal and
# never reaches here as arbitrary input.
_KINDS: dict[str, tuple[str, str, str]] = {
    "people": ("Person", "EntryPerson", "personId"),
    "topics": ("Topic", "EntryTopic", "topicId"),
    "places": ("Place", "EntryPlace", "placeId"),
}

# Sparkline cap — the most recent N moods keep the index payload small.
_MOOD_SERIES_CAP = 30


@router.get("", response_model=list[ThreadSummary])
async def list_threads(
    kind: ThreadKind = Query(..., description="people | topics | places"),
    current=Depends(get_current_user),
) -> list[ThreadSummary]:
    """The index for one kind, most-relevant first (moments desc, recency tiebreak).
    One grouped query; the mood series is built per thread from its ordered rows."""
    tbl, join, fk = _KINDS[kind]
    rows = await db.query_raw(
        f'''SELECT x."id" AS id, x."name" AS name,
                   e."createdAt" AS created, e."moodScore" AS mood
            FROM "{tbl}" x
            JOIN "{join}" j ON j."{fk}" = x."id"
            JOIN "Entry" e ON e."id" = j."entryId"
            WHERE x."userId" = $1
            ORDER BY x."id", e."createdAt"''',
        current.id,
    )

    # Rows arrive grouped by thread and ascending by date, so first/last fall out.
    threads: dict[str, dict] = {}
    for r in rows:
        t = threads.get(r["id"])
        if t is None:
            t = threads[r["id"]] = {
                "name": r["name"],
                "count": 0,
                "first": r["created"],
                "last": r["created"],
                "moods": [],
            }
        t["count"] += 1
        t["last"] = r["created"]
        if r["mood"] is not None:
            t["moods"].append(int(r["mood"]))

    out = [
        ThreadSummary(
            id=tid,
            kind=kind,
            name=t["name"],
            momentCount=t["count"],
            firstDate=t["first"],
            lastDate=t["last"],
            moodSeries=t["moods"][-_MOOD_SERIES_CAP:],
        )
        for tid, t in threads.items()
    ]
    out.sort(key=lambda s: (s.momentCount, s.lastDate), reverse=True)
    return out


@router.get("/{kind}/{thread_id}", response_model=ThreadDetail)
async def get_thread(
    kind: ThreadKind,
    thread_id: str,
    current=Depends(get_current_user),
) -> ThreadDetail:
    """One thread opened up: its span, average mood, and ordered moments. 404 if the
    id isn't this user's (ownership is checked before any moment is read)."""
    tbl, join, fk = _KINDS[kind]

    owner = await db.query_raw(
        f'SELECT "name" FROM "{tbl}" WHERE "id" = $1 AND "userId" = $2',
        thread_id,
        current.id,
    )
    if not owner:
        raise HTTPException(status_code=404, detail={"code": "THREAD_NOT_FOUND"})

    rows = await db.query_raw(
        f'''SELECT e."id" AS entry_id, e."createdAt" AS created,
                   e."moodScore" AS mood, e."summary" AS summary
            FROM "{join}" j
            JOIN "Entry" e ON e."id" = j."entryId"
            WHERE j."{fk}" = $1 AND e."userId" = $2
            ORDER BY e."createdAt"''',
        thread_id,
        current.id,
    )
    if not rows:  # a thread with no visible moments — nothing to show
        raise HTTPException(status_code=404, detail={"code": "THREAD_NOT_FOUND"})

    moments = [
        ThreadMoment(
            entryId=r["entry_id"],
            date=r["created"],
            moodScore=r["mood"],
            summary=r["summary"] or "",
        )
        for r in rows
    ]
    moods = [m.moodScore for m in moments if m.moodScore is not None]
    return ThreadDetail(
        id=thread_id,
        kind=kind,
        name=owner[0]["name"],
        momentCount=len(moments),
        firstDate=moments[0].date,
        lastDate=moments[-1].date,
        averageMood=round(mean(moods), 1) if moods else None,
        moments=moments,
    )


# ---- Correction (Step 17, Phase 4) ------------------------------------------
# All non-destructive: entries are the source of truth, threads are a view over
# the join tables. Merge/move/split reassign links and touch no entry.

# kind -> (entity Prisma model, join Prisma model, join FK column).
_MODELS = {
    "people": ("person", "entryperson", "personId"),
    "topics": ("topic", "entrytopic", "topicId"),
    "places": ("place", "entryplace", "placeId"),
}


def _models(kind: str):
    ent, join, fk = _MODELS[kind]
    return getattr(db, ent), getattr(db, join), fk


async def _owned_entity(model, entity_id: str, user_id: str):
    ent = await model.find_first(where={"id": entity_id, "userId": user_id})
    if ent is None:
        raise HTTPException(status_code=404, detail={"code": "THREAD_NOT_FOUND"})
    return ent


def _folded_aliases(dst, src) -> list[str]:
    """dst keeps its display name and gains src's name + aliases as aliases, so a
    merge preserves both surface forms. Deduped by normalized form; never re-adds
    dst's own canonical."""
    seen = {dst.normalizedName}
    result: list[str] = []
    for alias in [*(dst.aliases or []), src.name, *(src.aliases or [])]:
        norm = normalize_name(alias)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(alias.strip())
    return result


async def _last_moment(join_table: str, fk: str, entity_id: str):
    rows = await db.query_raw(
        f'''SELECT MAX(e."createdAt") AS last FROM "{join_table}" j
            JOIN "Entry" e ON e."id" = j."entryId" WHERE j."{fk}" = $1''',
        entity_id,
    )
    return rows[0]["last"] if rows else None


async def _top_cotopics(join_table: str, fk: str, entity_id: str, limit: int = 2) -> list[str]:
    """The topics that most co-occur in this entity's entries — context that helps
    tell two same-named people apart. Excludes the entity itself (for topics)."""
    rows = await db.query_raw(
        f'''SELECT t."name" AS name, COUNT(*) AS c
            FROM "{join_table}" j
            JOIN "EntryTopic" et ON et."entryId" = j."entryId"
            JOIN "Topic" t ON t."id" = et."topicId"
            WHERE j."{fk}" = $1 AND et."topicId" <> $2
            GROUP BY t."id" ORDER BY c DESC, t."name" ASC LIMIT $3''',
        entity_id,
        entity_id,
        limit,
    )
    return [r["name"] for r in rows]


@router.get("/{kind}/{thread_id}/disambiguate", response_model=DisambiguationOut)
async def disambiguate(
    kind: ThreadKind,
    thread_id: str,
    current=Depends(get_current_user),
) -> DisambiguationOut:
    """The candidates a shared surface form could mean — with distinguishing
    context — plus a distinct default name for "Someone new"."""
    model, join, fk = _models(kind)
    _, join_table, _ = _KINDS[kind]  # SQL table name (e.g. "EntryPerson")
    ent = await _owned_entity(model, thread_id, current.id)
    norm = ent.normalizedName

    rows = await model.find_many(where={"userId": current.id})
    matches = [
        r
        for r in rows
        if r.normalizedName == norm or any(normalize_name(a) == norm for a in (r.aliases or []))
    ]

    candidates: list[DisambiguationCandidate] = []
    for c in matches:
        candidates.append(
            DisambiguationCandidate(
                id=c.id,
                name=c.name,
                momentCount=await join.count(where={fk: c.id}),
                topTopics=await _top_cotopics(join_table, fk, c.id),
                lastMoment=await _last_moment(join_table, fk, c.id),
            )
        )
    candidates.sort(key=lambda c: c.momentCount, reverse=True)

    # A distinct default for a new entity: "<name> · <its top topic>", else "<name> (new)".
    top = candidates[0].topTopics[0] if candidates and candidates[0].topTopics else None
    suggested = f"{ent.name} · {top}" if top else f"{ent.name} (new)"

    return DisambiguationOut(
        kind=kind, name=ent.name, candidates=candidates, suggestedName=suggested
    )


@router.post("/{kind}/{thread_id}/merge")
async def merge_thread(
    kind: ThreadKind,
    thread_id: str,
    body: MergeRequest,
    current=Depends(get_current_user),
):
    """Absorb this thread INTO body.intoId: reassign every moment, fold the surface
    forms in as aliases, delete the now-empty source. Touches no entry."""
    if body.intoId == thread_id:
        raise HTTPException(status_code=400, detail={"code": "MERGE_INTO_SELF"})
    model, join, fk = _models(kind)
    src = await _owned_entity(model, thread_id, current.id)
    dst = await _owned_entity(model, body.intoId, current.id)

    dst_entries = {l.entryId for l in await join.find_many(where={fk: dst.id})}
    for link in await join.find_many(where={fk: src.id}):
        if link.entryId not in dst_entries:
            await join.create(data={"entryId": link.entryId, fk: dst.id})
            dst_entries.add(link.entryId)

    await model.update(where={"id": dst.id}, data={"aliases": _folded_aliases(dst, src)})
    await model.delete(where={"id": src.id})  # cascades any remaining src links
    return {"id": dst.id}


@router.post("/{kind}/{thread_id}/moments/{entry_id}/move")
async def move_moment(
    kind: ThreadKind,
    thread_id: str,
    entry_id: str,
    body: MoveMomentRequest,
    current=Depends(get_current_user),
):
    """Reassign one moment off this thread onto another (existing toId, or a new/
    existing one by toName). An optional alias — the surface form being split off —
    is attached to the target so future mentions of it read as ambiguous."""
    model, join, fk = _models(kind)
    src = await _owned_entity(model, thread_id, current.id)

    entry = await db.entry.find_first(where={"id": entry_id, "userId": current.id})
    if entry is None or not await join.find_first(where={"entryId": entry_id, fk: src.id}):
        raise HTTPException(status_code=404, detail={"code": "MOMENT_NOT_ON_THREAD"})

    # Resolve the target thread.
    if body.toId:
        dst = await _owned_entity(model, body.toId, current.id)
    elif body.toName and body.toName.strip():
        norm = normalize_name(body.toName)
        if not norm:
            raise HTTPException(status_code=400, detail={"code": "EMPTY_NAME"})
        dst = await model.find_first(where={"userId": current.id, "normalizedName": norm})
        if dst is None:
            dst = await model.create(
                data={"userId": current.id, "name": body.toName.strip(), "normalizedName": norm}
            )
    else:
        raise HTTPException(status_code=400, detail={"code": "NO_TARGET"})

    if dst.id == src.id:
        return {"id": dst.id}  # no-op

    # Attach the split-off surface as an alias on the target (so ambiguity forms).
    if body.alias:
        an = normalize_name(body.alias)
        existing = {dst.normalizedName, *(normalize_name(a) for a in (dst.aliases or []))}
        if an and an not in existing:
            await model.update(
                where={"id": dst.id}, data={"aliases": [*(dst.aliases or []), body.alias.strip()]}
            )

    # Reassign the single link: drop it from src, add to dst (idempotent).
    await join.delete_many(where={"entryId": entry_id, fk: src.id})
    if not await join.find_first(where={"entryId": entry_id, fk: dst.id}):
        await join.create(data={"entryId": entry_id, fk: dst.id})
    return {"id": dst.id}
