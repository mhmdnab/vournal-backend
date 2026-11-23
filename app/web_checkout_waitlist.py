"""The accounts waiting for web checkout (owner 2026-11-23: Paddle's verification has to wait).

They asked on life.vournal.app (PUT /api/users/me/web-checkout-notify). Once checkout is open,
scripts/email_web_checkout_waitlist.py emails each one once. Accounts that got Plus in the
meantime are skipped (and keep their flag, harmlessly); a delivered email clears the flag, a
failed one stays for the next run.
"""

from app.db import db
from app.email import send_web_checkout_open
from app.entitlements import effective_plan


async def waitlist() -> list:
    """Free accounts (or Plus that lapsed) that asked to be emailed."""
    rows = await db.usersettings.find_many(
        where={"webCheckoutNotifyAt": {"not": None}}, include={"user": True}, order={"webCheckoutNotifyAt": "asc"}
    )
    return [r.user for r in rows if r.user is not None and effective_plan(r.user) == "free"]


async def email_waitlist(send: bool) -> tuple[int, int]:
    """Returns (waiting, delivered). With send=False it only counts."""
    users = await waitlist()
    if not send:
        return len(users), 0
    delivered = 0
    for user in users:
        if await send_web_checkout_open(user.email):
            await db.usersettings.update(where={"userId": user.id}, data={"webCheckoutNotifyAt": None})
            delivered += 1
    return len(users), delivered
