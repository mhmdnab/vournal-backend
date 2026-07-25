"""One-off (Step 11, Phase 1): move existing local recordings to Supabase Storage.

Every entry whose `audioUrl` is still a LOCAL PATH (contains a slash — the local
scheme was `uploads/audio/{id}.ext`) gets its file uploaded to the bucket and its
`audioUrl` rewritten to the object KEY (`{id}.ext`), so existing entries keep
playing after the storage swap. Entries already keyed (no slash) are skipped, so
this is safe to re-run.

Run from the backend root, with the Supabase + DATABASE_URL env set:
    .venv/bin/python -m scripts.migrate_audio_to_supabase
"""

import asyncio
import os

from dotenv import load_dotenv

from app import storage
from app.db import db


def _looks_local(audio_url: str) -> bool:
    # Local paths contain a separator ("uploads/audio/x.m4a"); bucket keys don't.
    return "/" in audio_url or "\\" in audio_url


async def main() -> None:
    load_dotenv()
    await db.connect()
    migrated = skipped = missing = failed = 0
    try:
        entries = await db.entry.find_many(where={"NOT": [{"audioUrl": None}]})
        print(f"{len(entries)} entries have audioUrl set.")
        for e in entries:
            au = e.audioUrl or ""
            if not _looks_local(au):
                skipped += 1
                continue
            if not os.path.isfile(au):
                print(f"  MISSING local file for {e.id}: {au!r}")
                missing += 1
                continue
            ext = os.path.splitext(au)[1].lstrip(".").lower() or "m4a"
            try:
                with open(au, "rb") as f:
                    data = f.read()
                key = await storage.save_audio(e.id, ext, data)
                await db.entry.update(where={"id": e.id}, data={"audioUrl": key})
                migrated += 1
                print(f"  migrated {e.id} -> {key}")
            except Exception as exc:  # noqa: BLE001 — report and continue
                print(f"  FAILED {e.id}: {exc}")
                failed += 1
    finally:
        await db.disconnect()

    print(
        f"\nDone. migrated={migrated}  already-keyed={skipped}  "
        f"missing-local={missing}  failed={failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
