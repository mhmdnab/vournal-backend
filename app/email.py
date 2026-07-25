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
                json={"from": sender, "to": [to], "subject": subject, "html": html, "text": text},
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
