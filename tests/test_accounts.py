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

        async def capture(to, code, note=None):
            self.sent.append(code)
            return True

        self.links = []

        async def capture_link(to, url):
            self.links.append(url)
            return True

        self.mail = patch("app.email.send_verification_code", capture)
        self.mail.start()
        self.link_mail = patch("app.email.send_plus_confirm_link", capture_link)
        self.link_mail.start()

    async def asyncTearDown(self):
        self.mail.stop()
        self.link_mail.stop()
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

    async def test_buyer_with_an_existing_account_is_emailed_a_confirm_link(self):
        from app.entitlements import effective_plan

        await self._register()  # every account from before build 5 looks like this
        sub = f"sub_{uuid.uuid4().hex}"
        await self._paddle(sub)
        await self._paddle(sub)  # Paddle sends created, then activated
        self.assertEqual(len(self.links), 1)
        token = self.links[0].split("token=")[1]
        # Opening the link, as mail scanners do, changes nothing: it takes a button.
        page = await self.client.get(f"/auth/email/confirm-link?token={token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<form", page.text)
        user = await self.db.user.find_unique(where={"email": self.address})
        self.assertEqual(effective_plan(user), "free")
        done = await self.client.post(f"/auth/email/confirm-link?token={token}")
        self.assertEqual(done.status_code, 200)
        self.assertIn("Forgot password", done.text)  # a squatted account stays reclaimable
        user = await self.db.user.find_unique(where={"email": self.address})
        self.assertIsNotNone(user.emailVerifiedAt)
        self.assertEqual(effective_plan(user), "plus")
        again = await self.client.post(f"/auth/email/confirm-link?token={token}")
        self.assertEqual(again.status_code, 400)

    async def test_buyer_with_no_account_gets_no_link(self):
        await self._paddle(f"sub_{uuid.uuid4().hex}")
        self.assertEqual(self.links, [])

    async def test_a_bad_confirm_link_is_refused(self):
        self.assertEqual((await self.client.post("/auth/email/confirm-link?token=nope")).status_code, 400)

    async def test_a_code_request_does_not_cancel_a_purchase_link(self):
        tokens = (await self._register()).json()
        await self._paddle(f"sub_{uuid.uuid4().hex}")
        await self.client.post("/auth/email/send-code", headers=self._auth(tokens))
        token = self.links[0].split("token=")[1]
        done = await self.client.post(f"/auth/email/confirm-link?token={token}")
        self.assertEqual(done.status_code, 200, done.text)

    async def test_codes_are_capped_per_account(self):
        from fastapi import HTTPException

        from app.auth import begin_email_challenge

        await self._register()
        user = await self.db.user.find_unique(where={"email": self.address})
        for _ in range(6):
            await begin_email_challenge(user)
        with self.assertRaises(HTTPException) as caught:
            await begin_email_challenge(user)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(len(self.sent), 6)

    async def test_wrong_guesses_are_capped_per_account_across_challenges(self):
        body = (await self._register(confirmEmail=True)).json()
        user = await self.db.user.find_unique(where={"email": self.address})
        await self.db.authchallenge.update_many(where={"userId": user.id}, data={"attempts": 20})
        await self.db.authchallenge.create(
            data={
                "userId": user.id,
                "tokenHash": "spent-budget",
                "purpose": "other",
                "attempts": 0,
                "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=5),
            }
        )
        fresh = (await self._login(confirmEmail=True)).json()
        wrong = await self._verify(fresh["challengeToken"], self.sent[-1])
        self.assertEqual(wrong.status_code, 400)
        self.assertNotEqual(body["challengeToken"], fresh["challengeToken"])

    async def test_a_password_reset_ends_old_access_tokens(self):
        from app import security

        tokens = (await self._register()).json()
        user = await self.db.user.find_unique(where={"email": self.address})
        raw = uuid.uuid4().hex
        await self.db.passwordresettoken.create(
            data={
                "userId": user.id,
                "tokenHash": security.hash_refresh_token(raw),
                "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=5),
            }
        )
        await self.client.post("/auth/reset-password", json={"token": raw, "newPassword": PASSWORD})
        self.assertEqual((await self.client.get("/auth/me", headers=self._auth(tokens))).status_code, 401)

    async def test_a_session_issued_before_a_generation_change_is_dead(self):
        """A refresh or login racing a link gets issued against the old generation."""
        from app.auth import _issue_tokens

        await self._register()
        user = await self.db.user.find_unique(where={"email": self.address})
        await self.db.user.update(where={"id": user.id}, data={"sessionGeneration": 1})
        stale = await _issue_tokens(user.id, 0)
        self.assertEqual(
            (await self.client.get("/auth/me", headers={"Authorization": f"Bearer {stale.accessToken}"})).status_code,
            401,
        )
        refreshed = await self.client.post("/auth/refresh", json={"refreshToken": stale.refreshToken})
        self.assertEqual(refreshed.status_code, 401)

    async def test_tokens_from_before_this_build_keep_working(self):
        """Access tokens already on build 4 phones carry no generation."""
        import jwt

        from app import security

        tokens = (await self._register()).json()
        claims = jwt.decode(tokens["accessToken"], security._secret(), algorithms=["HS256"])
        claims.pop("gen", None)
        old = jwt.encode(claims, security._secret(), algorithm="HS256")
        self.assertEqual((await self.client.get("/auth/me", headers={"Authorization": f"Bearer {old}"})).status_code, 200)

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


class ReviewAccount(unittest.TestCase):
    def test_review_account_is_pre_confirmed(self):
        source = open("scripts/seed_review_account.py", encoding="utf-8").read()
        self.assertIn('"emailVerifiedAt"', source)


if __name__ == "__main__":
    unittest.main()
