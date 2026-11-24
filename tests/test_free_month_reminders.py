"""The email 3 days before a free month of Plus ends (owner 2026-11-24). Apple's signed
transaction says when a free month ends; its renewal info says whether it will renew; Paddle
says "trialing". One email per free month, in the version that fits.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check test_free_month_reminders
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx

PRODUCT = "app.vournal.mobile.plus.monthly"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _txn(token, ends: datetime, free=True):
    return SimpleNamespace(
        purchaseDate=_ms(datetime.now(timezone.utc) - timedelta(minutes=1)),
        productId=PRODUCT,
        originalTransactionId=f"orig-{uuid.uuid4().hex[:8]}",
        transactionId=f"txn-{uuid.uuid4().hex[:6]}",
        expiresDate=_ms(ends),
        appAccountToken=token,
        revocationDate=None,
        rawOfferType=1 if free else None,
        rawOfferDiscountType="FREE_TRIAL" if free else None,
    )


class FreeMonthReminders(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db

        self.db = db
        self.security = security
        await db.connect()
        self.users = []
        self.now = datetime.now(timezone.utc)
        self.mails = []

        async def capture(to, ends_at, days_left, *, renews, billed_by, manage_url):
            self.mails.append({"to": to, "ends": ends_at, "days": days_left, "renews": renews, "billed_by": billed_by, "manage": manage_url})
            return True

        self.patch = patch("app.free_month_reminders.mailer.send_free_month_ending", capture)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.db.user.delete_many(where={"id": {"in": [u.id for u in self.users]}})
        await self.db.disconnect()

    async def _user(self, **data):
        user = await self.db.user.create(data={"email": f"fm-{uuid.uuid4().hex}@example.test", **data})
        self.users.append(user)
        return user

    async def _reload(self, user):
        return await self.db.user.find_unique(where={"id": user.id})

    async def _post(self, path, json, headers=None):
        from app.main import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fm") as client:
            return await client.post(path, json=json, headers=headers or {})

    async def _apple_buy(self, user, payload):
        auth = {"Authorization": f"Bearer {self.security.create_access_token(user.id)}"}
        with patch("app.billing_apple.decode_transaction", lambda jws: payload):
            return await self._post("/billing/apple/transactions", {"signedTransaction": "jws"}, auth)

    async def _apple_notify(self, kind, payload, subtype=None, auto_renew=None):
        from app.billing_apple import AppleNotification

        note = AppleNotification(type=kind, subtype=subtype, transaction=payload, grace_expires_ms=None, auto_renew=auto_renew)
        with patch("app.billing_apple.decode_notification", lambda body: note):
            return await self._post("/billing/apple/notifications", {"signedPayload": "p"})

    async def _apple_user_in_free_month(self, days=3):
        from app.entitlements import account_token_for

        user = await self._user()
        token = await account_token_for(self.db, user.id)
        ends = self.now + timedelta(days=days)
        response = await self._apple_buy(user, _txn(token, ends))
        self.assertEqual(response.status_code, 200, response.text)
        return user, token, ends

    async def _run(self, now=None):
        from app.free_month_reminders import send_free_month_reminders

        await send_free_month_reminders(now or self.now)
        mine = {u.email for u in self.users}
        return [m for m in self.mails if m["to"] in mine]

    async def test_apple_free_month_is_recorded(self):
        user, _, ends = await self._apple_user_in_free_month(days=30)
        saved = await self._reload(user)
        self.assertAlmostEqual(saved.planIntroEndsAt.timestamp(), ends.timestamp(), delta=1)

    async def test_a_paid_apple_purchase_is_not_a_free_month(self):
        from app.entitlements import account_token_for

        user = await self._user()
        token = await account_token_for(self.db, user.id)
        await self._apple_buy(user, _txn(token, self.now + timedelta(days=3), free=False))
        self.assertIsNone((await self._reload(user)).planIntroEndsAt)
        self.assertEqual(await self._run(), [])

    async def test_reminds_once_three_days_before_when_it_will_renew(self):
        user, _, ends = await self._apple_user_in_free_month(days=3)
        sent = await self._run()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["days"], 3)
        self.assertTrue(sent[0]["renews"])
        self.assertEqual(sent[0]["billed_by"], "apple")
        self.assertEqual(sent[0]["manage"], "https://apps.apple.com/account/subscriptions")
        self.assertEqual(await self._run(), sent)  # the next run sends nothing more

    async def test_too_early_and_too_late_send_nothing(self):
        await self._apple_user_in_free_month(days=10)
        await self._apple_user_in_free_month(days=0.3)
        self.assertEqual(await self._run(), [])

    async def test_cancelled_gets_the_ends_version(self):
        user, token, ends = await self._apple_user_in_free_month(days=3)
        renewal_off = _txn(token, ends)
        response = await self._apple_notify("DID_CHANGE_RENEWAL_STATUS", renewal_off, subtype="AUTO_RENEW_DISABLED", auto_renew=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs((await self._reload(user)).planAutoRenew, False)
        sent = await self._run()
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["renews"])

    async def test_turning_renewal_back_on_is_seen(self):
        user, token, ends = await self._apple_user_in_free_month(days=3)
        await self._apple_notify("DID_CHANGE_RENEWAL_STATUS", _txn(token, ends), subtype="AUTO_RENEW_DISABLED", auto_renew=False)
        await self._apple_notify("DID_CHANGE_RENEWAL_STATUS", _txn(token, ends), subtype="AUTO_RENEW_ENABLED", auto_renew=True)
        self.assertIs((await self._reload(user)).planAutoRenew, True)

    async def test_refunded_or_free_accounts_are_skipped(self):
        user = await self._user(planIntroEndsAt=self.now + timedelta(days=3))  # never had Plus
        self.assertEqual(await self._run(), [])

    async def test_paddle_trial_is_recorded_and_reminded(self):
        import json
        import os

        from app import billing_paddle

        user = await self._user(emailVerifiedAt=self.now)
        ends = self.now + timedelta(days=3)
        event = {
            "event_type": "subscription.created",
            "data": {
                "id": f"sub_{uuid.uuid4().hex[:8]}",
                "status": "trialing",
                "items": [{"price": {"id": "pri_plus"}}],
                "custom_data": {"vournalUserId": user.id, "vournalEmail": user.email},
                "current_billing_period": {"starts_at": self.now.isoformat(), "ends_at": ends.isoformat()},
                "scheduled_change": None,
            },
        }
        with patch.dict(os.environ, {"PADDLE_WEBHOOK_SECRET": "s", "PADDLE_PLUS_PRICE_ID": "pri_plus", "PADDLE_MANAGE_URL": "https://paddle.net"}), \
                patch.object(billing_paddle, "verify_paddle_signature", lambda *a: True):
            response = await self._post("/billing/paddle/webhook", json.loads(json.dumps(event)))
            self.assertEqual(response.status_code, 200, response.text)
            saved = await self._reload(user)
            self.assertIsNotNone(saved.planIntroEndsAt)
            self.assertIs(saved.planAutoRenew, True)
            # they cancel during the trial
            event["event_type"] = "subscription.updated"
            event["data"]["scheduled_change"] = {"action": "cancel", "effective_at": ends.isoformat()}
            await self._post("/billing/paddle/webhook", event)
            sent = await self._run()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["billed_by"], "paddle")
        self.assertFalse(sent[0]["renews"])
        self.assertEqual(sent[0]["manage"], "https://paddle.net")
