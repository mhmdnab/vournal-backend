"""Transactional email via Resend (Step 11).

A thin REST seam over the Resend API (async httpx). Verified against the Resend
docs (2026): POST https://api.resend.com/emails, `Authorization: Bearer <key>`,
JSON body {from, to, subject, html, text}; `from` must use a verified domain.

Fails SOFT: a missing key or a failed send is logged, never raised. The
forgot-password endpoint returns 200 regardless, so email hiccups can't 500 the
user and can't leak whether an address is registered.

Every email is built from app/email_layout.py (designs approved 2026-11-24): the vournal
mark, a serif headline, at most one button, and a plain-text twin.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import httpx

from app.email_layout import ACCOUNT_REASON, Button, Code, Facts, LinkFallback, Note, P, render, spaced_code

log = logging.getLogger("vournal.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"
SENDER_NAME = "vournal"
DEFAULT_ADDRESS = "noreply@vournal.app"
DEFAULT_FROM = f"{SENDER_NAME} <{DEFAULT_ADDRESS}>"
_ADDRESS = re.compile(r"<([^>]+)>")


def _sender() -> str:
    """Always "vournal" (owner 2026-11-24), at RESEND_FROM's address when that's set."""
    raw = (os.environ.get("RESEND_FROM") or "").strip()
    match = _ADDRESS.search(raw)
    address = (match.group(1) if match else raw).strip() or DEFAULT_ADDRESS
    return f"{SENDER_NAME} <{address}>"


async def _send(to: str, subject: str, html: str, text: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("RESEND_API_KEY not set — email to %s not sent.", to)
        return False
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": _sender(),
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
            )
        if resp.status_code >= 400:
            log.error("Resend send failed (%s): %s", resp.status_code, resp.text[:300])
            return False
        return True
    except httpx.HTTPError as exc:  # noqa: BLE001 — email is best-effort
        log.error("Resend send error: %s", exc)
        return False


# ----- signing in -------------------------------------------------------------------------


async def send_verification_code(to: str, code: str, note: str | None = None) -> bool:
    blocks = [
        P("Enter this code in vournal to confirm this email address is yours."),
        Code(code),
        *([Note(note)] if note else []),
        P(
            "It works for 10 minutes. If you didn’t ask for it, you can ignore this email."
            + ("" if note else " Nobody can get in without the code."),
            quiet=True,
            gap=22,
        ),
    ]
    preheader = "Entering it connects your sign-in to this account." if note else "It works for 10 minutes."
    html, text = render(title="Your vournal code", preheader=preheader, blocks=blocks, reason=ACCOUNT_REASON)
    return await _send(to, f"{spaced_code(code)} is your vournal code", html, text)


async def send_password_reset(to: str, reset_url: str, ttl_minutes: int) -> bool:
    """Calm reset email. Says the link expires."""
    html, text = render(
        title="Choose a new password",
        preheader=f"The link works for {ttl_minutes} minutes.",
        blocks=[
            P("You asked to reset the password for your vournal account."),
            Button("Choose a new password", reset_url),
            P(
                f"The link works for {ttl_minutes} minutes. If you didn’t ask for this, you can ignore this email. "
                "Your password stays the same.",
                quiet=True,
                gap=26,
            ),
            LinkFallback(reset_url),
        ],
        reason=ACCOUNT_REASON,
    )
    return await _send(to, "Reset your vournal password", html, text)


# ----- vournal Plus -----------------------------------------------------------------------


async def send_plus_confirm_link(to: str, url: str) -> bool:
    """A website purchase is waiting on an account that hasn't confirmed this address."""
    html, text = render(
        title="One step to turn on Plus",
        eyebrow="vournal Plus",
        preheader="One tap and Plus is on.",
        blocks=[
            P("Thanks for subscribing to vournal Plus. Confirm that this email address is yours and Plus turns on right away."),
            Button("Confirm my email", url),
            P("The link works for 7 days.", quiet=True, gap=26),
            LinkFallback(url),
        ],
        reason=ACCOUNT_REASON,
    )
    return await _send(to, "Confirm your email to turn on vournal Plus", html, text)


def _day(when: datetime) -> str:
    return f"{when:%A}, {when.day} {when:%B}"


def _short_day(when: datetime) -> str:
    return f"{when.day} {when:%B}"


FREE_MONTH_REASON = "You’re getting this because your free month of vournal Plus is ending."


