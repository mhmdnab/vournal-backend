"""One-off migration: convert the legacy demo user into a real login.

Before Step 5, `demo@voicejournal.local` was a passwordless fallback owning all
the dev/test entries. Auth now requires a password, so rather than delete that
account and its data, this attaches it to a real, loginable account by setting a
password on it. Its existing entries then belong to an account you can sign into.

Run once from voice-journal-backend/:
    .venv/bin/python scripts/set_demo_password.py [password]

Prints the email + password to use. Idempotent — re-running just resets the
password. If the demo user doesn't exist, it does nothing.
"""

import asyncio
import os
import sys

# Make `app` importable when run as `scripts/set_demo_password.py` (Python puts
# the script's own dir on the path, not the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.db import DEMO_USER_EMAIL, db  # noqa: E402
from app.security import hash_password  # noqa: E402

DEFAULT_PASSWORD = "vournal-demo-2026"


async def main() -> None:
    password = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PASSWORD

    await db.connect()
    try:
        user = await db.user.find_unique(where={"email": DEMO_USER_EMAIL})
        if user is None:
            print(f"No demo user ({DEMO_USER_EMAIL}) found — nothing to do.")
            return

        entry_count = await db.entry.count(where={"userId": user.id})
        await db.user.update(
            where={"id": user.id},
            data={"passwordHash": hash_password(password)},
        )
        print("Demo account converted to a real login.")
        print(f"  email:    {DEMO_USER_EMAIL}")
        print(f"  password: {password}")
        print(f"  owns {entry_count} existing entries.")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
