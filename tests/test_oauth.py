"""Sign in with Apple and Google: nothing is trusted before the server verifies it.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import hashlib
import json
import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec, rsa

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
APPLE = "https://appleid.apple.com"
GOOGLE = "https://accounts.google.com"
NONCE = "raw-nonce"
HASHED = hashlib.sha256(NONCE.encode()).hexdigest()
ENV = {
    "OAUTH_APPLE_AUDIENCES": "app.vournal.mobile",
    "GOOGLE_OAUTH_CLIENT_IDS": "ios-client,web-client",
    "VOURNAL_FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
}


def token(iss=APPLE, aud="app.vournal.mobile", sub="sub-1", key=KEY, alg="RS256", **claims):
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + 300, "nonce": HASHED, **claims}
    return jwt.encode(payload, key, algorithm=alg)


class OAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import db
        from app.main import app
        from app.rate_limit import limiter

        self.db = db
        await db.connect()
        limiter.reset()
        self.address = f"oauth-{uuid.uuid4().hex}@example.test"
        self.subject = uuid.uuid4().hex
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://oauth"
        )
        self.sent = []

        self.notes = []

        async def capture(to, code, note=None):
            self.sent.append(code)
            self.notes.append(note)
            return True

        self.patches = [
            patch.dict(os.environ, ENV),
            patch("app.email.send_verification_code", capture),
            patch("app.oauth._signing_key", AsyncMock(return_value=KEY.public_key())),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.client.aclose()
        await self.db.user.delete_many(where={"email": self.address})
        await self.db.pendinggrant.delete_many(where={"email": self.address})
        await self.db.disconnect()

    async def _apple(self, id_token=None, nonce=NONCE, **extra):
        id_token = id_token or token(
            sub=self.subject, email=self.address, email_verified="true", is_private_email="true"
        )
        return await self.client.post(
            "/auth/oauth/apple", json={"idToken": id_token, "nonce": nonce, **extra}
        )

    async def _google(self, email_verified=True, **claims):
        id_token = token(
            iss=GOOGLE, aud="ios-client", sub=self.subject, email=self.address,
            email_verified=email_verified, **claims,
        )
        return await self.client.post(
            "/auth/oauth/google", json={"idToken": id_token, "nonce": NONCE}
        )

    async def _me(self, tokens):
        return (
            await self.client.get(
                "/auth/me", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
            )
        ).json()

    async def test_bad_tokens_are_rejected(self):
        email = {"sub": self.subject, "email": self.address, "email_verified": True}
        now = int(time.time())
        for name, bad in {
            "wrong app": token(aud="com.someone.else", **email),
            "wrong issuer": token(iss="https://evil.test", **email),
            "another key": token(key=OTHER_KEY, **email),
            "hmac": token(key="x" * 40, alg="HS256", **email),
            "none": token(key=None, alg="none", **email),
            "expired": jwt.encode(
                {"iss": APPLE, "aud": "app.vournal.mobile", "iat": now - 900, "exp": now - 600, "nonce": HASHED, **email},
                KEY, algorithm="RS256",
            ),
            "wrong nonce": token(**{**email, "nonce": "other"}),
            "not a token": "garbage",
        }.items():
            response = await self._apple(bad)
            self.assertEqual(response.status_code, 401, name)
        self.assertIsNone(await self.db.user.find_unique(where={"email": self.address}))

    async def test_a_google_token_can_not_sign_in_as_apple(self):
        response = await self._apple(token(iss=GOOGLE, sub=self.subject, email=self.address, email_verified=True))
        self.assertEqual(response.status_code, 401)

    async def test_new_account_from_apple_relay_email(self):
        response = await self._apple(timezone="Asia/Beirut")
        self.assertEqual(response.status_code, 200, response.text)
        me = await self._me(response.json())
        self.assertEqual(me["email"], self.address)
        self.assertTrue(me["emailConfirmed"])
        self.assertFalse(me["hasPassword"])
        self.assertEqual(me["timezone"], "Asia/Beirut")
        self.assertEqual(self.sent, [])

    async def test_returning_identity_signs_in_without_email(self):
        first = await self._apple()
        again = await self._apple(token(sub=self.subject))
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual((await self._me(first.json()))["id"], (await self._me(again.json()))["id"])

    async def test_google_new_account_needs_no_nonce_if_the_token_has_none(self):
        now = int(time.time())
        id_token = jwt.encode(
            {"iss": GOOGLE, "aud": "web-client", "sub": self.subject, "iat": now, "exp": now + 300,
             "email": self.address, "email_verified": True},
            KEY, algorithm="RS256",
        )
        response = await self.client.post("/auth/oauth/google", json={"idToken": id_token, "nonce": NONCE})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_links_to_a_confirmed_account(self):
        user = await self.db.user.create(
            data={"email": self.address, "passwordHash": "x", "emailVerifiedAt": datetime.now(timezone.utc)}
        )
        # Google speaks for this address: it's a Workspace domain.
        response = await self._google(hd=self.address.split("@")[1])
        self.assertEqual(response.status_code, 200, response.text)
        me = await self._me(response.json())
        self.assertEqual(me["id"], user.id)
        self.assertTrue(me["hasPassword"])
        linked = await self.db.externalidentity.find_first(where={"userId": user.id})
        self.assertEqual((linked.issuer, linked.subject), (GOOGLE, self.subject))

    async def test_a_confirmed_account_needs_a_code_when_google_cant_vouch_for_the_address(self):
        """Google's flag can be stale for an address it doesn't run: someone may have
        verified it long ago and no longer own it."""
        user = await self.db.user.create(
            data={"email": self.address, "passwordHash": "x", "emailVerifiedAt": datetime.now(timezone.utc)}
        )
        body = (await self._google()).json()
        self.assertEqual(body["step"], "link_identity")
        self.assertFalse(body["clearsPassword"])
        self.assertIsNone(await self.db.externalidentity.find_first(where={"userId": user.id}))
        done = await self.client.post(
            "/auth/challenge/verify", json={"challengeToken": body["challengeToken"], "code": self.sent[0]}
        )
        self.assertEqual(done.status_code, 200, done.text)
        # A confirmed account's password is its owner's: it stays.
        self.assertTrue((await self._me(done.json()))["hasPassword"])

    async def test_linking_ends_the_old_access_token(self):
        registered = (await self.client.post(
            "/auth/register", json={"email": self.address, "password": "someone-elses"}
        )).json()
        body = (await self._google()).json()
        self.assertTrue(body["clearsPassword"])
        self.assertIsNotNone(self.notes[0])
        await self.client.post(
            "/auth/challenge/verify", json={"challengeToken": body["challengeToken"], "code": self.sent[0]}
        )
        old = await self.client.get("/auth/me", headers={"Authorization": f"Bearer {registered['accessToken']}"})
        self.assertEqual(old.status_code, 401)
        gone = await self.client.request(
            "DELETE", "/auth/me",
            headers={"Authorization": f"Bearer {registered['accessToken']}"},
            json={"confirmWord": "DELETE"},
        )
        self.assertEqual(gone.status_code, 401)
        self.assertIsNotNone(await self.db.user.find_unique(where={"email": self.address}))

    async def test_a_failed_link_changes_nothing(self):
        from app import security
        from app.main import app

        user = await self.db.user.create(
            data={"email": self.address, "passwordHash": security.hash_password("someone-elses")}
        )
        body = (await self._google()).json()
        # The identity turns out to belong to someone else by the time the code comes in.
        other = await self.db.user.create(data={"email": f"other-{self.address}"})
        await self.db.externalidentity.create(
            data={"userId": other.id, "issuer": GOOGLE, "subject": self.subject}
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://oauth"
            ) as client:
                failed = await client.post(
                    "/auth/challenge/verify", json={"challengeToken": body["challengeToken"], "code": self.sent[0]}
                )
            self.assertGreaterEqual(failed.status_code, 400)
            user = await self.db.user.find_unique(where={"id": user.id})
            self.assertIsNone(user.emailVerifiedAt)
            self.assertIsNotNone(user.passwordHash)
        finally:
            await self.db.user.delete(where={"id": other.id})

    async def test_key_problems_are_503s_and_bad_keys_are_401s(self):
        import json

        for error, expected in (
            (json.JSONDecodeError("html", "<", 0), 503),
            (ConnectionResetError("dropped"), 503),
            (jwt.PyJWKSetError("no keys"), 503),
            (jwt.PyJWKClientError("unknown kid"), 401),
            (jwt.DecodeError("garbage"), 401),
        ):
            with patch("app.oauth._signing_key", AsyncMock(side_effect=error)):
                self.assertEqual((await self._apple()).status_code, expected, repr(error))

    async def test_the_real_key_client_is_used_and_rejects_forgeries(self):
        """No stubbed key: the real PyJWKClient, with only its download replaced."""
        from app import oauth

        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
        jwk.update(kid="k1", use="sig", alg="RS256")
        client = jwt.PyJWKClient("https://appleid.apple.com/auth/keys")
        client.fetch_data = MagicMock(return_value={"keys": [jwk]})
        good = jwt.encode(
            {"iss": APPLE, "aud": "app.vournal.mobile", "sub": self.subject, "iat": int(time.time()),
             "exp": int(time.time()) + 300, "nonce": HASHED, "email": self.address, "email_verified": True},
            KEY, algorithm="RS256", headers={"kid": "k1"},
        )
        forged = jwt.encode({"iss": APPLE, "sub": self.subject}, None, algorithm="none", headers={"kid": "k1"})
        self.patches[2].stop()
        try:
            with patch.dict(oauth._JWKS, {"apple": client}):
                self.assertEqual((await self._apple(good)).status_code, 200)
                self.assertEqual((await self._apple(forged)).status_code, 401)
        finally:
            self.patches[2].start()

    async def test_a_bad_apple_key_never_fails_a_sign_in(self):
        with patch.dict(os.environ, {"APPLE_TEAM_ID": "T", "APPLE_SIGNIN_KEY_ID": "K", "APPLE_SIGNIN_PRIVATE_KEY": "not a key"}):
            self.assertEqual((await self._apple(authorizationCode="code")).status_code, 200)

    async def test_deleting_an_apple_account_with_no_token_says_so_in_the_log(self):
        tokens = (await self._apple()).json()
        with self.assertLogs("vournal.oauth", level="ERROR"):
            done = await self.client.request(
                "DELETE", "/auth/me",
                headers={"Authorization": f"Bearer {tokens['accessToken']}"},
                json={"confirmWord": "DELETE"},
            )
        self.assertEqual(done.status_code, 204)

    def test_config_problems_are_named(self):
        from app import oauth

        with patch.dict(os.environ, {"APPLE_SIGNIN_PRIVATE_KEY": "", "VOURNAL_FIELD_ENCRYPTION_KEY": ""}):
            problems = oauth.config_problems()
        self.assertTrue(any("APPLE_SIGNIN" in p for p in problems))
        self.assertTrue(any("VOURNAL_FIELD_ENCRYPTION_KEY" in p for p in problems))

    async def test_unconfirmed_account_needs_the_code_before_linking(self):
        from app import security

        user = await self.db.user.create(data={"email": self.address, "passwordHash": security.hash_password("someone-elses")})
        response = await self._google()
        body = response.json()
        self.assertEqual(body["step"], "link_identity")
        self.assertNotIn("accessToken", body)
        self.assertIsNone(await self.db.externalidentity.find_first(where={"userId": user.id}))
        done = await self.client.post(
            "/auth/challenge/verify", json={"challengeToken": body["challengeToken"], "code": self.sent[0]}
        )
        self.assertEqual(done.status_code, 200, done.text)
        me = await self._me(done.json())
        self.assertEqual(me["id"], user.id)
        self.assertTrue(me["emailConfirmed"])
        self.assertEqual(await self.db.user.count(where={"email": self.address}), 1)
        # Whoever set that password never proved this email: it can't stay a way in.
        self.assertFalse(me["hasPassword"])
        login = await self.client.post("/auth/login", json={"email": self.address, "password": "someone-elses"})
        self.assertEqual(login.status_code, 401)
        # And signing in again is now direct.
        self.assertIn("accessToken", (await self._google()).json())

    async def test_linking_an_unconfirmed_account_ends_its_old_sessions(self):
        registered = await self.client.post(
            "/auth/register", json={"email": self.address, "password": "someone-elses"}
        )
        old_refresh = registered.json()["refreshToken"]
        body = (await self._google()).json()
        await self.client.post(
            "/auth/challenge/verify", json={"challengeToken": body["challengeToken"], "code": self.sent[0]}
        )
        refreshed = await self.client.post("/auth/refresh", json={"refreshToken": old_refresh})
        self.assertEqual(refreshed.status_code, 401)

    async def test_unverified_provider_email_never_links(self):
        user = await self.db.user.create(
            data={"email": self.address, "emailVerifiedAt": datetime.now(timezone.utc)}
        )
        response = await self._google(email_verified=False)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(await self.db.externalidentity.find_first(where={"userId": user.id}))

    async def test_unverified_provider_email_makes_no_account(self):
        self.assertEqual((await self._google(email_verified=False)).status_code, 409)
        self.assertIsNone(await self.db.user.find_unique(where={"email": self.address}))

    async def test_no_email_no_identity_is_refused(self):
        response = await self._apple(token(sub=self.subject))
        self.assertEqual(response.status_code, 409)

    async def test_provider_off_without_config(self):
        with patch.dict(os.environ, GOOGLE_OAUTH_CLIENT_IDS=""):
            providers = (await self.client.get("/auth/oauth/providers")).json()
            self.assertEqual(providers["google"], {"enabled": False})
            self.assertEqual(providers["apple"], {"enabled": True})
            self.assertEqual((await self._google()).status_code, 503)

    async def test_jwks_failure_is_a_503_not_a_500(self):
        with patch("app.oauth._signing_key", AsyncMock(side_effect=jwt.PyJWKClientConnectionError("down"))):
            self.assertEqual((await self._apple()).status_code, 503)

    async def test_pending_paddle_grant_reaches_a_new_apple_account(self):
        from app.entitlements import effective_plan

        expires = datetime.now(timezone.utc) + timedelta(days=30)
        await self.db.pendinggrant.create(
            data={"email": self.address, "source": "paddle", "externalId": f"sub_{self.subject}", "expiresAt": expires}
        )
        await self._apple()
        user = await self.db.user.find_unique(where={"email": self.address})
        self.assertEqual(effective_plan(user), "plus")

    def _apple_key_env(self):
        from cryptography.hazmat.primitives import serialization

        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        return {"APPLE_TEAM_ID": "TEAM", "APPLE_SIGNIN_KEY_ID": "KEY", "APPLE_SIGNIN_PRIVATE_KEY": pem}

    async def test_apple_code_is_exchanged_and_sealed_and_delete_revokes(self):
        from app import mfa

        calls = []

        class Fake:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, data=None, **_):
                calls.append((url, data))
                return MagicMock(status_code=200, json=lambda: {"refresh_token": "apple-rt"}, raise_for_status=lambda: None)

        with patch.dict(os.environ, self._apple_key_env()), patch("app.oauth.httpx.AsyncClient", Fake):
            tokens = (await self._apple(authorizationCode="one-time-code")).json()
            user = await self.db.user.find_unique(where={"email": self.address})
            self.assertNotEqual(user.appleRefreshToken, "apple-rt")
            self.assertEqual(mfa.decrypt(user.appleRefreshToken), "apple-rt")
            self.assertTrue(calls[0][0].endswith("/auth/token"))
            self.assertEqual(calls[0][1]["code"], "one-time-code")
            secret = jwt.decode(calls[0][1]["client_secret"], options={"verify_signature": False}, audience=APPLE)
            self.assertEqual((secret["iss"], secret["sub"]), ("TEAM", "app.vournal.mobile"))

            done = await self.client.request(
                "DELETE", "/auth/me",
                headers={"Authorization": f"Bearer {tokens['accessToken']}"},
                json={"confirmWord": "DELETE"},
            )
            self.assertEqual(done.status_code, 204, done.text)
            self.assertTrue(calls[-1][0].endswith("/auth/revoke"))
            self.assertEqual(calls[-1][1]["token"], "apple-rt")

    async def test_delete_survives_apple_being_down(self):
        class Down:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise httpx.ConnectError("down")

        from app import mfa, security

        user = await self.db.user.create(
            data={"email": self.address, "appleRefreshToken": mfa.encrypt("apple-rt")}
        )
        with patch.dict(os.environ, self._apple_key_env()), patch("app.oauth.httpx.AsyncClient", Down):
            done = await self.client.request(
                "DELETE", "/auth/me",
                headers={"Authorization": f"Bearer {security.create_access_token(user.id)}"},
                json={"confirmWord": "DELETE"},
            )
        self.assertEqual(done.status_code, 204, done.text)
        self.assertIsNone(await self.db.user.find_unique(where={"id": user.id}))

    async def test_sign_in_works_when_apple_key_is_not_configured(self):
        with patch.dict(os.environ, {"APPLE_SIGNIN_PRIVATE_KEY": ""}):
            self.assertEqual((await self._apple(authorizationCode="code")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
