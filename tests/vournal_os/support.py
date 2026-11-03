"""Shared setup for vournal OS tests: a database connection per test and signed-in
Plus users on the in-process app (cookies, like the browser)."""

import unittest
import uuid

import httpx

PASSWORD = "web-passphrase-1"
CSRF = {"X-Requested-With": "life-os"}


class OsTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.rate_limit import limiter

        self.db = db
        self.security = security
        await db.connect()  # each test has its own event loop
        self.addAsyncCleanup(db.disconnect)
        limiter.reset()
        self.emails: list[str] = []
        self.addAsyncCleanup(self._delete_users)

    async def _delete_users(self):
        if self.emails:
            await self.db.user.delete_many(where={"email": {"in": self.emails}})

    async def make_user(self, **data):
        email = f"os-{uuid.uuid4().hex}@example.test"
        self.emails.append(email)
        fields = {"email": email, "passwordHash": self.security.hash_password(PASSWORD), "plan": "plus"}
        fields.update(data)
        return await self.db.user.create(data=fields)

    async def signed_in(self, user=None) -> httpx.AsyncClient:
        """A client signed in through /api/auth/login (a Plus user unless given one)."""
        from app.main import app

        user = user or await self.make_user()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://life.test", headers=CSRF
        )
        self.addAsyncCleanup(client.aclose)
        response = await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        client.user = user
        return client
