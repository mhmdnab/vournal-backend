"""Side-by-side checks of the Python /api against the NestJS reference.

Each ParityCase signs in one user on each backend, sends both the same request
and compares the answers once ids and "now" timestamps are masked. Both run at
the same moment, so date-dependent answers ("today", "overdue") stay comparable.

Run only through the guarded runner, with the reference started first:
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py os-reference
    ../voice-journal-backend/.venv/bin/python scripts/dev/isolated_backend.py os-parity
"""

import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

CSRF = {"X-Requested-With": "life-os"}
PARITY_PASSWORD = "parity-passphrase-1"
PYTHON_BASE = "https://life.test"

_CUID = re.compile(r"^c[a-z0-9]{20,}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_NOW_WINDOW = timedelta(minutes=10)


def _is_now(value: str) -> bool:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return abs(datetime.now(timezone.utc) - moment) <= _NOW_WINDOW


def normalize(body: Any) -> Any:
    """Mask what legitimately differs between the two backends: each distinct id
    becomes <id:N> (numbered by first appearance, so references still line up),
    each timestamp from the last few minutes becomes <now>, and a 400's
    validation messages (worded by class-validator vs Pydantic) become one marker."""
    ids: dict[str, str] = {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if node.get("statusCode") == 400 and isinstance(node.get("message"), list):
                node = {**node, "message": "<validation>"}
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        if isinstance(node, str):
            if _CUID.match(node):
                return ids.setdefault(node, f"<id:{len(ids) + 1}>")
            if _ISO.match(node) and _is_now(node):
                return "<now>"
        return node

    return walk(body)


def _without(node: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        return {k: _without(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [_without(v, keys) for v in node]
    return node


async def python_client(user, password: str = PARITY_PASSWORD) -> httpx.AsyncClient:
    """An in-process client on the Python app, signed in as `user` through the
    web's own sign-in route (cookies, like the browser)."""
    from app.main import app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PYTHON_BASE, headers=CSRF
    )
    response = await client.post("/api/auth/login", json={"email": user.email, "password": password})
    if response.status_code != 200:
        await client.aclose()
        raise AssertionError(f"Python sign-in failed: {response.status_code} {response.text}")
    return client


@unittest.skipUnless(
    os.environ.get("NEST_REFERENCE_URL"), "NEST_REFERENCE_URL not set — use isolated_backend.py os-parity"
)
class ParityCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import security
        from app.db import db
        from app.rate_limit import limiter

        self.db = db
        limiter.reset()
        await db.connect()  # each test has its own event loop
        self.email = f"parity-{uuid.uuid4().hex}@example.test"

        self.nest = httpx.AsyncClient(base_url=os.environ["NEST_REFERENCE_URL"], headers=CSRF)
        registered = await self.nest.post(
            "/auth/register",
            json={
                "email": self.email,
                "password": PARITY_PASSWORD,
                "name": "Parity",
                "timezone": "UTC",
                "defaultCurrency": "USD",
                "language": "en",
            },
        )
        if registered.status_code not in (200, 201):
            raise AssertionError(f"NestJS register failed: {registered.status_code} {registered.text}")

        self.user = await db.user.create(
            data={
                "email": self.email,
                "passwordHash": security.hash_password(PARITY_PASSWORD),
                "plan": "plus",
            }
        )
        self.py = await python_client(self.user)
        named = await self.py.patch("/api/users/me", json={"name": "Parity"})
        if named.status_code != 200:
            raise AssertionError(f"Python name update failed: {named.status_code} {named.text}")

    async def asyncTearDown(self):
        await self.nest.aclose()
        await self.py.aclose()
        await self.db.user.delete_many(where={"email": self.email})
        await self.db.disconnect()

    async def both(self, method: str, path: str, json: Any = None) -> tuple[httpx.Response, httpx.Response]:
        """The same request to NestJS (`path` under its /api base) and to Python."""
        kwargs = {} if json is None else {"json": json}
        nest = await self.nest.request(method, path, **kwargs)
        py = await self.py.request(method, f"/api{path}", **kwargs)
        return nest, py

    def assertParity(self, nest: httpx.Response, py: httpx.Response, *, ignore_keys: tuple[str, ...] = ()):
        self.assertEqual(nest.status_code, py.status_code, f"NestJS {nest.text}\nPython {py.text}")
        expected = normalize(_without(nest.json(), ignore_keys)) if nest.content else None
        actual = normalize(_without(py.json(), ignore_keys)) if py.content else None
        self.assertEqual(expected, actual)
