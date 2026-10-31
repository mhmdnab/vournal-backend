"""Email confirmation, accounts with no password, and who Plus can reach.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import json
import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

PASSWORD = "local-passphrase"


class Accounts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db
        from app.main import app
        from app.rate_limit import limiter

        self.db = db
        await db.connect()
        limiter.reset()  # the suite registers more than 3 accounts per hour
        self.address = f"accounts-{uuid.uuid4().hex}@example.test"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://accounts"
        )
        self.sent = []

        async def capture(to, code):
            self.sent.append(code)
            return True

        self.mail = patch("app.email.send_verification_code", capture)
        self.mail.start()

    async def asyncTearDown(self):
        self.mail.stop()
        await self.client.aclose()
        await self.db.user.delete_many(where={"email": self.address})
        await self.db.pendinggrant.delete_many(where={"email": self.address})
        await self.db.disconnect()

    async def _register(self, **extra):
        return await self.client.post(
            "/auth/register", json={"email": self.address, "password": PASSWORD, **extra}
        )

    async def _login(self, **extra):
        return await self.client.post(
            "/auth/login", json={"email": self.address, "password": PASSWORD, **extra}
        )

    async def _verify(self, token, code):
        return await self.client.post(
            "/auth/challenge/verify", json={"challengeToken": token, "code": code}
        )

    def _auth(self, tokens):
        return {"Authorization": f"Bearer {tokens['accessToken']}"}

    async def test_build4_register_and_login_still_get_tokens(self):
        registered = await self._register()
        self.assertEqual(registered.status_code, 200, registered.text)
        self.assertIn("accessToken", registered.json())
        self.assertIn("accessToken", (await self._login()).json())
        self.assertEqual(self.sent, [])

    async def test_confirm_flow_gives_tokens_only_after_the_code(self):
        started = await self._register(confirmEmail=True)
        self.assertEqual(started.status_code, 200, started.text)
        body = started.json()
        self.assertEqual(body["step"], "verify_email")
        self.assertNotIn("accessToken", body)
        wrong = "000000" if self.sent[0] != "000000" else "111111"
        self.assertEqual((await self._verify(body["challengeToken"], wrong)).status_code, 400)
        done = await self._verify(body["challengeToken"], self.sent[0])
        self.assertEqual(done.status_code, 200, done.text)
        me = await self.client.get("/auth/me", headers=self._auth(done.json()))
        self.assertTrue(me.json()["emailConfirmed"])
        self.assertTrue(me.json()["hasPassword"])
        # A used code can't be used again.
        self.assertEqual((await self._verify(body["challengeToken"], self.sent[0])).status_code, 400)

    async def test_challenge_expires_and_locks_after_five_tries(self):
        body = (await self._register(confirmEmail=True)).json()
        wrong = "000000" if self.sent[0] != "000000" else "111111"
        for _ in range(5):
            await self._verify(body["challengeToken"], wrong)
        self.assertEqual((await self._verify(body["challengeToken"], self.sent[0])).status_code, 400)

        again = (await self._login(confirmEmail=True)).json()
        await self.db.authchallenge.update_many(
            where={"usedAt": None, "user": {"is": {"email": self.address}}},
            data={"expiresAt": datetime.now(timezone.utc) - timedelta(minutes=1)},
        )
        self.assertEqual((await self._verify(again["challengeToken"], self.sent[-1])).status_code, 400)

    async def test_abandoned_signup_is_asked_again_on_login(self):
        await self._register(confirmEmail=True)
        self.assertEqual((await self._login(confirmEmail=True)).json()["step"], "verify_email")
        self.assertEqual(len(self.sent), 2)
        # A build 4 phone can't answer a challenge, so it isn't sent one.
        self.assertIn("accessToken", (await self._login()).json())

    async def test_existing_account_is_not_blocked(self):
        await self._register()
        logged_in = await self._login(confirmEmail=True)
        self.assertIn("accessToken", logged_in.json())
        me = await self.client.get("/auth/me", headers=self._auth(logged_in.json()))
        self.assertFalse(me.json()["emailConfirmed"])

    async def test_profile_confirm_send_and_verify(self):
        tokens = (await self._register()).json()
        headers = self._auth(tokens)
        sent = await self.client.post("/auth/email/send-code", headers=headers)
        self.assertEqual(sent.json(), {"ok": True, "alreadyConfirmed": False})
        wrong = "000000" if self.sent[0] != "000000" else "111111"
        bad = await self.client.post("/auth/email/confirm", headers=headers, json={"code": wrong})
        self.assertEqual(bad.status_code, 400)
        good = await self.client.post("/auth/email/confirm", headers=headers, json={"code": self.sent[0]})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(good.json()["emailConfirmed"])
        again = await self.client.post("/auth/email/send-code", headers=headers)
        self.assertEqual(again.json(), {"ok": True, "alreadyConfirmed": True})
        self.assertEqual(len(self.sent), 1)

    async def test_profile_send_code_is_limited(self):
        headers = self._auth((await self._register()).json())
        statuses = [
            (await self.client.post("/auth/email/send-code", headers=headers)).status_code
            for _ in range(4)
        ]
        self.assertEqual(statuses, [200, 200, 200, 429])

    async def test_email_failure_returns_503_and_can_retry(self):
        with patch("app.email.send_verification_code", AsyncMock(return_value=False)):
            failed = await self._register(confirmEmail=True)
        self.assertEqual(failed.status_code, 503)
        retried = await self._login(confirmEmail=True)
        self.assertEqual(retried.json()["step"], "verify_email")

    async def test_password_reset_confirms_the_email(self):
        from app import security

        await self._register(confirmEmail=True)
        user = await self.db.user.find_unique(where={"email": self.address})
        raw = uuid.uuid4().hex
        await self.db.passwordresettoken.create(
            data={
                "userId": user.id,
                "tokenHash": security.hash_refresh_token(raw),
                "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=5),
            }
        )
        reset = await self.client.post(
            "/auth/reset-password", json={"token": raw, "newPassword": PASSWORD}
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertIn("accessToken", (await self._login(confirmEmail=True)).json())

    async def _paddle(self, sub):
        import hashlib
        import hmac

        payload = json.dumps(
            {
                "event_type": "subscription.activated",
                "data": {
                    "id": sub,
                    "status": "active",
                    "custom_data": {"vournalEmail": self.address},
                    "items": [{"price": {"id": "pri_plus"}}],
                    "current_billing_period": {"ends_at": "2031-01-01T00:00:00Z"},
                },
            }
        ).encode()
        ts = int(time.time())
        h1 = hmac.new(b"whsec", f"{ts}:".encode() + payload, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, PADDLE_WEBHOOK_SECRET="whsec", PADDLE_PLUS_PRICE_ID="pri_plus"):
            response = await self.client.post(
                "/billing/paddle/webhook",
                content=payload,
                headers={"Paddle-Signature": f"ts={ts};h1={h1}"},
            )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_unconfirmed_account_gets_no_paddle_plus(self):
        from app.entitlements import effective_plan

        tokens = (await self._register()).json()  # anyone can claim an email this way
        await self._paddle(f"sub_{uuid.uuid4().hex}")
        user = await self.db.user.find_unique(where={"email": self.address})
        self.assertEqual(effective_plan(user), "free")
        headers = self._auth(tokens)
        await self.client.post("/auth/email/send-code", headers=headers)
        await self.client.post("/auth/email/confirm", headers=headers, json={"code": self.sent[0]})
        user = await self.db.user.find_unique(where={"email": self.address})
        self.assertEqual(effective_plan(user), "plus")

    async def test_registering_alone_never_applies_a_pending_grant(self):
        from app.entitlements import effective_plan

        await self._paddle(f"sub_{uuid.uuid4().hex}")
        await self._register()
        user = await self.db.user.find_unique(where={"email": self.address})
        self.assertEqual(effective_plan(user), "free")

    async def test_no_password_account_can_delete(self):
        from app import security

        user = await self.db.user.create(data={"email": self.address})
        access = security.create_access_token(user.id)
        headers = {"Authorization": f"Bearer {access}"}
        me = await self.client.get("/auth/me", headers=headers)
        self.assertFalse(me.json()["hasPassword"])
        # No password to guess: the generic login error, as for any wrong password.
        self.assertEqual((await self._login()).status_code, 401)
        wrong = await self.client.request("DELETE", "/auth/me", headers=headers, json={"confirmWord": "delete"})
        self.assertEqual(wrong.status_code, 403)
        done = await self.client.request("DELETE", "/auth/me", headers=headers, json={"confirmWord": "DELETE"})
        self.assertEqual(done.status_code, 204)

    async def test_password_account_still_needs_its_password_to_delete(self):
        headers = self._auth((await self._register()).json())
        word = await self.client.request("DELETE", "/auth/me", headers=headers, json={"confirmWord": "DELETE"})
        self.assertEqual(word.status_code, 403)
        done = await self.client.request("DELETE", "/auth/me", headers=headers, json={"password": PASSWORD})
        self.assertEqual(done.status_code, 204)


class SealedFields(unittest.TestCase):
    def test_encryption_and_purpose_binding(self):
        from cryptography.fernet import Fernet

        from app import mfa

        with patch.dict(os.environ, VOURNAL_FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            stored = mfa.encrypt("apple-refresh-token")
            self.assertNotIn("apple-refresh-token", stored)
            self.assertEqual(mfa.decrypt(stored), "apple-refresh-token")
        self.assertNotEqual(mfa.digest("123456", "email"), mfa.digest("123456", "recovery"))


if __name__ == "__main__":
    unittest.main()
