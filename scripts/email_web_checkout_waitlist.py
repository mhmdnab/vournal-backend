"""Email the accounts that asked to hear when web checkout opens. Run it ONCE checkout is live
(the NEXT_PUBLIC_PADDLE_* values set on Vercel and the Subscribe button showing on life.vournal.app).

It uses the database and Resend key in the backend's .env (production). Without --send it only
prints how many accounts are waiting; it never prints their addresses.

Usage:
    .venv/bin/python scripts/email_web_checkout_waitlist.py           # count only
    .venv/bin/python scripts/email_web_checkout_waitlist.py --send    # email each one once
"""

import asyncio
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, ".env"))

from app.db import db  # noqa: E402
from app.web_checkout_waitlist import email_waitlist  # noqa: E402


async def main() -> None:
    send = "--send" in sys.argv[1:]
    await db.connect()
    try:
        waiting, delivered = await email_waitlist(send=send)
    finally:
        await db.disconnect()
    if send:
        print(f"{delivered} of {waiting} emails delivered; the rest stay on the list for the next run.")
    else:
        print(f"{waiting} accounts are waiting. Run again with --send to email them.")


if __name__ == "__main__":
    asyncio.run(main())
