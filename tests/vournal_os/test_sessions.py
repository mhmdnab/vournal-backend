"""Nobody is signed out while their session is still good (2026-11-07).

People were sent back to sign-in on the dashboard, and sometimes in the app, with a
live session. A refresh answer that never arrived looked like a stolen token and ended
every session, and the dashboard's
access cookie vanished after 30 minutes, sending every page load through /login.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check vournal_os.test_sessions
"""

from datetime import datetime, timedelta, timezone

from vournal_os.support import OsTestCase


class LostRefreshAnswer(OsTestCase):
    """The phone sent its refresh token, the server rotated it, and the answer was lost
    (the app went to the background, the network dropped). The phone still holds the old
    token and tries again."""

    async def _rotate(self, raw: str):
        return await self.app.post("/auth/refresh", json={"refreshToken": raw})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.auth import _issue_tokens
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        self.user = await self.make_user()
        self.first = await _issue_tokens(self.user.id, self.user.sessionGeneration)
        self.app = AsyncClient(transport=ASGITransport(app=app), base_url="https://api.test")
        self.addAsyncCleanup(self.app.aclose)

    async def test_the_old_token_works_again_while_its_answer_was_never_used(self):
        web = await self.signed_in(self.user)
        lost = await self._rotate(self.first.refreshToken)
        self.assertEqual(lost.status_code, 200, lost.text)

        again = await self._rotate(self.first.refreshToken)
        self.assertEqual(again.status_code, 200, again.text)
        fresh = again.json()
        me = await self.app.get("/auth/me", headers={"Authorization": f"Bearer {fresh['accessToken']}"})
        self.assertEqual(me.status_code, 200)
        # the dashboard was never touched
        self.assertEqual((await web.post("/api/auth/refresh")).status_code, 200)
        # the answer that never arrived is spent: whoever shows it up later is not the phone
        self.assertEqual((await self._rotate(lost.json()["refreshToken"])).status_code, 401)

    async def test_a_replay_after_the_new_token_was_used_still_ends_every_session(self):
        web = await self.signed_in(self.user)
        second = (await self._rotate(self.first.refreshToken)).json()
        third = await self._rotate(second["refreshToken"])
        self.assertEqual(third.status_code, 200)

        replay = await self._rotate(self.first.refreshToken)
        self.assertEqual(replay.status_code, 401)
        self.assertEqual((await self._rotate(third.json()["refreshToken"])).status_code, 401)
        self.assertEqual((await web.post("/api/auth/refresh")).status_code, 401)

    async def test_the_retry_window_closes_after_a_day(self):
        await self._rotate(self.first.refreshToken)
        await self.db.refreshtoken.update_many(
            where={"userId": self.user.id, "revoked": True},
            data={"revokedAt": datetime.now(timezone.utc) - timedelta(hours=25)},
        )
        self.assertEqual((await self._rotate(self.first.refreshToken)).status_code, 401)

    async def test_a_signed_out_token_is_not_brought_back(self):
        await self.app.post("/auth/logout", json={"refreshToken": self.first.refreshToken})
        self.assertEqual((await self._rotate(self.first.refreshToken)).status_code, 401)


class RequestCeiling(OsTestCase):
    """Everyone on the dashboard reaches the backend through the same proxy, so a
    per-IP ceiling is one shared ceiling (in tests every client is 127.0.0.1 too). A
    refused refresh signs people out, so refresh must stay outside it."""

    async def _spend_the_ip_ceiling(self):
        for _ in range(120):
            await self.app_anon.get("/health")
        self.assertEqual((await self.app_anon.get("/health")).status_code, 429)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        self.app_anon = AsyncClient(transport=ASGITransport(app=app), base_url="https://api.test")
        self.addAsyncCleanup(self.app_anon.aclose)

    async def test_refresh_is_never_refused_by_the_ceiling(self):
        from app.auth import _issue_tokens

        user = await self.make_user()
        web = await self.signed_in(user)
        tokens = await _issue_tokens(user.id, user.sessionGeneration)
        await self._spend_the_ip_ceiling()
        self.assertEqual((await self.app_anon.post("/auth/refresh", json={"refreshToken": tokens.refreshToken})).status_code, 200)
        self.assertEqual((await web.post("/api/auth/refresh")).status_code, 200)


class AccessCookie(OsTestCase):
    async def test_the_access_cookie_lives_as_long_as_the_session(self):
        """The dashboard's page guard only sees life_at (life_rt is scoped to /api/auth).
        If life_at disappears after 30 minutes, every page load goes through /login."""
        from app import security

        user = await self.make_user()
        web = await self.signed_in(user)
        r = await web.post("/api/auth/refresh")
        header = next(h for h in r.headers.get_list("set-cookie") if h.startswith("life_at="))
        max_age = int(header.lower().split("max-age=")[1].split(";")[0])
        self.assertEqual(max_age, int(security._refresh_ttl().total_seconds()))
