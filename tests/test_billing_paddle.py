"""Paddle (web) purchases grant the same Plus as Apple.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import hashlib
import hmac
import json
import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

SECRET = "pdl_ntfset_test_secret"
PRICE = "pri_plus_test"


def _sign(raw: bytes, ts: int | None = None, secret: str = SECRET) -> str:
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), f"{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


def _iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _event(kind: str, email: str, sub_id: str, status="active", days=30, price=PRICE) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:8]}",
        "event_type": kind,
        "occurred_at": _iso(0),
        "data": {
            "id": sub_id,
            "status": status,
            "customer_id": "ctm_1",
            "custom_data": {"vournalEmail": email},
            "current_billing_period": {"starts_at": _iso(0), "ends_at": _iso(days)},
            "items": [{"price": {"id": price}, "quantity": 1}],
        },
    }


class Signature(unittest.TestCase):
    def test_signature_valid_and_tampered(self):
        from app.billing_paddle import verify_paddle_signature

        raw = b'{"a":1}'
        now = int(time.time())
        self.assertTrue(verify_paddle_signature(raw, _sign(raw, now), SECRET, now))
        self.assertFalse(verify_paddle_signature(b'{"a":2}', _sign(raw, now), SECRET, now))
        self.assertFalse(verify_paddle_signature(raw, "garbage", SECRET, now))
        self.assertFalse(verify_paddle_signature(raw, _sign(raw, now, "other"), SECRET, now))

    def test_signature_too_old(self):
        from app.billing_paddle import verify_paddle_signature

        raw = b"{}"
        now = int(time.time())
        self.assertFalse(verify_paddle_signature(raw, _sign(raw, now - 301), SECRET, now))


class Webhook(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db

        self.db = db
        await db.connect()
        self.email = f"paddle-{uuid.uuid4().hex}@example.test"
        self.sub = f"sub_{uuid.uuid4().hex[:10]}"
        self.env = patch.dict(
            os.environ, PADDLE_WEBHOOK_SECRET=SECRET, PADDLE_PLUS_PRICE_ID=PRICE
        )
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.db.user.delete_many(where={"email": self.email})
        await self.db.pendinggrant.delete_many(where={"externalId": self.sub})
        await self.db.disconnect()

    async def _send(self, event: dict, signature: str | None = None):
        from app.main import app

        raw = json.dumps(event).encode()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://paddle"
        ) as client:
            return await client.post(
                "/billing/paddle/webhook",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "Paddle-Signature": signature or _sign(raw),
                },
            )

    async def _user(self):
        return await self.db.user.create(data={"email": self.email})

    async def test_subscription_activated_grants_by_email(self):
        from app.entitlements import effective_plan

        user = await self._user()
        response = await self._send(_event("subscription.activated", self.email.upper(), self.sub))
        self.assertEqual(response.status_code, 200, response.text)
        user = await self.db.user.find_unique(where={"id": user.id})
        self.assertEqual(effective_plan(user), "plus")
        self.assertEqual(user.planSource, "paddle")
        self.assertEqual(user.paddleSubscriptionId, self.sub)

    async def test_unknown_email_stores_pending_grant_then_applies_on_register(self):
        from app.entitlements import effective_plan
        from app.main import app

        response = await self._send(_event("subscription.created", self.email, self.sub))
        self.assertEqual(response.status_code, 200, response.text)
        pending = await self.db.pendinggrant.find_unique(where={"externalId": self.sub})
        self.assertIsNotNone(pending)
        from app.rate_limit import limiter

        limiter.reset()  # the suite registers more than 3 accounts per hour
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://paddle"
        ) as client:
            with patch.dict(os.environ, VOURNAL_ENHANCEMENTS="0"):
                registered = await client.post(
                    "/auth/register",
                    json={"email": self.email, "password": "local-passphrase"},
                )
        self.assertEqual(registered.status_code, 200, registered.text)
        user = await self.db.user.find_unique(where={"email": self.email})
        self.assertEqual(effective_plan(user), "plus")
        pending = await self.db.pendinggrant.find_unique(where={"externalId": self.sub})
        self.assertIsNotNone(pending.appliedAt)

    async def test_subscription_canceled_lets_period_run_until_canceled(self):
        from app.entitlements import effective_plan

        user = await self._user()
        await self._send(_event("subscription.activated", self.email, self.sub))
        # A scheduled cancellation is still an active subscription.
        await self._send(_event("subscription.updated", self.email, self.sub, status="active"))
        self.assertEqual(effective_plan(await self.db.user.find_unique(where={"id": user.id})), "plus")
        await self._send(_event("subscription.canceled", self.email, self.sub, status="canceled"))
        self.assertEqual(effective_plan(await self.db.user.find_unique(where={"id": user.id})), "free")

    async def test_other_price_never_grants(self):
        # A sandbox price id can never match the live Plus price configured in
        # production, so sandbox purchases can't grant real Plus.
        from app.entitlements import effective_plan

        user = await self._user()
        await self._send(_event("subscription.activated", self.email, self.sub, price="pri_sandbox"))
        self.assertEqual(effective_plan(await self.db.user.find_unique(where={"id": user.id})), "free")

    async def test_cancel_of_another_subscription_keeps_plus(self):
        from app.entitlements import effective_plan

        user = await self._user()
        await self._send(_event("subscription.activated", self.email, self.sub))
        await self._send(_event("subscription.canceled", self.email, "sub_duplicate", status="canceled"))
        self.assertEqual(effective_plan(await self.db.user.find_unique(where={"id": user.id})), "plus")

    async def test_bad_signature_401_and_no_write(self):
        from app.entitlements import effective_plan

        user = await self._user()
        response = await self._send(
            _event("subscription.activated", self.email, self.sub), signature="ts=1;h1=00"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(effective_plan(await self.db.user.find_unique(where={"id": user.id})), "free")


if __name__ == "__main__":
    unittest.main()
