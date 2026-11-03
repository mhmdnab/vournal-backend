"""An app account gets its vournal OS profile the first time it opens the web.

Run only through the guarded runner (frontend-mobile-app):
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py check
"""

import asyncio
import unittest
import uuid

import httpx

PASSWORD = "web-passphrase-1"
CSRF = {"X-Requested-With": "life-os"}


class Profile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.main import app
        from app.rate_limit import limiter

        self.db = db
        self.security = security
        await db.connect()  # each test has its own event loop
        limiter.reset()
        self.emails: list[str] = []
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://life.test", headers=CSRF
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.emails:
            await self.db.user.delete_many(where={"email": {"in": self.emails}})
        await self.db.disconnect()

    async def _user(self, **data):
        email = f"profile-{uuid.uuid4().hex}@example.test"
        self.emails.append(email)
        fields = {"email": email, "passwordHash": self.security.hash_password(PASSWORD), "plan": "plus"}
        fields.update(data)
        return await self.db.user.create(data=fields)

    async def _sign_in(self, user):
        response = await self.client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_profile_created_once(self):
        from app.os.common.defaults import default_category_rows
        from app.os.profile import ensure_os_profile

        user = await self._user()
        first = await ensure_os_profile(user)
        second = await ensure_os_profile(user)
        self.assertEqual(first.id, second.id)
        self.assertEqual(await self.db.usersettings.count(where={"userId": user.id}), 1)
        self.assertEqual(
            await self.db.category.count(where={"userId": user.id}), len(default_category_rows("en"))
        )
        self.assertEqual(await self.db.account.count(where={"userId": user.id, "type": "CASH"}), 1)
        self.assertEqual(await self.db.shoppinglist.count(where={"userId": user.id}), 1)
        self.assertEqual(await self.db.exchangerate.count(where={"userId": user.id}), 17)

    async def test_profile_race(self):
        from app.os.profile import ensure_os_profile

        user = await self._user()
        results = await asyncio.gather(ensure_os_profile(user), ensure_os_profile(user))
        self.assertEqual(results[0].id, results[1].id)
        self.assertEqual(await self.db.usersettings.count(where={"userId": user.id}), 1)
        self.assertEqual(await self.db.account.count(where={"userId": user.id, "type": "CASH"}), 1)

    async def test_profile_uses_app_timezone(self):
        from app.os.profile import ensure_os_profile

        user = await self._user(timezone="Asia/Beirut")
        settings = await ensure_os_profile(user)
        self.assertEqual(settings.timezone, "Asia/Beirut")
        self.assertEqual(settings.defaultCurrency, "USD")
        self.assertEqual(settings.language, "en")

    async def test_delete_me_removes_os_rows(self):
        user = await self._user()
        await self._sign_in(user)
        self.assertEqual((await self.client.get("/api/settings")).status_code, 200)
        response = await self.client.request("DELETE", "/api/users/me", json={"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True})
        self.assertIsNone(await self.db.user.find_unique(where={"id": user.id}))
        self.assertEqual(await self.db.usersettings.count(where={"userId": user.id}), 0)
        self.assertEqual(await self.db.category.count(where={"userId": user.id}), 0)
        self.assertEqual(await self.db.account.count(where={"userId": user.id}), 0)
        self.assertEqual((await self.client.get("/api/auth/me")).status_code, 401)

    async def test_delete_me_checks_the_password(self):
        user = await self._user()
        await self._sign_in(user)
        response = await self.client.request("DELETE", "/api/users/me", json={"password": "wrong-one"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["message"], "Password is incorrect.")
        self.assertIsNotNone(await self.db.user.find_unique(where={"id": user.id}))

    async def test_delete_me_passwordless_needs_word(self):
        user = await self._user()
        await self._sign_in(user)
        await self.db.user.update(where={"id": user.id}, data={"passwordHash": None})
        wrong = await self.client.request("DELETE", "/api/users/me", json={"confirmWord": "nope"})
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.json()["message"], "Type DELETE to confirm.")
        right = await self.client.request("DELETE", "/api/users/me", json={"confirmWord": "DELETE"})
        self.assertEqual(right.status_code, 200, right.text)
        self.assertIsNone(await self.db.user.find_unique(where={"id": user.id}))

    async def test_free_user_settings_is_gated(self):
        user = await self._user(plan="free")
        await self._sign_in(user)
        settings = await self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 403)
        self.assertEqual(settings.json()["code"], "plus_required")
        me = await self.client.get("/api/users/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual((await self.client.get("/api/settings/options")).status_code, 200)

    async def test_auth_me_returns_settings(self):
        user = await self._user()
        await self._sign_in(user)
        me = await self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["settings"]["defaultCurrency"], "USD")
        self.assertEqual(me.json()["settings"]["enabledSections"][0], "dashboard")


if __name__ == "__main__":
    unittest.main()
