"""/api/journal: the app's journal on the web, read-only (spec "Journal on the web"). It
reuses the code behind the app's GET /entries and GET /entries/{id}: the list with
summary, mood, topics, people and to-dos, an entry with its transcript, and its audio
through a short-lived signed link. The storage path itself is never sent; `hasAudio` is."""

from fastapi import Depends, HTTPException, Request

from app.db import db
from app.review import IN_JOURNAL
from app.os.common import dates
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.query import _query_int, unknown_params

router = nest_router("/journal")


def _iso(moment) -> str:
    return dates._utc(moment).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _entry(out) -> dict:
    data = out.model_dump(exclude={"reviewState", "inJournal", "review"})  # the app's review card only
    data["hasAudio"] = bool(data.pop("audioUrl", None))
    return data


async def _owned(entry_id: str, user_id: str):
    from app import main  # the app's own helpers (loaded with the app)

    try:
        return await main._owned_entry_or_404(entry_id, user_id)
    except HTTPException as exc:
        raise nest_error(exc.status_code, exc.detail) from None


@router.get("")
async def list_entries(request: Request, user=Depends(plus_user)):
    """Newest first; `?before=<createdAt>` pages back, `?limit=` 1–50 (default 20)."""
    from app import main

    errors = unknown_params(request, ("before", "limit"))
    before = request.query_params.get("before")
    if before is not None and not dates.is_iso_date_string(before):
        errors.append("before must be a valid ISO 8601 date string")
    limit = _query_int(request, "limit", errors, maximum=50, minimum=1)
    if errors:
        raise nest_error(400, errors)
    limit = limit or 20
    where = {
        "userId": user.id,
        # a silent recording that failed is hidden, as in the app; a real failure keeps its audio
        "NOT": {"status": "FAILED", "rawTranscript": "", "audioUrl": None},
        **IN_JOURNAL,
        **({"createdAt": {"lt": dates.parse_instant(before)}} if before else {}),
    }
    rows = await db.entry.find_many(where=where, order={"createdAt": "desc"}, take=limit + 1, include=main._ENTRY_INCLUDE)
    page = rows[:limit]
    return {"items": [_entry(main._to_entry_out(e)) for e in page],
            "nextBefore": _iso(page[-1].createdAt) if len(rows) > limit else None}


@router.get("/{entry_id}")
async def get_entry(entry_id: str, user=Depends(plus_user)):
    from app import main
    from app.entities import ambiguous_norm_sets

    entry = await _owned(entry_id, user.id)
    return _entry(main._to_entry_out(entry, await ambiguous_norm_sets(db, user.id)))


@router.get("/{entry_id}/audio")
async def get_audio(entry_id: str, user=Depends(plus_user)):
    """A short-lived link to the recording, as the app's GET /entries/{id}/audio mints it."""
    from app import main

    entry = await _owned(entry_id, user.id)
    if not entry.audioUrl:
        raise nest_error(404, "Entry has no audio")
    url = await main.create_signed_url(entry.audioUrl)
    if url is None:
        raise nest_error(404, "Audio is unavailable")
    return {"url": url}
