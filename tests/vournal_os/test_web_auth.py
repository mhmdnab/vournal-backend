"""The web dashboard signs in with vournal accounts, in cookies, never creating one.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import APIRouter, Depends

PASSWORD = "web-passphrase-1"
CSRF = {"X-Requested-With": "life-os"}
PLUS_TEXT = "vournal OS is part of vournal Plus. Subscribe in the vournal app on your iPhone."

def _cookie(response: httpx.Response, name: str) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"no {name} cookie in {response.headers.get_list('set-cookie')}")


class WebAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.main import app
        from app.rate_limit import limiter

        self.db = db
        self.security = security
        self.app = app
        await db.connect()  # each test has its own event loop
        limiter.reset()
        self.emails: list[str] = []
        self.codes: list[str] = []
        self.client = self._new_client()

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.emails:
            await self.db.user.delete_many(where={"email": {"in": self.emails}})
        await self.db.disconnect()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://life.test")

    async def _user(self, **data):
        email = f"web-{uuid.uuid4().hex}@example.test"
        self.emails.append(email)
        fields = {"email": email, "passwordHash": self.security.hash_password(PASSWORD), "plan": "plus"}
        fields.update(data)
        return await self.db.user.create(data=fields)

    async def _login(self, user, client: httpx.AsyncClient | None = None) -> httpx.Response:
        return await (client or self.client).post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF
        )

    async def test_login_sets_http_only_cookies(self):
        user = await self._user()
        # Production's value; the local runner turns Secure off for plain http.
        with patch.dict(os.environ, {"WEB_COOKIE_SECURE": "1"}):
            response = await self._login(user)
        self.assertEqual(response.status_code, 200, response.text)
        access = _cookie(response, "life_at").lower()
        refresh = _cookie(response, "life_rt").lower()
        for part in ("httponly", "samesite=lax", "path=/", "secure"):
            self.assertIn(part, access)
        self.assertIn("path=/api/auth", refresh)
        self.assertIn("httponly", refresh)
        body = response.json()["user"]
        self.assertEqual(body["email"], user.email)
        self.assertIs(body["twoFactorEnabled"], False)
        self.assertEqual(body["plan"], "plus")

    async def test_wrong_password_is_nest_401(self):
        user = await self._user()
        response = await self.client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "not-the-password"},
            headers=CSRF,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"statusCode": 401, "message": "Invalid email or password", "error": "Unauthorized"},
        )

    async def test_passwordless_email_login_is_generic_401(self):
        user = await self._user(passwordHash=None)
        response = await self._login(user)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["message"], "Invalid email or password")

    async def test_unconfirmed_login_sends_a_code(self):
        user = await self._user(emailConfirmRequired=True)
        with patch("app.email.send_verification_code", self._capture):
            response = await self._login(user)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["step"], "verify_email")
        self.assertNotIn("life_at", response.headers.get("set-cookie", ""))
        verified = await self._verify(response.json()["challengeToken"], self.codes[-1])
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["user"]["email"], user.email)

    async def test_refresh_rotates_cookie(self):
        user = await self._user()
        first = await self._login(user)
        old_refresh = self.client.cookies.get("life_rt")
        rotated = await self.client.post("/api/auth/refresh", headers=CSRF)
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(rotated.json(), {"ok": True})
        self.assertNotEqual(self.client.cookies.get("life_rt"), old_refresh)
        self.assertTrue(first.status_code == 200)

        # Once the new cookie has been used, presenting the old token again is a replay.
        # (Before that, it may be a lost answer: test_sessions.LostRefreshAnswer.)
        self.assertEqual((await self.client.post("/api/auth/refresh", headers=CSRF)).status_code, 200)
        await self.db.refreshtoken.update_many(
            where={"userId": user.id, "revoked": False},
            data={"createdAt": datetime.now(timezone.utc) - timedelta(minutes=5)},
        )
        replay = self._new_client()
        replay.cookies.set("life_rt", old_refresh, domain="life.test", path="/api/auth")
        async with replay:
            again = await replay.post("/api/auth/refresh", headers=CSRF)
        self.assertEqual(again.status_code, 401)

    async def test_two_tabs_refreshing_keep_the_phone_signed_in(self):
        # Two dashboard tabs refresh with the same cookie. The second must not look
        # like a stolen token: that would end every session, the iPhone's included.
        from app.auth import _issue_tokens, rotate_refresh_token

        user = await self._user()
        phone = await _issue_tokens(user.id)
        await self._login(user)
        shared = self.client.cookies.get("life_rt")
        first = await self.client.post("/api/auth/refresh", headers=CSRF)
        self.assertEqual(first.status_code, 200, first.text)

        other_tab = self._new_client()
        other_tab.cookies.set("life_rt", shared, domain="life.test", path="/api/auth")
        async with other_tab:
            second = await other_tab.post("/api/auth/refresh", headers=CSRF)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json(), {"ok": True})
        self.assertFalse(any(h.startswith("life_rt=") for h in second.headers.get_list("set-cookie")))

        self.assertEqual((await self.client.get("/api/auth/me")).status_code, 200)
        await rotate_refresh_token(phone.refreshToken)  # raises 401 if the phone was signed out

    async def test_failed_refresh_clears_cookies(self):
        # Stale cookies would make the dashboard bounce between /login and /dashboard.
        dead = self._new_client()
        dead.cookies.set("life_rt", "not-a-token", domain="life.test", path="/api/auth")
        dead.cookies.set("life_at", "not-a-token", domain="life.test", path="/")
        async with dead:
            response = await dead.post("/api/auth/refresh", headers=CSRF)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["statusCode"], 401)
        cleared = {h.split("=", 1)[0] for h in response.headers.get_list("set-cookie") if "max-age=0" in h.lower()}
        self.assertEqual(cleared, {"life_at", "life_rt"})

    async def test_logout_clears_cookies(self):
        user = await self._user()
        await self._login(user)
        out = await self.client.post("/api/auth/logout", headers=CSRF)
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.json(), {"ok": True})
        me = await self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 401)

    async def test_app_session_end_kills_web_cookie(self):
        from app.auth import end_all_sessions

        user = await self._user()
        await self._login(user)
        self.assertEqual((await self.client.get("/api/auth/me")).status_code, 200)
        await end_all_sessions(user.id)
        me = await self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 401)
        self.assertEqual(me.json()["message"], "Session expired")

    async def test_csrf_header_required_for_cookie_writes(self):
        user = await self._user()
        signed_in = await self._login(user)
        bare = await self.client.post("/api/auth/logout-all")
        self.assertEqual(bare.status_code, 403)
        self.assertEqual(bare.json()["message"], "Missing or invalid X-Requested-With header")

        access = signed_in.cookies.get("life_at") or self.client.cookies.get("life_at")
        bearer = self._new_client()
        async with bearer:
            by_token = await bearer.post(
                "/api/auth/logout-all", headers={"Authorization": f"Bearer {access}"}
            )
        self.assertEqual(by_token.status_code, 200, by_token.text)

        again = await self._login(user)
        self.assertEqual(again.status_code, 200)
        with_header = await self.client.post("/api/auth/logout-all", headers=CSRF)
        self.assertEqual(with_header.status_code, 200, with_header.text)

    async def _capture(self, to, code, note=None):
        self.codes.append(code)
        return True

    async def _register(self, email: str, password: str = PASSWORD) -> httpx.Response:
        with patch("app.email.send_verification_code", self._capture):
            return await self.client.post(
                "/api/auth/register", json={"email": email, "password": password}, headers=CSRF
            )

    async def _verify(self, token: str, code: str) -> httpx.Response:
        return await self.client.post(
            "/api/auth/register/verify", json={"challengeToken": token, "code": code}, headers=CSRF
        )

    def _new_email(self) -> str:
        email = f"web-{uuid.uuid4().hex}@example.test"
        self.emails.append(email)
        return email

    async def test_register_sends_a_code_then_signs_in(self):
        email = self._new_email()
        response = await self._register(email)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["step"], "verify_email")
        verified = await self._verify(response.json()["challengeToken"], self.codes[-1])
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertIn("life_at=", _cookie(verified, "life_at"))
        self.assertEqual(verified.json()["user"]["email"], email)
        stored = await self.db.user.find_unique(where={"email": email})
        self.assertIsNotNone(stored.emailVerifiedAt)
        self.assertTrue(stored.emailConfirmRequired)

    async def test_register_wrong_code_is_400(self):
        response = await self._register(self._new_email())
        wrong = "000000" if self.codes[-1] != "000000" else "111111"
        verified = await self._verify(response.json()["challengeToken"], wrong)
        self.assertEqual(verified.status_code, 400, verified.text)

    async def test_register_existing_confirmed_email_is_409(self):
        user = await self._user(emailVerifiedAt=datetime.now(timezone.utc))
        response = await self._register(user.email)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["message"], "An account with that email already exists.")

    async def test_register_never_takes_over_an_account_in_use(self):
        # An older app build made accounts without the code step: unconfirmed, but in use.
        user = await self._user()
        response = await self._register(user.email, "another-pass-99")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((await self._login(user)).status_code, 200)

    async def test_register_again_before_confirming_sends_a_new_code(self):
        email = self._new_email()
        first = await self._register(email)
        first_code = self.codes[-1]
        second = await self._register(email, "second-pass-99")
        self.assertEqual(second.status_code, 200, second.text)
        stale = await self._verify(first.json()["challengeToken"], first_code)
        self.assertEqual(stale.status_code, 400)
        verified = await self._verify(second.json()["challengeToken"], self.codes[-1])
        self.assertEqual(verified.status_code, 200, verified.text)
        login = await self.client.post(
            "/api/auth/login", json={"email": email, "password": "second-pass-99"}, headers=CSRF
        )
        self.assertEqual(login.status_code, 200, login.text)

    async def test_register_rate_limited_per_email(self):
        from app.rate_limit import rate_limited

        email = self._new_email()
        for _ in range(3):
            rate_limited("web-register", email, 3, 3600)
        response = await self._register(email)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIsNone(await self.db.user.find_unique(where={"email": email}))

    async def test_trial_starts_on_web_sign_up(self):
        when = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.dict(os.environ, {"PLUS_TRIAL_FROM": when}):
            response = await self._register(self._new_email())
            verified = await self._verify(response.json()["challengeToken"], self.codes[-1])
            me = await self.client.get("/api/auth/me")
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(me.json()["user"]["plan"], "plus")
        stored = await self.db.user.find_unique(where={"email": self.emails[-1]})
        self.assertEqual(stored.planSource, "trial")

    async def test_unknown_oauth_identity_creates_the_account(self):
        from app.oauth import APPLE_ISSUER, VerifiedIdentity

        email = self._new_email()
        identity = VerifiedIdentity(APPLE_ISSUER, f"sub-{uuid.uuid4().hex}", email, True, True)
        with patch("app.oauth.verify_identity", return_value=identity):
            response = await self.client.post(
                "/api/auth/oauth/apple", json={"idToken": "t", "nonce": "n"}, headers=CSRF
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["email"], email)
        user = await self.db.user.find_unique(where={"email": email})
        self.assertIsNotNone(user.emailVerifiedAt)
        linked = await self.db.externalidentity.find_first(where={"userId": user.id})
        self.assertEqual(linked.subject, identity.subject)

    async def test_unknown_oauth_identity_without_a_verified_email_is_409(self):
        from app.oauth import APPLE_ISSUER, VerifiedIdentity

        email = self._new_email()
        identity = VerifiedIdentity(APPLE_ISSUER, f"sub-{uuid.uuid4().hex}", email, False, False)
        with patch("app.oauth.verify_identity", return_value=identity):
            response = await self.client.post(
                "/api/auth/oauth/apple", json={"idToken": "t", "nonce": "n"}, headers=CSRF
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(await self.db.user.find_unique(where={"email": email}))

    async def test_unlinked_oauth_for_existing_account_points_to_the_app(self):
        # The account exists; only this sign-in method isn't connected to it yet.
        from app.oauth import GOOGLE_ISSUERS, VerifiedIdentity

        user = await self._user()
        identity = VerifiedIdentity(GOOGLE_ISSUERS[0], f"sub-{uuid.uuid4().hex}", user.email, True, True)
        identities = await self.db.externalidentity.count()
        with patch("app.oauth.verify_identity", return_value=identity):
            response = await self.client.post(
                "/api/auth/oauth/google", json={"idToken": "t", "nonce": "n"}, headers=CSRF
            )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "link_in_app")
        self.assertEqual(
            response.json()["message"],
            "Sign in with your email and password, or use this sign-in once in the vournal app.",
        )
        self.assertEqual(await self.db.externalidentity.count(), identities)

    async def _google(self, user, *, verified=True, authoritative=True, email=None):
        from app.oauth import GOOGLE_ISSUERS, VerifiedIdentity

        identity = VerifiedIdentity(GOOGLE_ISSUERS[0], f"sub-{uuid.uuid4().hex}", email or user.email, verified, authoritative)
        with patch("app.oauth.verify_identity", return_value=identity):
            response = await self.client.post("/api/auth/oauth/google", json={"idToken": "t", "nonce": "n"}, headers=CSRF)
        return response, identity

    async def test_google_with_the_confirmed_email_links_and_signs_in(self):
        # Gmail through Google, on an account whose email was confirmed in the app: the same
        # proof the app accepts, so the web links it too (and the app sees the link).
        user = await self._user(emailVerifiedAt=datetime.now(timezone.utc))
        response, identity = await self._google(user, email=user.email.upper())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["email"], user.email)
        _cookie(response, "life_at")
        linked = await self.db.externalidentity.find_many(where={"userId": user.id})
        self.assertEqual([(i.issuer, i.subject) for i in linked], [(identity.issuer, identity.subject)])
        again, _ = await self._google(user)  # a second Google account with the same address is a separate identity
        self.assertEqual(again.status_code, 200, again.text)

    async def test_google_never_links_an_unconfirmed_account(self):
        # whoever set this account's password never proved the address
        user = await self._user()
        response, _ = await self._google(user)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "link_in_app"))
        self.assertEqual(await self.db.externalidentity.count(where={"userId": user.id}), 0)

    async def test_google_links_only_what_it_vouches_for(self):
        user = await self._user(emailVerifiedAt=datetime.now(timezone.utc))
        for kwargs in ({"authoritative": False}, {"verified": False}):
            response, _ = await self._google(user, **kwargs)
            self.assertEqual((response.status_code, response.json()["code"]), (403, "link_in_app"), kwargs)
        self.assertEqual(await self.db.externalidentity.count(where={"userId": user.id}), 0)

    async def test_google_linked_to_someone_else_stays_theirs(self):
        owner = await self._user(emailVerifiedAt=datetime.now(timezone.utc))
        other = await self._user(emailVerifiedAt=datetime.now(timezone.utc))
        response, identity = await self._google(owner)
        self.assertEqual(response.status_code, 200)
        # the same Google identity now carries the other account's address: it signs into its own account
        from app.oauth import VerifiedIdentity

        moved = VerifiedIdentity(identity.issuer, identity.subject, other.email, True, True)
        with patch("app.oauth.verify_identity", return_value=moved):
            again = await self.client.post("/api/auth/oauth/google", json={"idToken": "t", "nonce": "n"}, headers=CSRF)
        self.assertEqual(again.json()["user"]["email"], owner.email)
        self.assertEqual(await self.db.externalidentity.count(where={"userId": other.id}), 0)

    async def test_linked_oauth_identity_signs_in(self):
        from app.oauth import APPLE_ISSUER, VerifiedIdentity

        user = await self._user(passwordHash=None)
        subject = f"sub-{uuid.uuid4().hex}"
        await self.db.externalidentity.create(
            data={"userId": user.id, "issuer": APPLE_ISSUER, "subject": subject}
        )
        identity = VerifiedIdentity(APPLE_ISSUER, subject, user.email, True, True)
        with patch("app.oauth.verify_identity", return_value=identity):
            response = await self.client.post(
                "/api/auth/oauth/apple", json={"idToken": "t", "nonce": "n"}, headers=CSRF
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["email"], user.email)
        _cookie(response, "life_at")

    async def test_lapsed_plus_is_refused(self):
        from fastapi import FastAPI

        from app.os.common.deps import plus_user
        from app.os.common.http import NestJSONResponse, install_nest_errors

        probe = APIRouter(prefix="/api/_test", default_response_class=NestJSONResponse)

        @probe.get("/plus")
        async def plus_only(user=Depends(plus_user)):
            return {"ok": True}

        app = FastAPI()
        install_nest_errors(app)
        app.include_router(probe)
        app.include_router(self.app.router)

        lapsed = await self._user(planExpiresAt=datetime.now(timezone.utc) - timedelta(days=1))
        lifetime = await self._user()
        free = await self._user(plan="free")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://life.test") as c:
            for user, status in ((lapsed, 403), (lifetime, 200)):
                c.cookies.clear()
                await self._login(user, c)
                response = await c.get("/api/_test/plus")
                self.assertEqual(response.status_code, status, response.text)
                if status == 403:
                    self.assertEqual(response.json()["code"], "plus_required")
                    self.assertEqual(response.json()["message"], PLUS_TEXT)
            c.cookies.clear()
            await self._login(free, c)
            me = await c.get("/api/auth/me")
            self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(me.json()["user"]["plan"], "free")


if __name__ == "__main__":
    unittest.main()
