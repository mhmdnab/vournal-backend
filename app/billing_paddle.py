"""Paddle (web checkout) for vournal Plus (build 5).

Paddle Billing calls POST /billing/paddle/webhook for subscription events. Each
request is authenticated by its Paddle-Signature header (HMAC-SHA256 of
"{ts}:{raw body}" with the notification destination's secret key,
PADDLE_WEBHOOK_SECRET), and rejected if older than five minutes.

The website checkout has no sign-in; it records the vournal email in the
subscription's custom_data.vournalEmail. That email finds the account. A purchase
made before the account exists is kept as a PendingGrant and applied when an
account with that email is created (app.entitlements.apply_pending_grants).

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
    email = ((data.get("custom_data") or {}).get("vournalEmail") or "").strip().lower()
    period_end = (data.get("current_billing_period") or {}).get("ends_at")
    user = None
    if email:
        user = await db.user.find_unique(where={"email": email})
    if user is None and sub_id:
        user = await db.user.find_first(where={"paddleSubscriptionId": sub_id})

    if status in _ACTIVE and period_end and sub_id:
        expires = _when(period_end)
        if user is not None:
            await grant_plus(db, user.id, expires, "paddle", sub_id)
        elif email:
            await db.pendinggrant.upsert(
                where={"externalId": sub_id},
                data={
                    "create": {"email": email, "source": "paddle", "externalId": sub_id, "expiresAt": expires},
                    "update": {"email": email, "expiresAt": expires},
                },
            )
    elif status in _ENDED:
        if user is not None:
            await revoke_plus(db, user.id, "paddle", sub_id)
        if sub_id:
            await db.pendinggrant.delete_many(where={"externalId": sub_id, "appliedAt": None})
    log.info("Paddle %s (%s) handled for user %s", kind, status, user.id if user else None)
    return {"ok": True}
