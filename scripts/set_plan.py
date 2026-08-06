"""DEV-ONLY: set a user's subscription plan for testing (Step 14a).

There is no Paddle yet, so this manual CLI is how you grant a test account a paid
plan. It writes directly to the DB the backend's .env points at — it is NOT an
API endpoint and is never reachable in production; only someone with the repo +
DB credentials can run it. Step 14b's Paddle webhook will write these same fields
automatically.

Usage:
    python scripts/set_plan.py <email> <plan> [days]

    plan   free | plus
    days   optional integer offset for planExpiresAt (now + days).
             - omit        -> no expiry (a lifetime/manual grant)
             - 30          -> expires in 30 days
             - -1          -> already expired (to test the fail-safe lapse to free)
           Ignored for `free`, which clears the plan + expiry.

Examples:
    python scripts/set_plan.py demo@voicejournal.local plus 30
    python scripts/set_plan.py demo@voicejournal.local plus
    python scripts/set_plan.py demo@voicejournal.local free
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# Make `app` importable and load the backend .env regardless of the cwd.
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, ".env"))

from prisma import Prisma  # noqa: E402

from app.entitlements import PLANS, effective_plan  # noqa: E402


def _usage_and_exit() -> None:
    print(f"usage: python scripts/set_plan.py <email> <{'|'.join(PLANS)}> [days]")
    sys.exit(1)


async def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2 or args[1] not in PLANS:
        _usage_and_exit()

    email = args[0].strip().lower()
    plan = args[1]
    days = None
    if len(args) > 2:
        try:
            days = int(args[2])
        except ValueError:
            _usage_and_exit()

    # free clears everything; a paid plan gets an expiry only if `days` was given.
    expires = None
    if plan != "free" and days is not None:
        expires = datetime.now(timezone.utc) + timedelta(days=days)

    db = Prisma()
    await db.connect()
    try:
        user = await db.user.find_unique(where={"email": email})
        if user is None:
            print(f"no user with email {email!r}")
            sys.exit(1)
        updated = await db.user.update(
            where={"id": user.id},
            data={"plan": plan, "planExpiresAt": expires},
        )
        print(f"set {email}")
        print(f"  stored plan      : {updated.plan}")
        print(f"  planExpiresAt    : {updated.planExpiresAt}")
        print(f"  EFFECTIVE plan   : {effective_plan(updated)}")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
