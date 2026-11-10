"""Paddle (web checkout) for vournal Plus (build 5).

Paddle Billing calls POST /billing/paddle/webhook for subscription events. Each
request is authenticated by its Paddle-Signature header (HMAC-SHA256 of
"{ts}:{raw body}" with the notification destination's secret key,
PADDLE_WEBHOOK_SECRET), and rejected if older than five minutes.

The dashboard's checkout runs signed in and records the account in the
subscription's custom_data.vournalUserId (and the email in vournalEmail); that
account is granted at once. A checkout that carries only an email (the old website
one) finds the account by that email, and grants only to a confirmed account; a
purchase made before the account exists is kept as a PendingGrant and applied when
an account with that email is confirmed (app.entitlements.apply_pending_grants).

Only subscriptions for PADDLE_PLUS_PRICE_ID count. Sandbox and live Paddle have
different price ids, so a sandbox purchase can never grant Plus on a backend
configured with the live price (and with no price configured, nothing grants).

Entitlement writes go through app.entitlements.grant_plus / revoke_plus only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from app import email as mailer
from app.db import db
from app.entitlements import grant_plus, revoke_plus

log = logging.getLogger("vournal.billing.paddle")

TOLERANCE_SECONDS = 300
_ACTIVE = {"active", "trialing", "past_due"}
_ENDED = {"canceled", "paused"}


def verify_paddle_signature(raw: bytes, header: str, secret: str, now: int) -> bool:
    try:
        parts = dict(part.split("=", 1) for part in header.split(";"))
        ts = int(parts["ts"])
        received = parts["h1"]
    except (KeyError, ValueError):
        return False
    if abs(now - ts) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


router = APIRouter(prefix="/billing/paddle", tags=["billing"])


async def _email_confirm_link(request: Request, user) -> None:
    from app.auth import begin_confirm_link

    base = (os.environ.get("RENDER_EXTERNAL_URL") or str(request.base_url)).rstrip("/")
    token = await begin_confirm_link(user)
    if not await mailer.send_plus_confirm_link(user.email, f"{base}/auth/email/confirm-link?token={token}"):
        log.error("Plus confirm link for user %s could not be sent.", user.id)


@router.post("/webhook")
async def webhook(request: Request):
    secret = os.environ.get("PADDLE_WEBHOOK_SECRET", "")
    raw = await request.body()
    if not secret or not verify_paddle_signature(
        raw, request.headers.get("Paddle-Signature", ""), secret, int(time.time())
    ):
        raise HTTPException(401, {"code": "INVALID_SIGNATURE"})

    event = json.loads(raw)
    kind = event.get("event_type", "")
    data = event.get("data") or {}
    if not kind.startswith("subscription."):
        return {"ok": True}

    plus_price = os.environ.get("PADDLE_PLUS_PRICE_ID", "")
    prices = {
        (item.get("price") or {}).get("id") for item in (data.get("items") or [])
    }
    if not plus_price or plus_price not in prices:
        log.info("Paddle %s for another price ignored", kind)
        return {"ok": True}

    sub_id = data.get("id")
    status = data.get("status")
    custom = data.get("custom_data") or {}
    email = (custom.get("vournalEmail") or "").strip().lower()
    user_id = str(custom.get("vournalUserId") or "").strip()
    period_end = (data.get("current_billing_period") or {}).get("ends_at")
    user = None
    signed_in = False
    if user_id:
        user = await db.user.find_unique(where={"id": user_id})
        signed_in = user is not None
    if user is None and email:
        user = await db.user.find_unique(where={"email": email})
    if user is None and sub_id:
        user = await db.user.find_first(where={"paddleSubscriptionId": sub_id})

    if status in _ACTIVE and period_end and sub_id:
        expires = _when(period_end)
        # Only a confirmed account: anyone can register with someone else's email.
        if user is not None and (signed_in or user.emailVerifiedAt or user.paddleSubscriptionId == sub_id):
            await grant_plus(db, user.id, expires, "paddle", sub_id)
        elif email:
            first_notice = (
                await db.pendinggrant.find_unique(where={"externalId": sub_id}) is None
            )
            await db.pendinggrant.upsert(
                where={"externalId": sub_id},
                data={
                    "create": {"email": email, "source": "paddle", "externalId": sub_id, "expiresAt": expires},
                    "update": {"email": email, "expiresAt": expires},
                },
            )
            # They've paid and have an account: every account from before build 5 is
            # unconfirmed, and a build 4 phone can't ask. Email them the way in.
            if user is not None and first_notice:
                await _email_confirm_link(request, user)
    elif status in _ENDED:
        if user is not None:
            await revoke_plus(db, user.id, "paddle", sub_id)
        if sub_id:
            await db.pendinggrant.delete_many(where={"externalId": sub_id, "appliedAt": None})
    log.info("Paddle %s (%s) handled for user %s", kind, status, user.id if user else None)
    return {"ok": True}
