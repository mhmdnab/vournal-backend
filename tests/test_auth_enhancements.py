"""Run only through the guarded isolated_backend.py check command."""

import os
import time
import unittest
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from app import mfa
from app.enhancements import enabled


class Primitives(unittest.TestCase):
    def test_totp_vector_window_and_replay(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(mfa.code_at(secret, 1), "287082")
        self.assertEqual(mfa.verify(secret, "287082", now=59), 1)
        self.assertIsNone(mfa.verify(secret, "287082", last_step=1, now=59))
        self.assertIsNone(mfa.verify(secret, "abcdef", now=59))
        self.assertIsNone(mfa.verify(secret, "287082", now=180))

    def test_encryption_and_purpose_binding(self):
        secret = mfa.new_secret()
        stored = mfa.encrypt(secret)
        self.assertNotIn(secret, stored)
        self.assertEqual(mfa.decrypt(stored), secret)
        self.assertNotEqual(
            mfa.digest("123456", "email"), mfa.digest("123456", "recovery")
        )

    @patch.dict(os.environ, {"VOURNAL_ENHANCEMENTS": "1"})
    def test_refuse_remote_or_non_dedicated_database(self):
        with patch.dict(os.environ, DATABASE_URL="postgresql://localhost/production"):
            with self.assertRaises(RuntimeError):
                enabled()
        with patch.dict(
            os.environ, DIRECT_URL="postgresql://example.com/vournal_enhancements"
        ):
            with self.assertRaises(RuntimeError):
                enabled()


class AuthIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_auth_contract_when_flag_is_off(self):
        from app.db import db
        from app.main import app

        await db.connect()
        address = f"legacy-{uuid.uuid4().hex}@example.test"
        try:
            with patch.dict(os.environ, VOURNAL_ENHANCEMENTS="0"):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://legacy"
                ) as client:
                    response = await client.post(
                        "/auth/register",
                        json={"email": address, "password": "local-passphrase"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("accessToken", response.json())
                    self.assertNotIn("challengeToken", response.json())
                    self.assertEqual(
                        (await client.get("/auth/oauth/providers")).status_code, 404
                    )
                    from app.extraction import _normalize

                    todo = _normalize({"todos": [{"text": "Legacy task"}]})["todos"][0]
                    self.assertLessEqual({"text", "dueDate", "time"}, set(todo))
                    self.assertIn("sourceText", todo)
        finally:
            await db.user.delete_many(where={"email": address})
            await db.disconnect()

    @patch.dict(os.environ, {"VOURNAL_ENHANCEMENTS": "1"})
    async def test_email_totp_recovery_refresh_and_lockout(self):
        self.assertTrue(enabled())
        from app.db import db
        from app.main import app
        from app import security

        await db.connect()
        account = f"test-{uuid.uuid4().hex}@example.test"
        credentials = {
            "email": account,
            "password": "local-test-passphrase",
            "timezone": "Asia/Beirut",
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch(
                    "app.email.send_verification_code",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as delivery:
                    response = await client.post("/auth/register", json=credentials)
                self.assertEqual(response.status_code, 200, response.text)
                challenge = response.json()
                self.assertNotIn("accessToken", challenge)
                self.assertEqual(challenge["step"], "verify_email")
                user = await db.user.find_unique(where={"email": account})
                stored = await db.authchallenge.find_first(where={"userId": user.id})
                email_code = delivery.call_args.args[1]
                self.assertNotEqual(stored.codeHash, email_code)
                self.assertNotEqual(stored.tokenHash, challenge["challengeToken"])
                data = {"challengeToken": challenge["challengeToken"], "code": "wrong"}
                self.assertEqual(
                    (
                        await client.post("/auth/challenge/verify", json=data)
                    ).status_code,
                    400,
                )
                data["code"] = email_code
                response = await client.post("/auth/challenge/verify", json=data)
                self.assertEqual(response.status_code, 200, response.text)
                enrollment = response.json()
                self.assertEqual(enrollment["step"], "enroll_totp")
                self.assertEqual(
                    (
                        await client.post("/auth/challenge/verify", json=data)
                    ).status_code,
                    400,
                )
                response = await client.post(
                    "/auth/challenge/verify",
                    json={
                        "challengeToken": enrollment["challengeToken"],
                        "code": mfa.code_at(
                            enrollment["secret"], int(time.time() // 30)
                        ),
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                tokens = response.json()
                self.assertEqual(len(tokens["recoveryCodes"]), 8)
                headers = {"Authorization": f"Bearer {tokens['accessToken']}"}
                self.assertEqual(
                    (await client.get("/auth/me", headers=headers)).status_code, 200
                )
                legacy = {
                    "Authorization": f"Bearer {security.create_access_token(user.id)}"
                }
                self.assertEqual(
                    (await client.get("/auth/me", headers=legacy)).status_code, 401
                )
                response = await client.post(
                    "/auth/refresh", json={"refreshToken": tokens["refreshToken"]}
                )
                self.assertEqual(response.status_code, 200, response.text)
                # Password success never grants a session before MFA.
                challenge = (await client.post("/auth/login", json=credentials)).json()
                self.assertEqual(challenge["step"], "verify_totp")
                data = {
                    "challengeToken": challenge["challengeToken"],
                    "code": tokens["recoveryCodes"][0],
                }
                self.assertEqual(
                    (
                        await client.post("/auth/challenge/verify", json=data)
                    ).status_code,
                    200,
                )
                challenge = (await client.post("/auth/login", json=credentials)).json()
                data["challengeToken"] = challenge["challengeToken"]
                self.assertEqual(
                    (
                        await client.post("/auth/challenge/verify", json=data)
                    ).status_code,
                    400,
                )
                for _ in range(4):
                    await client.post("/auth/challenge/verify", json=data)
                data["code"] = tokens["recoveryCodes"][1]
                self.assertEqual(
                    (
                        await client.post("/auth/challenge/verify", json=data)
                    ).status_code,
                    400,
                )
                # Stored secrets and recovery codes are never plaintext.
                stored_user = await db.user.find_unique(where={"id": user.id})
                self.assertNotEqual(stored_user.twoFactorSecret, enrollment["secret"])
                self.assertNotIn(
                    tokens["recoveryCodes"][1], stored_user.recoveryCodeHashes
                )
                providers = (await client.get("/auth/oauth/providers")).json()
                self.assertFalse(providers["apple"]["enabled"])
                self.assertFalse(providers["google"]["enabled"])
                self.assertEqual(
                    (
                        await client.post(
                            "/auth/oauth/apple/exchange",
                            json={
                                "code": "untrusted",
                                "codeVerifier": "x" * 43,
                                "state": "y" * 32,
                            },
                        )
                    ).status_code,
                    503,
                )
                # Concurrent consumption cannot use the same recovery code twice.
                from app.auth import _begin_challenge

                # A stale pre-enrollment user object can never request a new
                # enrollment after another request has enabled the factor.
                stale_challenge = await _begin_challenge(user)
                self.assertEqual(stale_challenge.step, "verify_totp")
                latest = await db.user.find_unique(where={"id": user.id})
                pending = await _begin_challenge(latest)
                payload = {
                    "challengeToken": pending.challengeToken,
                    "code": tokens["recoveryCodes"][2],
                }
                results = await asyncio.gather(
                    *(
                        client.post("/auth/challenge/verify", json=payload)
                        for _ in range(2)
                    )
                )
                self.assertEqual(sorted(r.status_code for r in results), [200, 400])
                session = next(r.json() for r in results if r.status_code == 200)
                rotations = await asyncio.gather(
                    *(
                        client.post(
                            "/auth/refresh",
                            json={"refreshToken": session["refreshToken"]},
                        )
                        for _ in range(2)
                    )
                )
                self.assertEqual(sorted(r.status_code for r in rotations), [200, 401])
                pending = await _begin_challenge(latest)
                await db.authchallenge.update_many(
                    where={"userId": user.id, "usedAt": None},
                    data={
                        "expiresAt": datetime.now(timezone.utc) - timedelta(seconds=1)
                    },
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/auth/challenge/verify",
                            json={
                                "challengeToken": pending.challengeToken,
                                "code": tokens["recoveryCodes"][3],
                            },
                        )
                    ).status_code,
                    400,
                )
        finally:
            await db.user.delete_many(where={"email": account})
            await db.disconnect()
