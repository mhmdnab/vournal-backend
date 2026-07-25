"""Cutover cleanup (Step 11 audit #1): remove seed/test accounts from production.

The demo account (a publicly-known credential) and any test accounts must not
exist once real users are on the app. This deletes each account's audio objects
from the bucket FIRST (the DB cascade can't reach external storage), then the user
row — cascading entries, todos, topics, people, refresh + reset tokens. Same order
as DELETE /auth/me.

Run at launch cutover — NOT during testing (it destroys those accounts' data):
    .venv/bin/python -m scripts.remove_demo_account
"""

import asyncio

from dotenv import load_dotenv

from app import storage
from app.db import db

# Accounts that must not exist in production. Add any throwaway test emails here.
SEED_EMAILS = ["demo@voicejournal.local"]


async def main() -> None:
    load_dotenv()
    await db.connect()
    try:
        for email in SEED_EMAILS:
            user = await db.user.find_unique(where={"email": email})
            if user is None:
                print(f"  {email}: not present (ok)")
                continue
            entries = await db.entry.find_many(where={"userId": user.id})
            for entry in entries:
                if entry.audioUrl:
                    await storage.delete_audio(entry.audioUrl)
            await db.user.delete(where={"id": user.id})
            print(f"  {email}: deleted ({len(entries)} entries + their audio objects)")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
