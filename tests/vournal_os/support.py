"""Shared setup for vournal OS tests: a database connection per test and signed-in
Plus users on the in-process app (cookies, like the browser)."""

import unittest
import uuid

import httpx

PASSWORD = "web-passphrase-1"
CSRF = {"X-Requested-With": "life-os"}


def fake_bucket(case: unittest.TestCase) -> dict[str, bytes]:
    """The private bucket as a dict for one test: app.storage's object helpers read and
    write it instead of R2 (the tests never reach the network)."""
    from unittest.mock import patch

    from app import storage

    objects: dict[str, bytes] = {}

    async def put_object(key: str, data: bytes, content_type: str) -> None:
        objects[key] = bytes(data)

    async def get_object(key: str) -> bytes | None:
        return objects.get(key)

    async def delete_object(key: str) -> bool:
        return objects.pop(key, None) is not None

    async def delete_objects(keys: list[str]) -> None:
        for key in keys:
            objects.pop(key, None)

    for name, fake in (("put_object", put_object), ("get_object", get_object), ("delete_object", delete_object), ("delete_objects", delete_objects)):
        patcher = patch.object(storage, name, fake)
        patcher.start()
        case.addCleanup(patcher.stop)
    return objects


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
        self.objects = fake_bucket(self)

    async def _delete_users(self):
        if self.emails:
            await self.db.user.delete_many(where={"email": {"in": self.emails}})

    async def make_user(self, **data):
        email = f"os-{uuid.uuid4().hex}@example.test"
        self.emails.append(email)
        fields = {"email": email, "passwordHash": self.security.hash_password(PASSWORD), "plan": "plus"}
        fields.update(data)
        return await self.db.user.create(data=fields)

    async def mobile(self, user=None) -> httpx.AsyncClient:
        """A client like the iOS app: the root routes, with a bearer access token."""
        from app.auth import _issue_tokens
        from app.main import app

        user = user or await self.make_user()
        tokens = await _issue_tokens(user.id, user.sessionGeneration)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.test",
            headers={"Authorization": f"Bearer {tokens.accessToken}"},
        )
        self.addAsyncCleanup(client.aclose)
        client.user = user
        return client

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
