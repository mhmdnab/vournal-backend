"""Apple In-App Purchase: purchase verification and App Store Server Notifications.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check

Signed payloads can't be forged in a test, so the decoders are patched with plain
payload objects; the real verifier is exercised only for rejection and for the
production→sandbox fallback.
"""

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx

PRODUCT = "app.vournal.mobile.plus.monthly"


def _ms(days: float) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp() * 1000)


def _txn(token, original="orig-1", days=30, product=PRODUCT, revoked=False):
    return SimpleNamespace(
        productId=product,
        originalTransactionId=original,
        transactionId=f"txn-{uuid.uuid4().hex[:6]}",
        expiresDate=_ms(days),
        appAccountToken=token,
        revocationDate=_ms(-0.01) if revoked else None,
    )


class Decoding(unittest.TestCase):
    def test_garbage_jws_rejected(self):
        from app.billing_apple import InvalidTransaction, decode_transaction

        with self.assertRaises(InvalidTransaction):
            decode_transaction("not.a.jws")

    def test_sandbox_transaction_verifies_with_sandbox_verifier(self):
        from appstoreserverlibrary.models.Environment import Environment
        from appstoreserverlibrary.signed_data_verifier import (
            VerificationException,
            VerificationStatus,
        )

        from app import billing_apple

        payload = _txn("tok")

        class Production:
            def verify_and_decode_signed_transaction(self, jws):
                raise VerificationException(VerificationStatus.INVALID_ENVIRONMENT)

        class Sandbox:
            def verify_and_decode_signed_transaction(self, jws):
                return payload

        fakes = {Environment.PRODUCTION: Production(), Environment.SANDBOX: Sandbox()}
        with patch.object(billing_apple, "verifier_for", lambda env: fakes[env]):
            self.assertIs(billing_apple.decode_transaction("jws"), payload)

    def test_xcode_environment_only_with_local_flag(self):
        from appstoreserverlibrary.models.Environment import Environment

        from app.billing_apple import environments

        with patch.dict(os.environ, {"APPLE_IAP_ALLOW_XCODE": ""}):
            self.assertNotIn(Environment.XCODE, environments())
        with patch.dict(os.environ, {"APPLE_IAP_ALLOW_XCODE": "1"}):
            self.assertIn(Environment.XCODE, environments())


class Endpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.entitlements import account_token_for

        self.db = db
        await db.connect()
        self.users = []
        self.user = await self._user()
        self.token = await account_token_for(db, self.user.id)
        self.auth = {"Authorization": f"Bearer {security.create_access_token(self.user.id)}"}

    async def asyncTearDown(self):
        await self.db.user.delete_many(where={"id": {"in": [u.id for u in self.users]}})
        await self.db.disconnect()

    async def _user(self):
        user = await self.db.user.create(
            data={"email": f"iap-{uuid.uuid4().hex}@example.test"}
        )
        self.users.append(user)
        return user

    async def _reload(self, user=None):
        return await self.db.user.find_unique(where={"id": (user or self.user).id})

    async def _post(self, path, json, headers=None):
        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://iap"
        ) as client:
            return await client.post(path, json=json, headers=headers or {})

    async def _buy(self, payload):
        with patch("app.billing_apple.decode_transaction", lambda jws: payload):
            return await self._post(
                "/billing/apple/transactions", {"signedTransaction": "jws"}, self.auth
            )

    async def _notify(self, kind, payload, subtype=None, grace_ms=None):
        from app.billing_apple import AppleNotification

        note = AppleNotification(
            type=kind, subtype=subtype, transaction=payload, grace_expires_ms=grace_ms
        )
        with patch("app.billing_apple.decode_notification", lambda body: note):
            return await self._post(
                "/billing/apple/notifications", {"signedPayload": "payload"}
            )

    async def test_account_token_endpoint(self):
        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://iap"
        ) as client:
            response = await client.get("/billing/apple/account-token", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"appAccountToken": self.token})

    async def test_purchase_grants_plus_until_expires_date(self):
        payload = _txn(self.token)
        response = await self._buy(payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["plan"], "plus")
        user = await self._reload()
        self.assertEqual(user.appleOriginalTransactionId, "orig-1")
        self.assertAlmostEqual(
            user.planExpiresAt.timestamp() * 1000, payload.expiresDate, delta=1000
        )

    async def test_same_transaction_twice_is_idempotent(self):
        payload = _txn(self.token)
        first = await self._buy(payload)
        second = await self._buy(payload)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json()["planExpiresAt"], second.json()["planExpiresAt"])

    async def test_foreign_account_token_rejected(self):
        from app.entitlements import account_token_for, effective_plan

        other = await self._user()
        other_token = await account_token_for(self.db, other.id)
        response = await self._buy(_txn(other_token))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(effective_plan(await self._reload()), "free")
        self.assertEqual(effective_plan(await self._reload(other)), "free")

    async def test_wrong_product_rejected(self):
        response = await self._buy(_txn(self.token, product="something.else"))
        self.assertEqual(response.status_code, 422, response.text)

    async def test_revoked_transaction_does_not_grant(self):
        from app.entitlements import effective_plan

        response = await self._buy(_txn(self.token, revoked=True))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(effective_plan(await self._reload()), "free")

    async def test_renewal_notification_extends(self):
        await self._buy(_txn(self.token, days=1))
        renewed = _txn(self.token, days=31)
        response = await self._notify("DID_RENEW", renewed)
        self.assertEqual(response.status_code, 200, response.text)
        user = await self._reload()
        self.assertAlmostEqual(
            user.planExpiresAt.timestamp() * 1000, renewed.expiresDate, delta=1000
        )

    async def test_refund_notification_revokes(self):
        from app.entitlements import effective_plan

        for kind in ("REFUND", "REVOKE"):
            await self._buy(_txn(self.token))
            response = await self._notify(kind, _txn(self.token, revoked=True))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(effective_plan(await self._reload()), "free", kind)

    async def test_expired_notification_lets_plan_lapse(self):
        from app.entitlements import effective_plan

        await self._buy(_txn(self.token))
        await self._notify("EXPIRED", _txn(self.token, days=-0.01))
        self.assertEqual(effective_plan(await self._reload()), "free")

    async def test_grace_period_keeps_plus(self):
        from app.entitlements import effective_plan

        await self._buy(_txn(self.token, days=-0.01))
        await self._notify(
            "DID_FAIL_TO_RENEW", _txn(self.token, days=-0.01),
            subtype="GRACE_PERIOD", grace_ms=_ms(6),
        )
        self.assertEqual(effective_plan(await self._reload()), "plus")

    async def test_notification_maps_by_original_transaction_without_token(self):
        await self._buy(_txn(self.token, days=1))
        renewed = _txn(None, days=31)
        await self._notify("DID_RENEW", renewed)
        user = await self._reload()
        self.assertAlmostEqual(
            user.planExpiresAt.timestamp() * 1000, renewed.expiresDate, delta=1000
        )

    async def test_notification_for_unknown_user_is_ok(self):
        response = await self._notify("DID_RENEW", _txn(str(uuid.uuid4()), "orig-x"))
        self.assertEqual(response.status_code, 200, response.text)

    async def test_bad_notification_signature_is_400(self):
        from app.billing_apple import InvalidTransaction

        def boom(body):
            raise InvalidTransaction("bad")

        with patch("app.billing_apple.decode_notification", boom):
            response = await self._post(
                "/billing/apple/notifications", {"signedPayload": "x"}
            )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
