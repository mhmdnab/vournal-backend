"""Transactional email via Resend (Step 11).

A thin REST seam over the Resend API (async httpx). Verified against the Resend
docs (2026): POST https://api.resend.com/emails, `Authorization: Bearer <key>`,
JSON body {from, to, subject, html, text}; `from` must use a verified domain.

Fails SOFT: a missing key or a failed send is logged, never raised. The
forgot-password endpoint returns 200 regardless, so email hiccups can't 500 the
user and can't leak whether an address is registered.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("vournal.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "Vournal <noreply@vournal.app>"


async def _send(to: str, subject: str, html: str, text: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.warning("RESEND_API_KEY not set — email to %s not sent.", to)
        return False
    sender = os.environ.get("RESEND_FROM") or DEFAULT_FROM
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": sender,
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


async def send_password_reset(to: str, reset_url: str, ttl_minutes: int) -> bool:
    """Plain, calm reset email. Says the link expires."""
    subject = "Reset your Vournal password"
    text = (
        "You asked to reset your Vournal password.\n\n"
        f"Open this link to choose a new one:\n{reset_url}\n\n"
        f"The link expires in {ttl_minutes} minutes. If you didn't ask for this, "
        "you can ignore this email — your password won't change."
    )
    html = (
        '<div style="font-family:system-ui,-apple-system,sans-serif;color:#2b2926;'
        'line-height:1.6;max-width:440px">'
        "<p>You asked to reset your Vournal password.</p>"
        f'<p><a href="{reset_url}" style="color:#5e8c6a">Choose a new password</a></p>'
        '<p style="color:#6b6558;font-size:14px">'
        f"The link expires in {ttl_minutes} minutes. If you didn't ask for this, you "
        "can ignore this email — your password won't change.</p>"
        "</div>"
    )
    return await _send(to, subject, html, text)


async def send_verification_code(to: str, code: str, note: str | None = None) -> bool:
    lines = [
        f"Your vournal code is {code}. It expires in 10 minutes.",
        *([note] if note else []),
        "If you didn't ask for this, you can ignore this email.",
    ]
    html = "".join(f"<p>{line}</p>" for line in lines)
    return await _send(to, "Your vournal code", html, "\n\n".join(lines))


async def send_plus_confirm_link(to: str, url: str) -> bool:
    """A website purchase is waiting on an account that hasn't confirmed this address."""
    text = (
        "Thanks for subscribing to vournal Plus. To turn it on, confirm that this "
        f"email address is yours: {url}\n\nThe link works for 7 days."
    )
    html = (
        "<p>Thanks for subscribing to vournal Plus. To turn it on, confirm that this "
        f'email address is yours:</p><p><a href="{url}">Confirm my email</a></p>'
        "<p>The link works for 7 days.</p>"
    )
    return await _send(to, "Turn on vournal Plus", html, text)


async def send_trial_ending(to: str, ends_at) -> bool:
    """A week before the free months of Plus end. Outside the app, so it can name both
    ways to subscribe."""
    day = f"{ends_at.day} {ends_at:%B}"
    lines = [
        f"Your free vournal Plus ends on {day}.",
        "Nothing will be charged: the free month ends on its own.",
        "To keep Plus, subscribe in the vournal app or at life.vournal.app.",
        "Your journal and everything in vournal life stay yours either way.",
    ]
    html = "".join(f"<p>{line}</p>" for line in lines)
    return await _send(to, f"Your free vournal Plus ends on {day}", html, "\n\n".join(lines))


def _esc(value: str) -> str:
    import html

    return html.escape(value or "", quote=True)


async def send_workspace_invite(to: str, workspace_name: str, inviter: str, url: str) -> bool:
    """An invitation to a vournal OS workspace (accepted on life.vournal.app)."""
    who = inviter or "Someone"
    text = (
        f"{who} invited you to the workspace \"{workspace_name}\" in vournal OS.\n\n"
        f"Open the invitation: {url}\n\nIt works for 14 days, with the account for this email address."
    )
    html_body = (
        f"<p>{_esc(who)} invited you to the workspace “{_esc(workspace_name)}” in vournal OS.</p>"
        f'<p><a href="{_esc(url)}">Open the invitation</a></p>'
        "<p>It works for 14 days, with the account for this email address.</p>"
    )
    return await _send(to, f"Join {workspace_name} on vournal", html_body, text)


async def send_invite_accepted(to: str, who: str, workspace_name: str) -> bool:
    """To the person who invited them."""
    text = f"{who} joined \"{workspace_name}\" in vournal OS."
    return await _send(to, f"{who} joined {workspace_name}", f"<p>{_esc(text)}</p>", text)


async def send_project_share(to: str, project_name: str, inviter: str, url: str) -> bool:
    """A project shared view-only."""
    who = inviter or "Someone"
    text = (
        f"{who} shared the project \"{project_name}\" with you in vournal OS (view only).\n\n"
        f"Open it: {url}\n\nUse the account for this email address. You don't need vournal Plus to view it."
    )
    html_body = (
        f"<p>{_esc(who)} shared the project “{_esc(project_name)}” with you in vournal OS (view only).</p>"
        f'<p><a href="{_esc(url)}">Open the project</a></p>'
        "<p>Use the account for this email address. You don't need vournal Plus to view it.</p>"
    )
    return await _send(to, f"{who} shared {project_name} with you", html_body, text)
