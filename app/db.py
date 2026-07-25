"""Prisma client lifecycle and demo-user helper.

A single shared Prisma() client is used across the app. It is connected and
disconnected via the FastAPI lifespan (see app/main.py).
"""

from prisma import Prisma

# One shared client for the whole process.
db = Prisma()

# The pre-auth demo account. Since Step 5 (auth) it's no longer a fallback —
# every request now derives its user from a bearer token. The constant remains
# only so scripts/set_demo_password.py can convert this legacy row into a real,
# loginable account (preserving its existing entries).
DEMO_USER_EMAIL = "demo@voicejournal.local"


async def connect() -> None:
    """Connect the shared client (called from the FastAPI lifespan)."""
    if not db.is_connected():
        await db.connect()


async def disconnect() -> None:
    """Disconnect the shared client (called from the FastAPI lifespan)."""
    if db.is_connected():
        await db.disconnect()
