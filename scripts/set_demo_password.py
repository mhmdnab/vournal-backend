"""One-off migration: convert the legacy demo user into a real login.

Before Step 5, `demo@voicejournal.local` was a passwordless fallback owning all
the dev/test entries. Auth now requires a password, so rather than delete that
account and its data, this attaches it to a real, loginable account by setting a
password on it. Its existing entries then belong to an account you can sign into.

Run from voice-journal-backend/, giving the password either way:
    DEMO_PASSWORD='…' .venv/bin/python scripts/set_demo_password.py
    .venv/bin/python scripts/set_demo_password.py '<password>'

There is NO default password. An earlier version of this script had one, written
here in the source, which made it a credential anyone with the repo could use.
Prefer DEMO_PASSWORD: an argument is saved in your shell history.

Idempotent — re-running just resets the password. If the demo user doesn't
exist, it does nothing.
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
from app.schemas import MIN_PASSWORD_LEN  # noqa: E402
from app.security import hash_password  # noqa: E402


async def main() -> None:
    password = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEMO_PASSWORD", "")
    if len(password) < MIN_PASSWORD_LEN:
        sys.exit(
            "Give the new password in DEMO_PASSWORD or as the first argument "
            f"(at least {MIN_PASSWORD_LEN} characters). There is no default."
        )

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
        print("Demo account password set.")
        print(f"  email: {DEMO_USER_EMAIL}")
        print(f"  owns {entry_count} existing entries.")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