async def send_free_month_ending(
    to: str, ends_at: datetime, days_left: int, *, renews: bool, billed_by: str, manage_url: str
) -> bool:
    """A few days before a free month of Plus ends (owner 2026-11-24). `ends_at` is already in
    the person's time zone. Two versions: it will renew, or they cancelled."""
    in_days = "tomorrow" if days_left == 1 else f"in {days_left} days"
    day, short = _day(ends_at), _short_day(ends_at)
    apple = billed_by == "apple"
    if renews:
        billed = "Apple, to your Apple Account" if apple else "Paddle, our reseller, to the card you added"
        cancel_how = (
            "on your iPhone, open Settings, tap your name, then Subscriptions."
            if apple else "use the link below."
        )
        html, text = render(
            title=f"Your free month ends {in_days}",
            eyebrow="vournal Plus",
            preheader=f"On {short} it becomes $9.99 a month, billed by {'Apple' if apple else 'Paddle'}.",
            blocks=[
                P(f"Your free month of vournal Plus ends on {day}. After that, "
                  + ("Apple bills you every month until you cancel." if apple else "you’re billed every month until you cancel.")),
                Facts([
                    ("Free until", day),
                    ("Then", "$9.99 a month, or your local App Store price" if apple else "$9.99 a month"),
                    ("Billed by", billed),
                ]),
                P("**Keeping Plus?** You don’t need to do anything.", gap=24),
                P(f"**Not for you?** Cancel before {short} and you won’t be charged: {cancel_how}", gap=10),
                Button("Manage my subscription" if apple else "Manage or cancel", manage_url),
                P("If you cancel, your journal and everything in vournal life stay yours. "
                  "Your account goes back to the free plan.", quiet=True, gap=26),
            ],
            reason=FREE_MONTH_REASON,
        )
        return await _send(to, f"Your free month of vournal Plus ends {in_days}", html, text)

    html, text = render(
        title=f"Plus ends on {ends_at:%A}",
        eyebrow="vournal Plus",
        preheader="You cancelled, so nothing will be charged.",
        blocks=[
            P(f"You cancelled, so nothing will be charged. Plus stays on until {day}, "
              "then your account goes back to the free plan."),
            Facts([
                ("Entries a month", "150 → 40"),
                ("Longest recording", "5 minutes → 1.5 minutes"),
                ("AI actions and vournal life", "Included → Not included"),
            ]),
            P("Your journal and everything in vournal life stay. If you change your mind, "
              "you can turn Plus back on before or after it ends.", gap=24),
            Button("Keep vournal Plus", manage_url),
        ],
        reason=FREE_MONTH_REASON,
    )
    return await _send(to, f"Your vournal Plus ends {in_days}", html, text)


async def send_trial_ending(to: str, ends_at) -> bool:
    """A week before the server's own free months end (dormant: PLUS_TRIAL_FROM is unset).
    Nothing is ever charged for those, so it's the "ends" version."""
    day = _day(ends_at)
    html, text = render(
        title=f"Your free vournal Plus ends on {ends_at:%A}",
        eyebrow="vournal Plus",
        preheader="Nothing will be charged: the free month ends on its own.",
        blocks=[
            P(f"Your free vournal Plus ends on {day}. Nothing will be charged: the free month ends on its own."),
            P("To keep Plus, subscribe in the vournal app. Your journal and everything in vournal life stay yours either way.", gap=12),
        ],
        reason=FREE_MONTH_REASON,
    )
    return await _send(to, f"Your free vournal Plus ends on {_short_day(ends_at)}", html, text)


async def send_web_checkout_open(to: str) -> bool:
    """To the accounts that asked on life.vournal.app to hear when web checkout opens."""
    html, text = render(
        title="Plus is on the web now",
        eyebrow="vournal Plus",
        preheader="Start with a free month on life.vournal.app.",
        blocks=[
            P("You asked us to tell you when you could subscribe on life.vournal.app. You can now."),
            P("Start with a free month: nothing is charged for the first month, then it’s $9.99 a month until you cancel.", gap=12),
            Button("Start my free month", "https://life.vournal.app"),
        ],
        reason="You asked to hear about this on life.vournal.app. We won’t email you about it again.",
    )
    return await _send(to, "You can now get vournal Plus on the web", html, text)


# ----- sharing in vournal life ------------------------------------------------------------


async def send_workspace_invite(to: str, workspace_name: str, inviter: str, url: str) -> bool:
    """An invitation to a vournal life workspace (accepted on life.vournal.app)."""
    who = inviter or "Someone"
    html, text = render(
        title=f"Join “{workspace_name}”",
        eyebrow="vournal life",
        preheader="The invitation works for 14 days.",
        blocks=[
            P(f"{who} invited you to the workspace “{workspace_name}” in vournal life, to keep its tasks, notes and calendar together."),
            Button("Open the invitation", url),
            P("It works for 14 days. Open it with the vournal account for this email address, or create one.", quiet=True, gap=26),
        ],
        reason=f"You’re getting this because {who} invited this email address.",
    )
    return await _send(to, f"{who} invited you to “{workspace_name}” on vournal", html, text)


async def send_invite_accepted(to: str, who: str, workspace_name: str) -> bool:
    """To the person who invited them."""
    html, text = render(
        title=f"{who} joined “{workspace_name}”",
        eyebrow="vournal life",
        preheader=f"{who} accepted your invitation.",
        blocks=[
            P(f"{who} accepted your invitation and is now in the workspace."),
            Button("Open vournal life", "https://life.vournal.app"),
        ],
        reason=f"You’re getting this because you invited {who} to “{workspace_name}”.",
    )
    return await _send(to, f"{who} joined “{workspace_name}”", html, text)


async def send_project_share(to: str, project_name: str, inviter: str, url: str) -> bool:
    """A project shared view-only."""
    who = inviter or "Someone"
    html, text = render(
        title=f"“{project_name}” was shared with you",
        eyebrow="vournal life",
        preheader="You can view it without vournal Plus.",
        blocks=[
            P(f"{who} shared this project with you in vournal life. You can view it; only {who} can change it."),
            Button("Open the project", url),
            P("Use the vournal account for this email address. You don’t need vournal Plus to view it.", quiet=True, gap=26),
        ],
        reason=f"You’re getting this because {who} shared a project with this email address.",
    )
    return await _send(to, f"{who} shared “{project_name}” with you", html, text)
